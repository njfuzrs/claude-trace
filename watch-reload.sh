#!/usr/bin/env bash
# watch-reload.sh — 监听核心 Python 文件变更，自动重启代理
#
# 使用 fswatch 监听 trace_agent.py / proxy.py / uploader.py / builder.py / collector.py，
# 文件保存后自动执行 install-daemon.sh restart。
# 防抖：2 秒内的多次变更只触发一次重启。

set -euo pipefail

LOCKDIR="/tmp/claude-trace-watch.lock"
LOG="/tmp/claude-trace-watch.log"

# mkdir 原子锁：单实例保护（macOS 无 flock）
cleanup() { rm -rf "$LOCKDIR"; }
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    # 检查持锁进程是否还活着
    old_pid=$(cat "$LOCKDIR/pid" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "$(date '+%H:%M:%S') [WATCH] 已有实例运行 (pid=$old_pid)，退出" >> "$LOG"
        exit 0
    fi
    # 持锁进程已死，清理残留锁
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR"
fi
echo $$ > "$LOCKDIR/pid"
trap cleanup EXIT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

WATCH_FILES=(
    "$SCRIPT_DIR/trace_agent.py"
    "$SCRIPT_DIR/proxy.py"
    "$SCRIPT_DIR/uploader.py"
    "$SCRIPT_DIR/builder.py"
    "$SCRIPT_DIR/collector.py"
    "$SCRIPT_DIR/version_info.py"
)

log() {
    echo "$(date '+%H:%M:%S') [WATCH] $*" >> "$LOG"
}

log "启动文件监听 (pid=$$): ${WATCH_FILES[*]}"

while true; do
    changed=$(fswatch -1 --latency 2 --event Updated "${WATCH_FILES[@]}" 2>/dev/null)
    if [ -n "$changed" ]; then
        filename=$(basename "$changed")
        log "检测到变更: $filename → 重启代理..."
        if "$SCRIPT_DIR/install-daemon.sh" restart >> "$LOG" 2>&1; then
            log "代理已自动重启 ✓"
        else
            log "代理重启失败 ✗"
        fi
    fi
done
