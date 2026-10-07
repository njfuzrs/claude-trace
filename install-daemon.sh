#!/bin/bash
# install-daemon.sh — 安装/卸载 claude-trace 代理的 launchd 自启动服务
#
# 用法：
#   ./install-daemon.sh install           # 安装并启动服务（不装文件监听）
#   ./install-daemon.sh install --watch   # 同时安装文件监听（开发用，改 .py 自动重启）
#   ./install-daemon.sh uninstall         # 停止并卸载服务
#   ./install-daemon.sh status            # 查看服务状态
#   ./install-daemon.sh restart           # 重启服务
#
# 文件监听默认不装。曾经默认装在同一台生产采集机上，改仓库 .py 会 kickstart
# 正在跑的二进制 —— 重启的是 ~/.claude-trace 里的旧包，还会按当时的 300s
# 超时把活会话切碎。生产二进制模式禁止装它。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-trace.proxy"
WATCH_LABEL="com.claude-trace.watch-reload"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
WATCH_PLIST_PATH="$HOME/Library/LaunchAgents/${WATCH_LABEL}.plist"
DAEMON_SCRIPT="$SCRIPT_DIR/proxy-daemon.sh"
WATCH_SCRIPT="$SCRIPT_DIR/watch-reload.sh"
LOG_FILE="/tmp/claude-trace-proxy.log"
WATCH_LOG="/tmp/claude-trace-watch.log"

# 可通过环境变量覆盖
PORT="${PORT:-4000}"
UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
OUTPUT="${OUTPUT:-$SCRIPT_DIR/trajectories}"
# 已废弃。仍写入 plist 以免旧 daemon 脚本读不到键；代理忽略该值。
FORCE_THINKING="${FORCE_THINKING:-0}"
TRAJ_PLATFORM_URL="${TRAJ_PLATFORM_URL:-}"
TRAJ_UPLOAD_TOKEN="${TRAJ_UPLOAD_TOKEN:-}"
TRAJ_USER_ID="${TRAJ_USER_ID:-}"
TRAJ_DEVICE_ID="${TRAJ_DEVICE_ID:-}"
# 默认 false：不删本地数据。
# 老默认值是 true（上传成功即删本地），后果是「上传成功」的判断一旦有偏差
# 数据就没了第二份 —— 而 409 幂等 bug 恰恰把「服务端拒绝覆盖」也算成了成功。
# 实测 7247 个会话目录只剩一个 .uploaded 标记，本地已无法重建。
TRAJ_CLEANUP_AFTER_UPLOAD="${TRAJ_CLEANUP_AFTER_UPLOAD:-false}"
# 启动补传：扫描盘上未上传的会话并补齐，上传链路的兜底
TRAJ_BACKFILL_ON_START="${TRAJ_BACKFILL_ON_START:-true}"
# 消息体密钥脱敏：默认开启，设为 false 关闭
TRAJ_SCRUB_SECRETS="${TRAJ_SCRUB_SECRETS:-true}"
INSTALL_WATCH=false

# bootout 是异步的：命令返回时 job 可能还在收尾（proxy-daemon.sh 最多等采集器 15 秒，
# plist ExitTimeOut=25）。这时 bootstrap 会报「5: Input/output error」，job 停在未加载状态，
# KeepAlive 管不到未加载的 job —— 服务就这样静默死掉，直到有人手动 bootstrap。
# 所以必须等 job 真正从域里消失，再 bootstrap，且 bootstrap 失败要重试并显式报错。
wait_job_gone() {
    local label="$1"
    for _ in $(seq 1 60); do
        launchctl print "gui/$(id -u)/$label" &>/dev/null || return 0
        sleep 0.5
    done
    return 1
}

bootstrap_job() {
    local plist="$1"
    for _ in 1 2 3 4 5; do
        launchctl bootstrap "gui/$(id -u)" "$plist" && return 0
        sleep 1
    done
    return 1
}

usage() {
    echo "用法: $0 {install [--watch]|uninstall|status|restart}"
    echo ""
    echo "  install            安装 launchd 服务（开机自启 + 崩溃重启）"
    echo "  install --watch    同时安装文件监听（开发用，改 .py 自动重启）"
    echo "  uninstall          停止并卸载服务"
    echo "  status             查看服务运行状态"
    echo "  restart            重启服务"
    echo ""
    echo "环境变量："
    echo "  PORT=$PORT"
    echo "  UPSTREAM=$UPSTREAM"
    echo "  OUTPUT=$OUTPUT"
    echo "  TRAJ_PLATFORM_URL=$TRAJ_PLATFORM_URL"
    echo "  TRAJ_UPLOAD_TOKEN=${TRAJ_UPLOAD_TOKEN:+***已设置***}"
    exit 1
}

install_watch() {
    # 安装文件监听服务（自动重启代理）
    if ! command -v fswatch &>/dev/null; then
        echo "⚠️  fswatch 未安装，跳过文件监听服务（brew install fswatch）"
        return
    fi

    # 停止已有的监听服务
    launchctl bootout "gui/$(id -u)/$WATCH_LABEL" 2>/dev/null || true
    wait_job_gone "$WATCH_LABEL" || true
    rm -rf /tmp/claude-trace-watch.lock

    cat > "$WATCH_PLIST_PATH" <<WPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${WATCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${WATCH_SCRIPT}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${WATCH_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${WATCH_LOG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
WPLIST

    if ! bootstrap_job "$WATCH_PLIST_PATH"; then
        echo "⚠️  文件监听服务加载失败（不影响代理本身）：launchctl bootstrap gui/$(id -u) ${WATCH_PLIST_PATH}"
        return
    fi
    echo "✅ 文件监听服务已安装（修改 .py 自动重启代理）"
}

uninstall_watch() {
    launchctl bootout "gui/$(id -u)/$WATCH_LABEL" 2>/dev/null || true
    pkill -f "watch-reload.sh" 2>/dev/null || true
    rm -rf /tmp/claude-trace-watch.lock
    rm -f "$WATCH_PLIST_PATH"
    echo "✅ 文件监听服务已卸载"
}

do_install() {
    # 确保目录存在
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$OUTPUT"

    # 如果已安装，先卸载
    if launchctl list "$LABEL" &>/dev/null; then
        echo "检测到已有服务，先卸载..."
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        wait_job_gone "$LABEL" || echo "⚠️  旧服务 30 秒内未退出，仍尝试加载"
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
        <key>TRAJ_PLATFORM_URL</key>
        <string>${TRAJ_PLATFORM_URL}</string>
        <key>TRAJ_UPLOAD_TOKEN</key>
        <string>${TRAJ_UPLOAD_TOKEN}</string>
        <key>TRAJ_USER_ID</key>
        <string>${TRAJ_USER_ID}</string>
        <key>TRAJ_DEVICE_ID</key>
        <string>${TRAJ_DEVICE_ID}</string>
        <key>TRAJ_CLEANUP_AFTER_UPLOAD</key>
        <string>${TRAJ_CLEANUP_AFTER_UPLOAD}</string>
        <key>TRAJ_BACKFILL_ON_START</key>
        <string>${TRAJ_BACKFILL_ON_START}</string>
        <key>TRAJ_SCRUB_SECRETS</key>
        <string>${TRAJ_SCRUB_SECRETS}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>25</integer>
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
    if ! bootstrap_job "$PLIST_PATH"; then
        echo "❌ 服务加载失败，代理没有运行，Claude Code 请求会 403。手动执行："
        echo "   launchctl bootstrap gui/$(id -u) ${PLIST_PATH}"
        exit 1
    fi

    echo "✅ 服务已安装并启动"
    echo "   plist: $PLIST_PATH"
    echo "   日志:  $LOG_FILE"
    echo "   端口:  $PORT"
    echo "   上游:  $UPSTREAM"
    echo ""
    echo "验证: curl -s http://127.0.0.1:$PORT/_internal/health"

    # 文件监听只在显式 --watch 时安装。默认路径曾经顺手装上，
    # 生产二进制模式就会被仓库 .py 的保存触发 kickstart，重启的是旧包。
    if [ "$INSTALL_WATCH" = true ]; then
        install_watch
    else
        echo "ℹ️  未安装文件监听（开发时加 --watch：./install-daemon.sh install --watch）"
    fi
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

    # 卸载文件监听服务
    uninstall_watch

    echo "✅ 卸载完成"
}

do_status() {
    echo "=== launchd 服务状态 ==="
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl list "$LABEL"
        echo ""
        echo "状态: 运行中"
    elif [ -f "$PLIST_PATH" ]; then
        # 已安装但 job 不在 launchd 里：KeepAlive 管不到，不会自己恢复
        echo "状态: ❌ 已安装但服务未加载 —— 代理没有运行，Claude Code 请求会 403，且不会自动恢复"
        echo "恢复: $0 restart"
    else
        echo "状态: 未安装"
    fi

    echo ""
    echo "=== 文件监听服务 ==="
    if launchctl list "$WATCH_LABEL" &>/dev/null; then
        echo "状态: 运行中"
        echo "日志: $WATCH_LOG"
    elif pgrep -f "watch-reload.sh" &>/dev/null; then
        echo "状态: 运行中（非 launchd）"
    else
        echo "状态: 未运行"
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
    if ! launchctl list "$LABEL" &>/dev/null; then
        # plist 还在、只是 job 没加载（bootstrap 失败留下的状态）：直接加载现有 plist。
        # 不能走重装 —— 它会用当前 shell 的环境变量重写 plist，
        # 把 UPSTREAM / 上传配置悄悄重置成默认值。
        if [ -f "$PLIST_PATH" ]; then
            echo "服务未加载，加载现有 ${PLIST_PATH} ..."
            if ! bootstrap_job "$PLIST_PATH"; then
                echo "❌ 加载失败，代理没有运行，Claude Code 请求会 403。"
                exit 1
            fi
            echo "✅ 服务已加载"
            return
        fi
        echo "服务未安装，执行 install..."
        do_install
        return
    fi

    # bootout + bootstrap，不是 kickstart。
    #
    # kickstart -k 只重启进程、不重读 plist：改完 plist 里的 env（上游、上传开关）
    # 执行 restart，会看到「已重启」但跑的还是旧配置。watch-reload.sh 也走这条路径，
    # 所以源码改动后的自动重启同样吃不到 plist 变更。
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    # 端口释放早于 job 退出（采集器先停监听、再做 15 秒收尾），只等端口不够。
    wait_job_gone "$LABEL" || echo "⚠️  旧服务 30 秒内未退出，仍尝试加载"

    # 等端口释放再拉起，否则新实例撞 EADDRINUSE，被 KeepAlive 反复重启，
    # 表面上服务在跑，实际一直起不来。
    local pid=""
    for _ in $(seq 1 50); do
        pid=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
        [ -z "$pid" ] && break
        sleep 0.2
    done
    if [ -n "$pid" ]; then
        echo "⚠️  端口 $PORT 仍被占用 (PID: $pid)，终止残留进程"
        kill "$pid" 2>/dev/null || true
        sleep 1
    fi

    if [ -f "$PLIST_PATH" ]; then
        if ! bootstrap_job "$PLIST_PATH"; then
            echo "❌ 重启失败：服务已停但未能重新加载，代理没有运行，Claude Code 请求会 403。手动执行："
            echo "   launchctl bootstrap gui/$(id -u) ${PLIST_PATH}"
            exit 1
        fi
        echo "✅ 服务已重启（已重新加载 ${PLIST_PATH}）"
    else
        echo "未找到 $PLIST_PATH，执行 install 重建配置..."
        do_install
    fi
}

case "${1:-}" in
    install)
        shift
        for arg in "$@"; do
            case "$arg" in
                --watch) INSTALL_WATCH=true ;;
                *)
                    echo "未知参数: $arg"
                    usage
                    ;;
            esac
        done
        do_install
        ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
    restart)   do_restart ;;
    *)         usage ;;
esac
