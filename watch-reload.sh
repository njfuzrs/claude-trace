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

# 守卫：只在「launchd 跑的就是本仓库源码」时才允许自动重启。
# 生产二进制模式（plist 指向 ~/.claude-trace/proxy-daemon.sh）下，改仓库 .py
# 根本不会进运行中的二进制，重启只会白白打断采集 —— v0.3.1 时这个监听服务
# 从 3 月一直挂着，开发 builder.py 每保存一次就重启一次生产代理。
PROXY_PLIST="$HOME/Library/LaunchAgents/com.claude-trace.proxy.plist"
if ! grep -qF "$SCRIPT_DIR/proxy-daemon.sh" "$PROXY_PLIST" 2>/dev/null; then
    log "代理服务不是本仓库源码模式（$PROXY_PLIST 未指向 $SCRIPT_DIR/proxy-daemon.sh），不监听，退出"
    exit 0
fi

log "启动文件监听 (pid=$$): ${WATCH_FILES[*]}"

while true; do
    changed=$(fswatch -1 --latency 2 --event Updated "${WATCH_FILES[@]}" 2>/dev/null)
    if [ -n "$changed" ]; then
        filename=$(basename "$changed")
        # 启动后才装了生产包（plist 被改写）的情况：每次重启前再核一次
        if ! grep -qF "$SCRIPT_DIR/proxy-daemon.sh" "$PROXY_PLIST" 2>/dev/null; then
            log "检测到变更: $filename，但代理已切到生产二进制模式，不重启，监听退出"
            exit 0
        fi
        log "检测到变更: $filename → 重启代理..."
        if "$SCRIPT_DIR/install-daemon.sh" restart >> "$LOG" 2>&1; then
            log "代理已自动重启 ✓"
        else
            log "代理重启失败 ✗"
        fi
    fi
done
