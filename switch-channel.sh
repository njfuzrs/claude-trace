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
    ft = ch.get("force_thinking", 0)
    token = ch.get("token", "")
    token_preview = token[:12] + "…" if token else "?"
    print(f"  {key:<12} {name}  ({upstream})")
    print(f"             token={token_preview}  force_thinking={ft}")
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
        FT=$(python3 -c "
import plistlib
with open('$PLIST','rb') as f: d=plistlib.load(f)
print(d.get('EnvironmentVariables',{}).get('FORCE_THINKING','?'))
" 2>/dev/null)
        echo "  upstream : $UPSTREAM"
        echo "  force_thinking : $FT"
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
    read -r TOKEN UPSTREAM FORCE_THINKING NAME < <(python3 - "$CONFIG" "$channel" <<'PYEOF'
import json, sys

config = json.load(open(sys.argv[1]))
key = sys.argv[2]
channels = config.get("channels", {})
if key not in channels:
    print(f"错误：渠道 '{key}' 不存在，可用: {list(channels.keys())}", file=sys.stderr)
    sys.exit(1)
ch = channels[key]
print(ch["token"], ch["upstream"], ch.get("force_thinking", 0), ch.get("name", key))
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
        python3 - "$PLIST" "$UPSTREAM" "$FORCE_THINKING" <<'PYEOF'
import sys, plistlib

path, upstream, ft = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "rb") as f:
    data = plistlib.load(f)
data["EnvironmentVariables"]["UPSTREAM"] = upstream
data["EnvironmentVariables"]["FORCE_THINKING"] = ft
with open(path, "wb") as f:
    plistlib.dump(data, f)
print("  ✅ plist 已更新")
PYEOF

        # 3. 重启代理（unload+load 让 plist 新环境变量生效）
        #    先停掉旧服务和进程，再重新加载
        local was_running=false
        if launchctl list "$LABEL" &>/dev/null; then
            was_running=true
            launchctl unload "$PLIST" 2>/dev/null || true
        fi

        # 确保端口释放（unload 可能不会立即杀掉 Python 子进程）
        local proxy_pid
        proxy_pid=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
        if [ -n "$proxy_pid" ]; then
            kill "$proxy_pid" 2>/dev/null
            for i in 1 2 3; do
                lsof -ti tcp:"$PORT" -sTCP:LISTEN &>/dev/null || break
                sleep 1
            done
        fi

        # 重新加载 plist 并启动
        launchctl load "$PLIST" 2>/dev/null
        if launchctl list "$LABEL" &>/dev/null; then
            echo "  ✅ 代理已重启（plist 已重新加载）"
        else
            echo "  ❌ 代理启动失败，请检查: tail /tmp/claude-trace-proxy.log"
        fi
    else
        echo "  ⚠️  plist 不存在，执行 UPSTREAM=$UPSTREAM FORCE_THINKING=$FORCE_THINKING ./install-daemon.sh install"
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
