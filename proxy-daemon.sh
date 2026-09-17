#!/bin/bash
# proxy-daemon.sh — 自动重启的代理守护进程
# 被 kill 后自动重启，直到收到 SIGTERM 两次或删除 PID 文件
#
# 支持 launchd 环境：所有路径使用绝对路径，不依赖 $PWD

PORT="${PORT:-4000}"
UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
OUTPUT="${OUTPUT:-}"
FORCE_THINKING="${FORCE_THINKING:-1}"
PID_FILE="/tmp/claude-trace-proxy.pid"
LOG_FILE="/tmp/claude-trace-proxy.log"

# launchd 启动时 $0 可能是绝对路径，确保 SCRIPT_DIR 正确解析
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="${OUTPUT:-$SCRIPT_DIR/trajectories}"

# 确保输出目录存在
mkdir -p "$OUTPUT"

echo $$ > "$PID_FILE"

cleanup() {
    echo "$(date '+%H:%M:%S') [DAEMON] 守护进程退出，等待代理收尾…" >> "$LOG_FILE"
    rm -f "$PID_FILE"
    # 转发 SIGTERM 并等它走完优雅退出（导出 traj + 持久化上传队列）。
    #
    # 老实现 kill 完立刻 exit 0，守护进程一死 launchd 就会连带清掉整个进程组，
    # 代理的收尾逻辑被砍在半路 —— 白瞎了 proxy.py 里的 signal handler。
    # 最多等 15 秒，超时才强杀，避免卡住 launchd 的停止流程。
    if [ -n "${CHILD_PID:-}" ]; then
        kill -TERM "$CHILD_PID" 2>/dev/null
        for _ in $(seq 1 150); do
            kill -0 "$CHILD_PID" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            echo "$(date '+%H:%M:%S') [DAEMON] 代理 15 秒未退出，强制终止" >> "$LOG_FILE"
            kill -KILL "$CHILD_PID" 2>/dev/null
        else
            echo "$(date '+%H:%M:%S') [DAEMON] 代理已优雅退出" >> "$LOG_FILE"
        fi
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    echo "$(date '+%H:%M:%S') [DAEMON] 启动代理..." >> "$LOG_FILE"
    python3 "$SCRIPT_DIR/proxy.py" \
        --port "$PORT" \
        --output "$OUTPUT" \
        --upstream "$UPSTREAM" \
        --force-thinking "$FORCE_THINKING" \
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
