#!/bin/bash
# install-daemon.sh — 安装/卸载 claude-trace 代理的 launchd 自启动服务
#
# 用法：
#   ./install-daemon.sh install    # 安装并启动服务
#   ./install-daemon.sh uninstall  # 停止并卸载服务
#   ./install-daemon.sh status     # 查看服务状态
#   ./install-daemon.sh restart    # 重启服务

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-trace.proxy"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
DAEMON_SCRIPT="$SCRIPT_DIR/proxy-daemon.sh"
LOG_FILE="/tmp/claude-trace-proxy.log"

# 可通过环境变量覆盖
PORT="${PORT:-4000}"
UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
OUTPUT="${OUTPUT:-$SCRIPT_DIR/trajectories}"
FORCE_THINKING="${FORCE_THINKING:-0}"

usage() {
    echo "用法: $0 {install|uninstall|status|restart}"
    echo ""
    echo "  install    安装 launchd 服务（开机自启 + 崩溃重启）"
    echo "  uninstall  停止并卸载服务"
    echo "  status     查看服务运行状态"
    echo "  restart    重启服务"
    echo ""
    echo "环境变量："
    echo "  PORT=$PORT"
    echo "  UPSTREAM=$UPSTREAM"
    echo "  OUTPUT=$OUTPUT"
    echo "  FORCE_THINKING=$FORCE_THINKING"
    exit 1
}

do_install() {
    # 确保目录存在
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$OUTPUT"

    # 如果已安装，先卸载
    if launchctl list "$LABEL" &>/dev/null; then
        echo "检测到已有服务，先卸载..."
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    fi

    # 查找 python3 绝对路径
    PYTHON3="$(which python3)"
    if [ -z "$PYTHON3" ]; then
        echo "错误：找不到 python3"
        exit 1
    fi

    # 生成 plist
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
        <key>PORT</key>
        <string>${PORT}</string>
        <key>UPSTREAM</key>
        <string>${UPSTREAM}</string>
        <key>OUTPUT</key>
        <string>${OUTPUT}</string>
        <key>FORCE_THINKING</key>
        <string>${FORCE_THINKING}</string>
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

    # 加载服务
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

    echo "✅ 服务已安装并启动"
    echo "   plist: $PLIST_PATH"
    echo "   日志:  $LOG_FILE"
    echo "   端口:  $PORT"
    echo "   上游:  $UPSTREAM"
    echo ""
    echo "验证: curl -s http://127.0.0.1:$PORT/_internal/health"
}

do_uninstall() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        echo "✅ 服务已停止"
    else
        echo "服务未运行"
    fi

    if [ -f "$PLIST_PATH" ]; then
        rm -f "$PLIST_PATH"
        echo "✅ plist 已删除: $PLIST_PATH"
    fi

    # 清理 PID 文件
    rm -f /tmp/claude-trace-proxy.pid

    # 杀掉残留进程
    kill $(lsof -ti :$PORT) 2>/dev/null || true
    echo "✅ 卸载完成"
}

do_status() {
    echo "=== launchd 服务状态 ==="
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl list "$LABEL"
        echo ""
        echo "状态: 运行中"
    else
        echo "状态: 未安装或未运行"
    fi

    echo ""
    echo "=== 端口 $PORT ==="
    if lsof -i :$PORT &>/dev/null; then
        lsof -i :$PORT
    else
        echo "无进程监听"
    fi

    echo ""
    echo "=== 健康检查 ==="
    curl -s "http://127.0.0.1:$PORT/_internal/health" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "代理未响应"

    echo ""
    echo "=== 最近日志 ==="
    if [ -f "$LOG_FILE" ]; then
        tail -5 "$LOG_FILE"
    else
        echo "无日志文件"
    fi
}

do_restart() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
        echo "✅ 服务已重启"
    else
        echo "服务未安装，执行 install..."
        do_install
    fi
}

case "${1:-}" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
    restart)   do_restart ;;
    *)         usage ;;
esac
