#!/bin/bash
# install-codex-daemon.sh — 安装/卸载 Codex watcher 的 launchd 自启动服务

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-trace.codex-watch"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
DAEMON_SCRIPT="$SCRIPT_DIR/codex-daemon.sh"
LOG_FILE="/tmp/claude-trace-codex-watch.log"

OUTPUT="${OUTPUT:-$SCRIPT_DIR/trajectories/sessions}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_CMD="${CODEX_CMD:-codex}"
WATCH_STATE_FILE="${WATCH_STATE_FILE:-$OUTPUT/.codex_watch_state.json}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"
DEBOUNCE_SEC="${DEBOUNCE_SEC:-2}"
FINALIZE_SEC="${FINALIZE_SEC:-8}"
RETRY_SEC="${RETRY_SEC:-10}"

usage() {
    echo "用法: $0 {install|uninstall|status|restart}"
    echo ""
    echo "环境变量："
    echo "  OUTPUT=$OUTPUT"
    echo "  CODEX_HOME=$CODEX_HOME"
    echo "  CODEX_CMD=$CODEX_CMD"
    echo "  WATCH_STATE_FILE=$WATCH_STATE_FILE"
    echo "  POLL_INTERVAL=$POLL_INTERVAL"
    echo "  DEBOUNCE_SEC=$DEBOUNCE_SEC"
    echo "  FINALIZE_SEC=$FINALIZE_SEC"
    echo "  RETRY_SEC=$RETRY_SEC"
    exit 1
}

do_install() {
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$OUTPUT"

    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${DAEMON_SCRIPT}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OUTPUT</key>
        <string>${OUTPUT}</string>
        <key>CODEX_HOME</key>
        <string>${CODEX_HOME}</string>
        <key>CODEX_CMD</key>
        <string>${CODEX_CMD}</string>
        <key>WATCH_STATE_FILE</key>
        <string>${WATCH_STATE_FILE}</string>
        <key>POLL_INTERVAL</key>
        <string>${POLL_INTERVAL}</string>
        <key>DEBOUNCE_SEC</key>
        <string>${DEBOUNCE_SEC}</string>
        <key>FINALIZE_SEC</key>
        <string>${FINALIZE_SEC}</string>
        <key>RETRY_SEC</key>
        <string>${RETRY_SEC}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
</dict>
</plist>
PLIST

    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
    echo "✅ Codex watcher 服务已安装并启动"
    echo "   plist: $PLIST_PATH"
    echo "   日志:  $LOG_FILE"
    echo "   输出:  $OUTPUT"
}

do_uninstall() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    rm -f /tmp/claude-trace-codex-watch.pid
    pkill -f "codex-daemon.sh" 2>/dev/null || true
    echo "✅ Codex watcher 服务已卸载"
}

do_status() {
    echo "=== Codex watcher launchd 状态 ==="
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl list "$LABEL"
        echo ""
        echo "状态: 运行中"
    else
        echo "状态: 未安装或未运行"
    fi
    echo "日志: $LOG_FILE"
}

do_restart() {
    do_uninstall
    do_install
}

case "${1:-}" in
    install)
        do_install
        ;;
    uninstall)
        do_uninstall
        ;;
    status)
        do_status
        ;;
    restart)
        do_restart
        ;;
    *)
        usage
        ;;
esac
