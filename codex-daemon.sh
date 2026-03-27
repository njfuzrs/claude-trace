#!/bin/bash
# codex-daemon.sh — 自动重启的 Codex rollout watcher 守护进程

POLL_INTERVAL="${POLL_INTERVAL:-2}"
DEBOUNCE_SEC="${DEBOUNCE_SEC:-2}"
FINALIZE_SEC="${FINALIZE_SEC:-8}"
RETRY_SEC="${RETRY_SEC:-10}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_CMD="${CODEX_CMD:-codex}"
WATCH_STATE_FILE="${WATCH_STATE_FILE:-}"
PID_FILE="/tmp/claude-trace-codex-watch.pid"
LOG_FILE="/tmp/claude-trace-codex-watch.log"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="${OUTPUT:-$SCRIPT_DIR/trajectories/sessions}"

mkdir -p "$OUTPUT"
echo $$ > "$PID_FILE"

cleanup() {
    echo "$(date '+%H:%M:%S') [CODEX-DAEMON] 守护进程退出" >> "$LOG_FILE"
    rm -f "$PID_FILE"
    kill "$CHILD_PID" 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    echo "$(date '+%H:%M:%S') [CODEX-DAEMON] 启动 watcher..." >> "$LOG_FILE"

    cmd=(
        python3
        "$SCRIPT_DIR/import_codex.py"
        --watch
        --codex-home "$CODEX_HOME"
        --output "$OUTPUT"
        --codex-cmd "$CODEX_CMD"
        --poll-interval "$POLL_INTERVAL"
        --debounce-sec "$DEBOUNCE_SEC"
        --finalize-sec "$FINALIZE_SEC"
        --retry-sec "$RETRY_SEC"
    )
    if [ -n "$WATCH_STATE_FILE" ]; then
        cmd+=(--state-file "$WATCH_STATE_FILE")
    fi

    "${cmd[@]}" >> "$LOG_FILE" 2>&1 &
    CHILD_PID=$!

    wait "$CHILD_PID"
    EXIT_CODE=$?

    if [ ! -f "$PID_FILE" ]; then
        echo "$(date '+%H:%M:%S') [CODEX-DAEMON] PID 文件已删除，彻底退出" >> "$LOG_FILE"
        exit 0
    fi

    echo "$(date '+%H:%M:%S') [CODEX-DAEMON] watcher 退出 (code=$EXIT_CODE)，2 秒后重启..." >> "$LOG_FILE"
    sleep 2
done
