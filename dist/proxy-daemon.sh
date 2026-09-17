#!/bin/bash
# proxy-daemon.sh — 自动重启的统一采集守护进程（安装版）
#
# 与开发版的区别：
#   - 调用 PyInstaller 打包的统一采集二进制而非 python3 脚本
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
    echo "$(date '+%H:%M:%S') [DAEMON] 守护进程退出，等待采集器收尾…" >> "$LOG_FILE"
    rm -f "$PID_FILE"
    # 转发 SIGTERM 并等它走完优雅退出（导出 traj + 持久化上传队列）。
    # 老实现 kill 完立刻 exit 0，launchd 随即清掉整个进程组，
    # 收尾逻辑被砍在半路。最多等 15 秒再强杀。
    if [ -n "${CHILD_PID:-}" ]; then
        kill -TERM "$CHILD_PID" 2>/dev/null
        for _ in $(seq 1 150); do
            kill -0 "$CHILD_PID" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            echo "$(date '+%H:%M:%S') [DAEMON] 采集器 15 秒未退出，强制终止" >> "$LOG_FILE"
            kill -KILL "$CHILD_PID" 2>/dev/null
        else
            echo "$(date '+%H:%M:%S') [DAEMON] 采集器已优雅退出" >> "$LOG_FILE"
        fi
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    echo "$(date '+%H:%M:%S') [DAEMON] 启动统一采集器（Claude + Codex）..." >> "$LOG_FILE"

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

    echo "$(date '+%H:%M:%S') [DAEMON] 统一采集器退出 (code=$EXIT_CODE)，2 秒后重启..." >> "$LOG_FILE"
    sleep 2
done
