#!/bin/bash
# build/release.sh — 构建并打包发布
#
# 用法：
#   ./build/release.sh                    # 构建当前架构并打包
#   ./build/release.sh --upload           # 构建并上传到 GitLab Release
#   ./build/release.sh --cross            # 尝试交叉编译双架构（需要 Rosetta）
#
# 环境变量：
#   GITLAB_TOKEN    — GitLab API Token（--upload 时需要）
#   GITLAB_URL      — GitLab 地址（默认 https://gitlab.example.com）
#   GITLAB_PROJECT  — 项目路径（默认 zhourusheng/claude-trace）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="$(cat "$ROOT/version")"
ARCH="$(uname -m)"
DIST_DIR="$ROOT/dist"

GITLAB_URL="${GITLAB_URL:-https://gitlab.example.com}"
GITLAB_PROJECT="${GITLAB_PROJECT:-zhourusheng/claude-trace}"
GITLAB_TOKEN="${GITLAB_TOKEN:-}"

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

# 复制 install.sh 并填入 RELEASE_BASE
INSTALL_RELEASE="$DIST_DIR/install.sh"
cp "$ROOT/dist/install.sh" "$INSTALL_RELEASE"

# 如果有 GitLab 配置，填入下载地址
if [ -n "$GITLAB_URL" ] && [ -n "$GITLAB_PROJECT" ]; then
    ENCODED_PROJECT=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$GITLAB_PROJECT', safe=''))")
    RELEASE_URL="${GITLAB_URL}/api/v4/projects/${ENCODED_PROJECT}/packages/generic/claude-trace/${VERSION}"

    # 替换 RELEASE_BASE 默认值
    sed -i '' "s|^RELEASE_BASE=\"\${RELEASE_BASE:-}\"|RELEASE_BASE=\"\${RELEASE_BASE:-${RELEASE_URL}}\"|" "$INSTALL_RELEASE"
    echo "  下载地址: $RELEASE_URL"
fi

echo "  install.sh: $INSTALL_RELEASE"

# ─── 汇总 ───

echo ""
echo "=== 发布产物 ==="
for f in "${TARBALLS[@]}"; do
    SIZE=$(du -h "$f" | cut -f1)
    echo "  $f ($SIZE)"
done
echo "  $INSTALL_RELEASE"

# ─── 上传到 GitLab（可选） ───

if [ "$DO_UPLOAD" = true ]; then
    echo ""
    echo ">>> 上传到 GitLab ..."

    if [ -z "$GITLAB_TOKEN" ]; then
        echo "错误：需要设置 GITLAB_TOKEN 环境变量"
        exit 1
    fi

    ENCODED_PROJECT=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$GITLAB_PROJECT', safe=''))")
    PACKAGE_URL="${GITLAB_URL}/api/v4/projects/${ENCODED_PROJECT}/packages/generic/claude-trace/${VERSION}"

    # 上传 tarballs
    for tarball in "${TARBALLS[@]}"; do
        FILENAME="$(basename "$tarball")"
        echo "  上传 $FILENAME ..."
        curl --fail --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
            --upload-file "$tarball" \
            "${PACKAGE_URL}/${FILENAME}"
        echo "  ✅ $FILENAME"
    done

    # 上传 install.sh
    echo "  上传 install.sh ..."
    curl --fail --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
        --upload-file "$INSTALL_RELEASE" \
        "${PACKAGE_URL}/install.sh"
    echo "  ✅ install.sh"

    echo ""
    echo "=== 上传完成 ==="
    echo ""
    echo "安装命令："
    echo "  curl -fsSL ${PACKAGE_URL}/install.sh | bash"

    # 创建 GitLab Release（可选）
    echo ""
    echo "创建 Release tag v${VERSION} ..."
    curl --fail --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
        --header "Content-Type: application/json" \
        --data "{
            \"name\": \"v${VERSION}\",
            \"tag_name\": \"v${VERSION}\",
            \"description\": \"claude-trace v${VERSION}\n\n安装：\n\`\`\`bash\ncurl -fsSL ${PACKAGE_URL}/install.sh | bash\n\`\`\`\",
            \"assets\": {
                \"links\": [
                    {\"name\": \"install.sh\", \"url\": \"${PACKAGE_URL}/install.sh\"},
                    $(printf '{\"name\": \"%s\", \"url\": \"%s/%s\"}' "$(basename "${TARBALLS[0]}")" "$PACKAGE_URL" "$(basename "${TARBALLS[0]}")")
                ]
            }
        }" \
        "${GITLAB_URL}/api/v4/projects/${ENCODED_PROJECT}/releases" 2>/dev/null || {
            echo "  ⚠️  Release 创建失败（可能已存在），请手动创建"
        }

    echo ""
    echo "✅ 发布完成！"
else
    echo ""
    echo "提示：添加 --upload 参数可自动上传到 GitLab"
    echo "  GITLAB_TOKEN=<token> ./build/release.sh --upload"
fi
