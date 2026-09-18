#!/bin/bash
# switch-channel.sh — 快速切换 API 渠道
#
# 渠道配置在 channels.json 中维护，无需修改本脚本。
#
# 用法：
#   ./switch-channel.sh company   # 切换到指定渠道
#   ./switch-channel.sh monthly
#   ./switch-channel.sh status    # 查看当前渠道
#   ./switch-channel.sh list      # 列出所有可用渠道

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/channels.json"
SETTINGS="$HOME/.claude/settings.json"
PLIST="$HOME/Library/LaunchAgents/com.claude-trace.proxy.plist"
LABEL="com.claude-trace.proxy"
PORT="${PORT:-4000}"

# 检查配置文件
if [ ! -f "$CONFIG" ]; then
    echo "错误：找不到渠道配置文件 $CONFIG"
    echo "请创建 channels.json，格式参考 channels.json.example"
    exit 1
fi

usage() {
    local channels
    channels=$(python3 -c "
import json
d = json.load(open('$CONFIG'))
keys = list(d.get('channels', {}).keys())
print(' | '.join(keys))
" 2>/dev/null || echo "?")
    echo "用法: $0 {$channels | status | list}"
    exit 1
}

do_list() {
    python3 - "$CONFIG" <<'PYEOF'
import json, sys

config = json.load(open(sys.argv[1]))
channels = config.get("channels", {})
print("可用渠道：")
for key, ch in channels.items():
    upstream = ch.get("upstream", "?")
    name = ch.get("name", key)
    token = ch.get("token", "")
    token_preview = token[:12] + "…" if token else "?"
    print(f"  {key:<12} {name}  ({upstream})")
    print(f"             token={token_preview}")
PYEOF
}

do_status() {
    echo "=== 当前渠道配置 ==="

    TOKEN=$(python3 -c "import json; d=json.load(open('$SETTINGS')); print(d['env'].get('ANTHROPIC_AUTH_TOKEN','?'))" 2>/dev/null)
    BASE_URL=$(python3 -c "import json; d=json.load(open('$SETTINGS')); print(d['env'].get('ANTHROPIC_BASE_URL','?'))" 2>/dev/null)
    echo "  token    : ${TOKEN:0:12}…"
    echo "  base_url : $BASE_URL"

    if [ -f "$PLIST" ]; then
        UPSTREAM=$(python3 -c "
import plistlib
with open('$PLIST','rb') as f: d=plistlib.load(f)
print(d.get('EnvironmentVariables',{}).get('UPSTREAM','?'))
" 2>/dev/null)
        echo "  upstream : $UPSTREAM"
    fi

    # 匹配渠道名称
    echo ""
    python3 - "$CONFIG" "$UPSTREAM" <<'PYEOF'
import json, sys

config = json.load(open(sys.argv[1]))
upstream = sys.argv[2] if len(sys.argv) > 2 else ""
channels = config.get("channels", {})
matched = None
for key, ch in channels.items():
    if ch.get("upstream", "") == upstream:
        matched = (key, ch.get("name", key))
        break
if matched:
    print(f"当前渠道: {matched[0]} — {matched[1]}")
else:
    print("当前渠道: 未知（不在 channels.json 中）")
PYEOF
}

do_switch() {
    local channel="$1"

    # 从配置文件读取渠道参数
    read -r TOKEN UPSTREAM NAME < <(python3 - "$CONFIG" "$channel" <<'PYEOF'
import json, sys

config = json.load(open(sys.argv[1]))
key = sys.argv[2]
channels = config.get("channels", {})
if key not in channels:
    print(f"错误：渠道 '{key}' 不存在，可用: {list(channels.keys())}", file=sys.stderr)
    sys.exit(1)
ch = channels[key]
print(ch["token"], ch["upstream"], ch.get("name", key))
PYEOF
)

    echo "切换到: $NAME"

    # 1. 更新 settings.json
    python3 - "$SETTINGS" "$TOKEN" <<'PYEOF'
import sys, json

path, token = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
data["env"]["ANTHROPIC_AUTH_TOKEN"] = token
data["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:4000"
with open(path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("  ✅ settings.json 已更新")
PYEOF

    # 2. 更新 plist
    if [ -f "$PLIST" ]; then
        python3 - "$PLIST" "$UPSTREAM" <<'PYEOF'
import sys, plistlib

path, upstream = sys.argv[1], sys.argv[2]
with open(path, "rb") as f:
    data = plistlib.load(f)
data["EnvironmentVariables"]["UPSTREAM"] = upstream
# 旧 plist 可能残留 FORCE_THINKING=1；代理已忽略该键，写 0 以免误导排查。
data["EnvironmentVariables"]["FORCE_THINKING"] = "0"
with open(path, "wb") as f:
    plistlib.dump(data, f)
print("  ✅ plist 已更新")
PYEOF

        # 改了 plist 必须 bootout + bootstrap，不是 unload/load，也不是 kickstart。
        # unload/load 是旧 API，对 bootstrap 装上的 job 经常是空操作，
        # 表现为「渠道切了、上游没变」；kickstart 不重读 plist。
        if launchctl list "$LABEL" &>/dev/null; then
            launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        fi

        local proxy_pid=""
        for _ in $(seq 1 50); do
            proxy_pid=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
            [ -z "$proxy_pid" ] && break
            sleep 0.2
        done
        if [ -n "$proxy_pid" ]; then
            echo "  ⚠️  端口 $PORT 仍被占用 (PID: $proxy_pid)，终止残留进程"
            kill "$proxy_pid" 2>/dev/null || true
            sleep 1
        fi

        launchctl bootstrap "gui/$(id -u)" "$PLIST"
        if launchctl list "$LABEL" &>/dev/null; then
            echo "  ✅ 代理已重启（plist 已重新加载）"
        else
            echo "  ❌ 代理启动失败，请检查: tail /tmp/claude-trace-proxy.log"
        fi
    else
        echo "  ⚠️  plist 不存在，执行 UPSTREAM=$UPSTREAM ./install-daemon.sh install"
    fi

    echo ""
    echo "🎯 $NAME → $UPSTREAM"
}

case "${1:-}" in
    status|s)   do_status ;;
    list|l)     do_list ;;
    "")         usage ;;
    *)
        # 将参数当作渠道名处理
        do_switch "$1"
        ;;
esac
