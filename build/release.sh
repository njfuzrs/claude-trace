#!/bin/bash
# build/release.sh — 构建并打包发布
#
# 用法：
#   ./build/release.sh                    # 构建当前架构并打包（不上传）
#   ./build/release.sh --bump 0.3.0       # 同步改 version + pyproject + CHANGELOG，然后构建
#   ./build/release.sh --upload           # 构建并发布到 GitHub Releases
#   ./build/release.sh --cross            # 尝试交叉编译双架构（需要 Rosetta）
#   ./build/release.sh --tag              # 发布后打本地 git tag（--upload 时 GitHub 侧也会建 tag）
#
# 环境变量：
#   GH_REPO  — 目标仓库（默认 njfuzrs/claude-trace，fork 时覆盖此值）
#
# 鉴权：--upload 使用 gh CLI 自带鉴权，脚本内不持有任何 token。
#   首次使用先执行：gh auth login
#
# 发版顺序（照这个顺序走，别跳步）：
#   ./build/release.sh --bump x.y.z   → 检查 git diff → commit
#   ./build/release.sh --upload --tag → 验证 curl 能下到 tarball

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARCH="$(uname -m)"
DIST_DIR="$ROOT/dist"

GH_REPO="${GH_REPO:-njfuzrs/claude-trace}"

DO_UPLOAD=false
DO_CROSS=false
DO_TAG=false
BUMP_TO=""

while [ $# -gt 0 ]; do
    case "$1" in
        --upload) DO_UPLOAD=true ;;
        --cross)  DO_CROSS=true ;;
        --tag)    DO_TAG=true ;;
        --bump)
            shift
            BUMP_TO="${1:-}"
            if [ -z "$BUMP_TO" ]; then
                echo "错误：--bump 需要版本号，例如 --bump 0.3.0"
                exit 1
            fi
            ;;
        --help|-h)
            sed -n '2,20p' "$0"
            exit 0
            ;;
    esac
    shift
done

# ─── bump：把版本号写进所有事实源 ───

# 这一步存在的理由：在它之前，整条发版流程里没有「版本」这个步骤 —— 构建只读
# version 文件，发布也只读，于是所有构建都叫 0.2.0，问「当前用的是哪一份」
# 只能去比对二进制的 mtime / sha256。版本号必须由脚本一次改全，不能人肉同步。
if [ -n "$BUMP_TO" ]; then
    if ! [[ "$BUMP_TO" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z._-]+)?$ ]]; then
        echo "错误：版本号 $BUMP_TO 不是合法的 x.y.z"
        exit 1
    fi

    OLD_VERSION="$(cat "$ROOT/version")"
    echo ">>> bump 版本: $OLD_VERSION → $BUMP_TO"

    python3 - "$ROOT" "$BUMP_TO" <<'PYEOF'
import re
import sys
from datetime import date
from pathlib import Path

root = Path(sys.argv[1])
new = sys.argv[2]

# 1. version 文件 —— 单一事实源
(root / "version").write_text(new + "\n", encoding="utf-8")
print(f"  ✅ version → {new}")

# 2. pyproject.toml —— 必须跟着改，否则包元数据与发布产物对不上。
#    只替换 [project] 段内行首的 version，不碰 tool.* 段里的同名键。
pyproject = root / "pyproject.toml"
text = pyproject.read_text(encoding="utf-8")
m = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
if not m:
    sys.exit("pyproject.toml 里找不到 [project] 段")
section = m.group(1)
patched, n = re.subn(
    r'^(\s*version\s*=\s*")[^"]+(")',
    lambda mm: mm.group(1) + new + mm.group(2),
    section,
    count=1,
    flags=re.M,
)
if n != 1:
    sys.exit("pyproject.toml 的 [project] 段里找不到 version 行")
pyproject.write_text(text[: m.start(1)] + patched + text[m.end(1):], encoding="utf-8")
print(f"  ✅ pyproject.toml → {new}")

# 3. CHANGELOG：把 [Unreleased] 切成 [x.y.z] - YYYY-MM-DD，并留一个新的空 Unreleased。
#    不切的话，已完成的变更对外永远显示「未发布」。
changelog = root / "CHANGELOG.md"
text = changelog.read_text(encoding="utf-8")
if f"## [{new}]" in text:
    print(f"  ⏭  CHANGELOG 已有 [{new}] 区段，跳过")
elif "## [Unreleased]" in text:
    text = text.replace(
        "## [Unreleased]",
        f"## [Unreleased]\n\n## [{new}] - {date.today().isoformat()}",
        1,
    )
    changelog.write_text(text, encoding="utf-8")
    print(f"  ✅ CHANGELOG [Unreleased] → [{new}]")
else:
    print("  ⚠️  CHANGELOG 里没有 [Unreleased] 区段，未改动")
PYEOF

    echo ""
    echo "  版本已改，请检查后提交："
    echo "    git diff --stat"
    echo "    git add version pyproject.toml CHANGELOG.md && git commit -m \"chore(release): v$BUMP_TO\""
    echo ""
    echo "  然后发版："
    echo "    ./build/release.sh --upload --tag"
    # bump 是改文件，不是打包装。构建留给下一次显式调用，
    # 否则「改个版本号」要等 PyInstaller 跑完，检查 diff 的窗口就被冲掉了。
    exit 0
fi

VERSION="$(cat "$ROOT/version")"

# ─── 事实源一致性门禁 ───

# 发布前断言两处版本号一致。这是「改一处漏一处」的唯一拦截点 ——
# 漏掉的后果不是报错，而是发出去的包与包元数据悄悄对不上。
PYPROJECT_VERSION="$(python3 - "$ROOT" <<'PYEOF'
import re
import sys
from pathlib import Path

text = (Path(sys.argv[1]) / "pyproject.toml").read_text(encoding="utf-8")
m = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
v = re.search(r'^\s*version\s*=\s*"([^"]+)"', m.group(1), re.M) if m else None
print(v.group(1) if v else "")
PYEOF
)"

if [ "$VERSION" != "$PYPROJECT_VERSION" ]; then
    echo "错误：版本号不一致 —— version 文件是 $VERSION，pyproject.toml 是 ${PYPROJECT_VERSION:-<未找到>}"
    echo "      用 ./build/release.sh --bump $VERSION 一次改全，不要手工只改一处。"
    exit 1
fi

echo "=== claude-trace 发布 v$VERSION ==="
echo ""

# ─── 构建当前架构 ───

echo ">>> 构建 $ARCH ..."
"$SCRIPT_DIR/build.sh" --package

TARBALL_CURRENT="$DIST_DIR/claude-trace-${VERSION}-darwin-${ARCH}.tar.gz"
if [ ! -f "$TARBALL_CURRENT" ]; then
    echo "错误：构建失败"
    exit 1
fi

TARBALLS=("$TARBALL_CURRENT")

# ─── 交叉编译（可选） ───

if [ "$DO_CROSS" = true ]; then
    if [ "$ARCH" = "arm64" ]; then
        OTHER_ARCH="x86_64"
    else
        OTHER_ARCH="arm64"
    fi

    echo ""
    echo ">>> 交叉编译 $OTHER_ARCH ..."

    if [ "$ARCH" = "arm64" ] && [ "$OTHER_ARCH" = "x86_64" ]; then
        # ARM Mac 上用 Rosetta 编译 x86_64
        if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
            echo "⚠️  Rosetta 未安装，跳过 x86_64 构建"
            echo "   安装 Rosetta: softwareupdate --install-rosetta"
        else
            # 在 Rosetta 下运行构建
            arch -x86_64 /bin/bash "$SCRIPT_DIR/build.sh" --package
            TARBALL_OTHER="$DIST_DIR/claude-trace-${VERSION}-darwin-${OTHER_ARCH}.tar.gz"
            if [ -f "$TARBALL_OTHER" ]; then
                TARBALLS+=("$TARBALL_OTHER")
                echo "✅ $OTHER_ARCH 构建完成"
            else
                echo "⚠️  $OTHER_ARCH 构建失败"
            fi
        fi
    else
        echo "⚠️  从 $ARCH 交叉编译到 $OTHER_ARCH 不支持，请在对应架构的机器上构建"
    fi
fi

# ─── 生成 install.sh 的发布版本 ───
#
# 下载 URL 用的是 asset 的 *文件名*，不是 # 后面的展示标签。
# v0.3.0 第一次上传写成 dist/install.sh.release#install.sh，结果
# GitHub 上的名字是 install.sh.release，curl | bash 404。
# 正确做法：磁盘上就放一份叫 install.sh 的文件再上传。

echo ""
echo ">>> 生成发布版 install.sh ..."

# 发布版 install.sh 脱离仓库运行（curl | bash），读不到仓库里的 version 文件，
# 所以在这里把版本号固化进去，其 RELEASE_BASE 会指向对应 tag 的 assets。
#
# 不用 sed：VERSION 默认值里有 $(cat ".../version")，斜杠和引号会把
# sed 的 s/// 模式弄断。按「以 VERSION= 开头的那一行」整行替换更稳。
INSTALL_RELEASE="$DIST_DIR/install.sh.release"
python3 - "$ROOT/dist/install.sh" "$INSTALL_RELEASE" "$VERSION" <<'PYEOF'
import sys
from pathlib import Path

src, dst, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
out = []
replaced = 0
for line in lines:
    if line.startswith("VERSION="):
        out.append(f'VERSION="${{VERSION:-{version}}}"\n')
        replaced += 1
    else:
        out.append(line)
if replaced != 1:
    sys.exit(f"install.sh 里找到 {replaced} 处 VERSION= 赋值，期望恰好 1 处")
dst.write_text("".join(out), encoding="utf-8")
PYEOF
chmod +x "$INSTALL_RELEASE"

if ! grep -q "^VERSION=\"\${VERSION:-${VERSION}}\"\$" "$INSTALL_RELEASE"; then
    echo "错误：install.sh 的 VERSION 注入失败（上游格式已变？）"
    exit 1
fi

RELEASE_URL="https://github.com/${GH_REPO}/releases/download/v${VERSION}"
echo "  下载地址: $RELEASE_URL"
echo "  install.sh: $INSTALL_RELEASE"

# ─── 汇总 ───

echo ""
echo "=== 发布产物 ==="
for f in "${TARBALLS[@]}"; do
    SIZE=$(du -h "$f" | cut -f1)
    echo "  $f ($SIZE)"
done
echo "  $INSTALL_RELEASE"

# ─── 发布到 GitHub Releases（可选） ───

if [ "$DO_UPLOAD" = true ]; then
    echo ""
    echo ">>> 发布到 GitHub Releases ..."

    if ! command -v gh >/dev/null 2>&1; then
        echo "错误：未找到 gh CLI，请先安装：brew install gh"
        exit 1
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "错误：gh 未登录，请先执行：gh auth login"
        exit 1
    fi

    NOTES="claude-trace v${VERSION}

安装：
\`\`\`bash
curl -fsSL ${RELEASE_URL}/install.sh | bash
\`\`\`"

    # 上传文件名必须就是 install.sh。gh 的 path#label 只改 Release 页展示文字，
    # 不改下载路径 —— v0.3.0 第一次写成 install.sh.release#install.sh，
    # 用户 curl 的是 /install.sh，实际 asset 叫 install.sh.release，404。
    # 源文件仍叫 install.sh.release，避免和仓库里的 dist/install.sh 撞名。
    prepare_install_asset() {
        INSTALL_ASSET_DIR="$(mktemp -d)"
        trap 'rm -rf "$INSTALL_ASSET_DIR"' EXIT
        INSTALL_ASSET="$INSTALL_ASSET_DIR/install.sh"
        cp "$INSTALL_RELEASE" "$INSTALL_ASSET"
        chmod +x "$INSTALL_ASSET"
        ASSETS=("${TARBALLS[@]}" "$INSTALL_ASSET")
        # 自检：传给 gh 的路径 basename 必须是 install.sh。#label 改不了下载名。
        local name
        name="$(basename "$INSTALL_ASSET")"
        if [ "$name" != "install.sh" ]; then
            echo "错误：安装器 asset 文件名是 $name，下载 URL 会变成 /$name 而不是 /install.sh"
            exit 1
        fi
        if [[ "$INSTALL_ASSET" == *#* ]]; then
            echo "错误：不要用 path#label 伪装文件名，GitHub 下载路径看的是磁盘文件名"
            exit 1
        fi
    }

    if gh release view "v${VERSION}" --repo "$GH_REPO" >/dev/null 2>&1; then
        # 覆盖已发布的 asset 要显式确认：覆盖之后「同一个 tag 对应两份行为完全
        # 不同的二进制」，而已经装过的用户不会知道自己装的是哪一份 —— 这正是
        # 0.2.0 时期「版本号相同、行为差 13 天」的成因。默认不许覆盖，要发新东西请 bump。
        echo "  ⚠️  Release v${VERSION} 已存在。"
        if [ "${ALLOW_CLOBBER:-false}" != true ]; then
            echo ""
            echo "  拒绝覆盖已发布的 assets。覆盖会让同一个 tag 对应两份不同的二进制，"
            echo "  已安装的用户无法分辨自己装的是哪一份。"
            echo ""
            echo "  正确做法：发一个新版本"
            echo "    ./build/release.sh --bump <x.y.z>   # 然后 commit"
            echo "    ./build/release.sh --upload --tag"
            echo ""
            echo "  确实要覆盖（例如上一次上传只传了一半）："
            echo "    ALLOW_CLOBBER=true ./build/release.sh --upload"
            exit 1
        fi
        echo "  ALLOW_CLOBBER=true，覆盖上传 assets ..."
        prepare_install_asset
        gh release upload "v${VERSION}" "${ASSETS[@]}" --repo "$GH_REPO" --clobber
    else
        echo "  创建 Release v${VERSION} ..."
        prepare_install_asset
        gh release create "v${VERSION}" "${ASSETS[@]}" \
            --repo "$GH_REPO" \
            --title "v${VERSION}" \
            --notes "$NOTES"
    fi

    # ─── 发布后验收：真的下一次 ───

    # 不验的话，「发布成功」只代表 gh 没报错，不代表用户能装上 ——
    # 文件名对不上的那个 bug 就是这么活了很久：脚本一路绿，curl 一路 404。
    echo ""
    echo ">>> 验收：检查 assets 可下载 ..."
    VERIFY_FAILED=false
    for f in "${TARBALLS[@]}" ; do
        name="$(basename "$f")"
        if curl -fsIL "${RELEASE_URL}/${name}" >/dev/null 2>&1; then
            echo "  ✅ ${name}"
        else
            echo "  ❌ ${name} 取不到: ${RELEASE_URL}/${name}"
            VERIFY_FAILED=true
        fi
    done
    if curl -fsIL "${RELEASE_URL}/install.sh" >/dev/null 2>&1; then
        echo "  ✅ install.sh"
    else
        echo "  ❌ install.sh 取不到: ${RELEASE_URL}/install.sh"
        VERIFY_FAILED=true
    fi

    # 再核一遍安装器真正会去请求的文件名与已上传的 asset 名一致 —— 这两者
    # 分别由 install.sh 和 build.sh 拼出来，曾经悄悄对不上。
    echo ""
    echo ">>> 验收：安装器候选名与 assets 对齐 ..."
    for want_arch in arm64 x86_64; do
        expected="claude-trace-${VERSION}-darwin-${want_arch}.tar.gz"
        if ! printf '%s\n' "${TARBALLS[@]}" | grep -qF "$expected"; then
            continue  # 这次没构建这个架构，跳过
        fi
        if VERSION="$VERSION" ARCH="$want_arch" RELEASE_BASE="$RELEASE_URL" \
             bash "$ROOT/dist/install.sh" --print-plan | grep -qxF "candidate=$expected"; then
            echo "  ✅ ${want_arch}: 安装器会请求 $expected"
        else
            echo "  ❌ ${want_arch}: 安装器的候选名里没有 $expected"
            VERIFY_FAILED=true
        fi
    done

    if [ "$VERIFY_FAILED" = true ]; then
        echo ""
        echo "❌ 发布已上传，但验收未通过 —— 用户很可能装不上，请先修好再对外公布链接。"
        exit 1
    fi

    # ─── 本地 git tag（可选） ───

    if [ "$DO_TAG" = true ]; then
        echo ""
        if git -C "$ROOT" rev-parse "v${VERSION}" >/dev/null 2>&1; then
            echo "  ⏭  本地已有 tag v${VERSION}"
        else
            git -C "$ROOT" tag -a "v${VERSION}" -m "claude-trace v${VERSION}"
            echo "  ✅ 已打本地 tag v${VERSION}（推送：git push origin v${VERSION}）"
        fi
    fi

    echo ""
    echo "=== 发布完成 ==="
    echo ""
    echo "安装命令："
    echo "  curl -fsSL ${RELEASE_URL}/install.sh | bash"
else
    echo ""
    echo "提示：添加 --upload 参数可发布到 GitHub Releases（需先 gh auth login）"
    echo "  ./build/release.sh --upload --tag"
fi
