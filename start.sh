#!/bin/bash
# start.sh — 一键启动 claude-trace（代理 + Hooks 配置 + Claude Code）
#
# 用法：
#   ./start.sh                    # 默认端口 4000
#   ./start.sh --port 5000        # 自定义端口
#   ./start.sh --proxy-only       # 仅启动代理，不启动 Claude Code

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-4000}"
OUTPUT="${OUTPUT:-./trajectories}"
UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
PROXY_ONLY=false
PROXY_PID=""

# 信号处理：确保代理进程被清理
cleanup() {
    if [ -n "$PROXY_PID" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
        echo ""
        echo "停止代理进程 (PID: $PROXY_PID)..."
        kill "$PROXY_PID" 2>/dev/null
        wait "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 解析参数（-- 之后的参数传递给 claude）
CLAUDE_ARGS=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --upstream) UPSTREAM="$2"; shift 2 ;;
        --proxy-only) PROXY_ONLY=true; shift ;;
        --) shift; CLAUDE_ARGS=("$@"); break ;;
        *) echo "未知参数: $1 (用 -- 分隔传给 claude 的参数)"; exit 1 ;;
    esac
done

echo "=================================================="
echo "  claude-trace 一键启动"
echo "=================================================="

# 1. 检查 Python 依赖
if ! python3 -c "import aiohttp" 2>/dev/null; then
    echo "缺少依赖 aiohttp，请先安装："
    echo "  pip3 install aiohttp"
    echo "  # 或使用 venv："
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install aiohttp"
    exit 1
fi

# 2. 部署 Hooks 采集脚本
HOOKS_DIR="$HOME/.claude/hooks"
mkdir -p "$HOOKS_DIR"
cp "$SCRIPT_DIR/collector.py" "$HOOKS_DIR/collector.py"
echo "✅ collector.py 已部署到 $HOOKS_DIR/"

# 3. 配置 Hooks（写入 settings.json）
python3 "$SCRIPT_DIR/setup_hooks.py" --collector "$HOOKS_DIR/collector.py" || {
    echo "⚠️  Hooks 配置失败，将以仅代理模式运行"
}
echo "✅ Hooks 已配置"

# 4. 启动代理（后台）
echo ""
echo "启动代理: http://127.0.0.1:$PORT"
echo "输出目录: $OUTPUT"
echo ""

export CLAUDE_PROXY_PORT="$PORT"

if [ "$PROXY_ONLY" = true ]; then
    # 前台运行代理
    exec python3 "$SCRIPT_DIR/proxy.py" --port "$PORT" --output "$OUTPUT" --upstream "$UPSTREAM"
else
    # 后台运行代理
    python3 "$SCRIPT_DIR/proxy.py" --port "$PORT" --output "$OUTPUT" --upstream "$UPSTREAM" &
    PROXY_PID=$!
    echo "代理 PID: $PROXY_PID"

    # 等待代理就绪
    sleep 1
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        echo "❌ 代理启动失败"
        exit 1
    fi

    # 5. 启动 Claude Code
    echo ""
    echo "启动 Claude Code..."
    echo "=================================================="
    ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" claude ${CLAUDE_ARGS[@]+"${CLAUDE_ARGS[@]}"}

    # Claude Code 退出后，trap EXIT 会自动清理代理进程
    echo ""
    echo "✅ 轨迹数据保存在: $OUTPUT"
fi
