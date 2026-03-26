#!/bin/bash
# build/build.sh — 构建 claude-trace-proxy 可执行文件
#
# 用法：
#   ./build/build.sh              # 构建当前架构
#   ./build/build.sh --clean      # 清理后重新构建
#   ./build/build.sh --package    # 构建并打包 tarball

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARCH="$(uname -m)"
VERSION="$(cat "$ROOT/version")"
DIST_DIR="$ROOT/dist"
BUILD_TMP="$ROOT/build/_build"
OUTPUT_DIR="$DIST_DIR/$ARCH"

echo "=== claude-trace 构建 ==="
echo "  版本:   $VERSION"
echo "  架构:   $ARCH"
echo "  输出:   $OUTPUT_DIR"
echo ""

# 清理
if [ "${1:-}" = "--clean" ] || [ "${2:-}" = "--clean" ]; then
    echo "清理旧构建产物..."
    rm -rf "$BUILD_TMP" "$OUTPUT_DIR"
fi

# 检查依赖
if ! command -v python3 &>/dev/null; then
    echo "错误：找不到 python3"
    exit 1
fi

# 确保 PyInstaller 和 aiohttp 已安装
echo "检查 Python 依赖..."
python3 -c "import PyInstaller" 2>/dev/null || {
    echo "安装 PyInstaller..."
    pip3 install pyinstaller
}
python3 -c "import aiohttp" 2>/dev/null || {
    echo "安装 aiohttp..."
    pip3 install "aiohttp>=3.9.0"
}

# 构建
echo ""
echo "开始 PyInstaller 打包..."
mkdir -p "$OUTPUT_DIR" "$BUILD_TMP"

pyinstaller "$SCRIPT_DIR/claude-trace-proxy.spec" \
    --distpath "$OUTPUT_DIR" \
    --workpath "$BUILD_TMP" \
    --noconfirm

# 验证产物
BINARY="$OUTPUT_DIR/claude-trace-proxy"
if [ ! -f "$BINARY" ]; then
    echo "错误：构建失败，未找到 $BINARY"
    exit 1
fi

BINARY_SIZE=$(du -h "$BINARY" | cut -f1)
echo ""
echo "✅ 构建成功"
echo "   二进制: $BINARY"
echo "   大小:   $BINARY_SIZE"

# 快速验证：检查二进制能否启动（打印 help 后退出）
"$BINARY" --help >/dev/null 2>&1 || {
    echo "⚠️  警告：二进制无法执行 --help，可能存在打包问题"
}

# 打包 tarball
if [ "${1:-}" = "--package" ] || [ "${2:-}" = "--package" ]; then
    echo ""
    echo "打包 tarball..."

    TARBALL_NAME="claude-trace-${VERSION}-darwin-${ARCH}.tar.gz"
    STAGING="$BUILD_TMP/staging"
    rm -rf "$STAGING"
    mkdir -p "$STAGING/claude-trace/bin"

    # 复制文件到 staging 目录
    cp "$BINARY"                              "$STAGING/claude-trace/bin/claude-trace-proxy"
    cp "$ROOT/collector.py"                   "$STAGING/claude-trace/collector.py"
    cp "$ROOT/channels.json.example"          "$STAGING/claude-trace/channels.json.example"
    cp "$ROOT/dist/claude-trace"              "$STAGING/claude-trace/claude-trace"
    cp "$ROOT/dist/proxy-daemon.sh"           "$STAGING/claude-trace/proxy-daemon.sh"
    cp "$ROOT/version"                        "$STAGING/claude-trace/version"

    # 确保脚本可执行
    chmod +x "$STAGING/claude-trace/bin/claude-trace-proxy"
    chmod +x "$STAGING/claude-trace/claude-trace"
    chmod +x "$STAGING/claude-trace/proxy-daemon.sh"

    # 打包
    tar -czf "$DIST_DIR/$TARBALL_NAME" -C "$STAGING" claude-trace

    TARBALL_SIZE=$(du -h "$DIST_DIR/$TARBALL_NAME" | cut -f1)
    echo "✅ tarball 已生成"
    echo "   文件: $DIST_DIR/$TARBALL_NAME"
    echo "   大小: $TARBALL_SIZE"

    # 清理 staging
    rm -rf "$STAGING"
fi

echo ""
echo "完成！"
