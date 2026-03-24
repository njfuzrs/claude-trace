#!/bin/bash
# proxy-daemon.sh — 自动重启的代理守护进程
# 被 kill 后自动重启，直到收到 SIGTERM 两次或删除 PID 文件

PORT="${PORT:-4000}"
UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
OUTPUT="${OUTPUT:-}"
PID_FILE="/tmp/claude-trace-proxy.pid"
LOG_FILE="/tmp/claude-trace-proxy.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="${OUTPUT:-$SCRIPT_DIR/trajectories}"

echo $$ > "$PID_FILE"

cleanup() {
    echo "$(date '+%H:%M:%S') [DAEMON] 守护进程退出" >> "$LOG_FILE"
    rm -f "$PID_FILE"
    # 杀掉子进程
    kill $CHILD_PID 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    echo "$(date '+%H:%M:%S') [DAEMON] 启动代理..." >> "$LOG_FILE"
    python3 "$SCRIPT_DIR/proxy.py" \
        --port "$PORT" \
        --output "$OUTPUT" \
        --upstream "$UPSTREAM" \
        --verbose >> "$LOG_FILE" 2>&1 &
    CHILD_PID=$!

    # 等待子进程结束
    wait $CHILD_PID
    EXIT_CODE=$?

    # 检查 PID 文件是否还在（手动删除表示要彻底停止）
    if [ ! -f "$PID_FILE" ]; then
        echo "$(date '+%H:%M:%S') [DAEMON] PID 文件已删除，彻底退出" >> "$LOG_FILE"
        exit 0
    fi

    echo "$(date '+%H:%M:%S') [DAEMON] 代理退出 (code=$EXIT_CODE)，2 秒后重启..." >> "$LOG_FILE"
    sleep 2
done
