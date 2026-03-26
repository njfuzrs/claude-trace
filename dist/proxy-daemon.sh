#!/bin/bash
# proxy-daemon.sh — 自动重启的代理守护进程（安装版）
#
# 与开发版的区别：
#   - 调用 PyInstaller 打包的二进制而非 python3 proxy.py
#   - 所有路径基于 INSTALL_DIR，不依赖 cwd

INSTALL_DIR="$HOME/.claude-trace"
PORT="${PORT:-4000}"
UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
OUTPUT="${OUTPUT:-$INSTALL_DIR/trajectories}"
FORCE_THINKING="${FORCE_THINKING:-0}"
PID_FILE="/tmp/claude-trace-proxy.pid"
LOG_FILE="/tmp/claude-trace-proxy.log"
BINARY="$INSTALL_DIR/bin/claude-trace-proxy"

# 确保输出目录存在
mkdir -p "$OUTPUT"

echo $$ > "$PID_FILE"

cleanup() {
    echo "$(date '+%H:%M:%S') [DAEMON] 守护进程退出" >> "$LOG_FILE"
    rm -f "$PID_FILE"
    kill $CHILD_PID 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    echo "$(date '+%H:%M:%S') [DAEMON] 启动代理..." >> "$LOG_FILE"

    if [ ! -x "$BINARY" ]; then
        echo "$(date '+%H:%M:%S') [DAEMON] 错误：找不到可执行文件 $BINARY" >> "$LOG_FILE"
        sleep 10
        continue
    fi

    echo "$(date '+%H:%M:%S') [DAEMON] 二进制: $BINARY (首次启动约需 10 秒)" >> "$LOG_FILE"

    "$BINARY" \
        --port "$PORT" \
        --output "$OUTPUT" \
        --upstream "$UPSTREAM" \
        --force-thinking "$FORCE_THINKING" \
        --verbose >> "$LOG_FILE" 2>&1 &
    CHILD_PID=$!

    wait $CHILD_PID
    EXIT_CODE=$?

    # PID 文件被删除表示要彻底停止
    if [ ! -f "$PID_FILE" ]; then
        echo "$(date '+%H:%M:%S') [DAEMON] PID 文件已删除，彻底退出" >> "$LOG_FILE"
        exit 0
    fi

    echo "$(date '+%H:%M:%S') [DAEMON] 代理退出 (code=$EXIT_CODE)，2 秒后重启..." >> "$LOG_FILE"
    sleep 2
done
