#!/bin/bash
# build/release.sh — 构建并打包发布
#
# 用法：
#   ./build/release.sh                    # 构建当前架构并打包（不上传）
#   ./build/release.sh --upload           # 构建并发布到 GitHub Releases
#   ./build/release.sh --cross            # 尝试交叉编译双架构（需要 Rosetta）
#
# 环境变量：
#   GH_REPO  — 目标仓库（默认 njfuzrs/claude-trace，fork 时覆盖此值）
#
# 鉴权：--upload 使用 gh CLI 自带鉴权，脚本内不持有任何 token。
#   首次使用先执行：gh auth login

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="$(cat "$ROOT/version")"
ARCH="$(uname -m)"
DIST_DIR="$ROOT/dist"

GH_REPO="${GH_REPO:-njfuzrs/claude-trace}"

DO_UPLOAD=false
DO_CROSS=false

for arg in "$@"; do
    case "$arg" in
        --upload) DO_UPLOAD=true ;;
        --cross)  DO_CROSS=true ;;
    esac
done

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

echo ""
echo ">>> 生成发布版 install.sh ..."

# 发布版 install.sh 脱离仓库运行（curl | bash），读不到仓库里的 version 文件，
# 所以在这里把版本号固化进去，其 RELEASE_BASE 会指向对应 tag 的 assets。
INSTALL_RELEASE="$DIST_DIR/install.sh.release"
sed "s|^VERSION=\"\${VERSION:-.*\$|VERSION=\"\${VERSION:-${VERSION}}\"|" \
    "$ROOT/dist/install.sh" > "$INSTALL_RELEASE"

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

    # asset 名固定为 install.sh（gh 的 path#label 语法），保证 curl | bash 地址稳定
    ASSETS=("${TARBALLS[@]}" "${INSTALL_RELEASE}#install.sh")

    NOTES="claude-trace v${VERSION}

安装：
\`\`\`bash
curl -fsSL ${RELEASE_URL}/install.sh | bash
\`\`\`"

    if gh release view "v${VERSION}" --repo "$GH_REPO" >/dev/null 2>&1; then
        echo "  Release v${VERSION} 已存在，上传/覆盖 assets ..."
        gh release upload "v${VERSION}" "${ASSETS[@]}" --repo "$GH_REPO" --clobber
    else
        echo "  创建 Release v${VERSION} ..."
        gh release create "v${VERSION}" "${ASSETS[@]}" \
            --repo "$GH_REPO" \
            --title "v${VERSION}" \
            --notes "$NOTES"
    fi

    echo ""
    echo "=== 发布完成 ==="
    echo ""
    echo "安装命令："
    echo "  curl -fsSL ${RELEASE_URL}/install.sh | bash"
else
    echo ""
    echo "提示：添加 --upload 参数可发布到 GitHub Releases（需先 gh auth login）"
    echo "  ./build/release.sh --upload"
fi
