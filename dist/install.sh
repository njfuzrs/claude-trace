#!/bin/bash
# install.sh — claude-trace 一键安装脚本
#
# 用法：
#   curl -fsSL <release-url>/install.sh | bash
#   curl -fsSL <release-url>/install.sh | bash -s -- --non-interactive
#
# 非交互模式环境变量：
#   ANTHROPIC_AUTH_TOKEN  — API Token（必填）
#   UPSTREAM_URL          — 上游 API 地址（默认 https://api.anthropic.com）
#   TRAJ_PLATFORM_URL     — 轨迹上传服务器（默认内置团队服务器）
#   TRAJ_UPLOAD_TOKEN     — 上传 Token（默认内置）
#   FORCE_THINKING        — 强制 thinking（默认 0）
#   RELEASE_BASE          — 下载地址前缀（覆盖默认）

set -euo pipefail

# ─── 配置 ───

INSTALL_DIR="$HOME/.claude-trace"
VERSION_URL=""  # 将在发布时填入
RELEASE_BASE="${RELEASE_BASE:-http://127.0.0.1/releases/0.2.0}"
PORT="${PORT:-4000}"
LABEL="com.claude-trace.proxy"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
SETTINGS="$HOME/.claude/settings.json"
COLLECTOR_DEST="$HOME/.claude/hooks/collector.py"
LOG_FILE="/tmp/claude-trace-proxy.log"
CLI_SYMLINK_DIR="$HOME/.local/bin"
CLI_SYMLINK_PATH="$CLI_SYMLINK_DIR/claude-trace"
NON_INTERACTIVE=false
PATH_SNIPPET_RC=""

# Hook 事件列表（与 setup_hooks.py 保持一致）
HOOK_EVENTS="SessionStart SessionEnd UserPromptSubmit Stop PostToolUse SubagentStart SubagentStop PostCompact PreToolUse PermissionRequest InstructionsLoaded StopFailure"

# ─── 参数解析 ───

for arg in "$@"; do
    case "$arg" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --help|-h)
            echo "用法: curl -fsSL <url>/install.sh | bash"
            echo ""
            echo "选项："
            echo "  --non-interactive  非交互模式（通过环境变量传入配置）"
            echo ""
            echo "环境变量："
            echo "  ANTHROPIC_AUTH_TOKEN  API Token"
            echo "  UPSTREAM_URL          上游 API 地址"
            echo "  TRAJ_PLATFORM_URL     上传服务器地址"
            echo "  TRAJ_UPLOAD_TOKEN     上传 Token"
            echo "  RELEASE_BASE          下载地址前缀"
            exit 0
            ;;
    esac
done

# ─── 工具函数 ───

info()  { echo "  $*"; }
ok()    { echo "  ✅ $*"; }
warn()  { echo "  ⚠️  $*"; }
fail()  { echo "  ❌ $*"; exit 1; }

prompt_input() {
    local var_name="$1" prompt="$2" default="${3:-}"
    if [ "$NON_INTERACTIVE" = true ]; then
        eval "$var_name=\"\${$var_name:-$default}\""
        return
    fi
    local value
    if [ -n "$default" ]; then
        read -rp "  $prompt [$default]: " value
        value="${value:-$default}"
    else
        read -rp "  $prompt: " value
    fi
    eval "$var_name=\"$value\""
}

detect_shell_rc() {
    case "$(basename "${SHELL:-}")" in
        zsh)  echo "$HOME/.zshrc" ;;
        bash) echo "$HOME/.bashrc" ;;
        *)    echo "" ;;
    esac
}

ensure_cli_command() {
    mkdir -p "$CLI_SYMLINK_DIR"
    ln -sf "$INSTALL_DIR/claude-trace" "$CLI_SYMLINK_PATH"
    info "命令入口: $CLI_SYMLINK_PATH"

    if echo "$PATH" | tr ':' '\n' | grep -qx "$CLI_SYMLINK_DIR"; then
        return
    fi

    local shell_rc
    shell_rc="$(detect_shell_rc)"
    if [ -z "$shell_rc" ]; then
        warn "未识别当前 shell，请手动加入 PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
        return
    fi

    touch "$shell_rc"
    if ! grep -qF '# >>> claude-trace >>>' "$shell_rc"; then
        cat >> "$shell_rc" <<'EOF'

# >>> claude-trace >>>
export PATH="$HOME/.local/bin:$PATH"
# <<< claude-trace <<<
EOF
        PATH_SNIPPET_RC="$shell_rc"
        ok "已写入命令 PATH 到 $shell_rc"
    fi
}

# ─── 前置检查 ───

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   claude-trace 安装程序              ║"
echo "╚══════════════════════════════════════╝"
echo ""

# 检查 macOS
if [ "$(uname -s)" != "Darwin" ]; then
    fail "仅支持 macOS"
fi

# 检查架构
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ] && [ "$ARCH" != "x86_64" ]; then
    fail "不支持的架构: $ARCH"
fi
info "系统: macOS $(sw_vers -productVersion) ($ARCH)"

# 检查 python3（collector.py 需要）
if ! command -v python3 &>/dev/null; then
    fail "需要 python3（macOS 通常自带）。请运行: xcode-select --install"
fi
info "Python: $(python3 --version 2>&1)"

# 检查端口
if lsof -ti tcp:"$PORT" -sTCP:LISTEN &>/dev/null; then
    local_pid=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
    # 如果是已有的 claude-trace 进程，允许继续（升级场景）
    if ! ps -p "$local_pid" -o command= 2>/dev/null | grep -q "claude-trace"; then
        warn "端口 $PORT 已被占用 (PID: $local_pid)"
        warn "如果是其他程序占用，请先停止或使用 PORT=<其他端口> 重新安装"
    fi
fi

echo ""

# ─── 检测已有安装 ───

IS_UPGRADE=false
if [ -f "$INSTALL_DIR/version" ]; then
    OLD_VERSION="$(cat "$INSTALL_DIR/version")"
    IS_UPGRADE=true
    info "检测到已有安装: v$OLD_VERSION"

    # 备份 channels.json
    if [ -f "$INSTALL_DIR/channels.json" ]; then
        cp "$INSTALL_DIR/channels.json" "$INSTALL_DIR/channels.json.upgrade-bak"
        info "已备份 channels.json"
    fi

    # 停止服务
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        info "已停止旧服务"
    fi

    # 杀掉残留进程
    kill $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null) 2>/dev/null || true
    echo ""
fi

# ─── 下载或本地安装 ───

echo "=== 安装文件 ==="

if [ -n "$RELEASE_BASE" ]; then
    # 从远程下载
    TMPDIR_DL="$(mktemp -d)"
    trap "rm -rf '$TMPDIR_DL'" EXIT

    RELEASE_NAME="$(basename "$RELEASE_BASE")"
    TARBALL_CANDIDATES=()
    if [[ "$RELEASE_NAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9._-]+)?$ ]]; then
        TARBALL_CANDIDATES+=("claude-trace-${RELEASE_NAME}-darwin-${ARCH}.tar.gz")
    fi
    TARBALL_CANDIDATES+=("claude-trace-darwin-${ARCH}.tar.gz")

    TARBALL_URL=""
    for tarball_name in "${TARBALL_CANDIDATES[@]}"; do
        candidate_url="${RELEASE_BASE}/${tarball_name}"
        info "尝试下载: $candidate_url"
        if curl -fsSL "$candidate_url" -o "$TMPDIR_DL/claude-trace.tar.gz"; then
            TARBALL_URL="$candidate_url"
            break
        fi
    done

    if [ -z "$TARBALL_URL" ]; then
        fail "下载失败: ${RELEASE_BASE}/claude-trace-<version>-darwin-${ARCH}.tar.gz"
    fi

    # 解压
    mkdir -p "$INSTALL_DIR"
    tar -xzf "$TMPDIR_DL/claude-trace.tar.gz" -C "$INSTALL_DIR" --strip-components=1

    # 清除 macOS 隔离属性（Gatekeeper）
    xattr -cr "$INSTALL_DIR" 2>/dev/null || true

    ok "文件已安装到 $INSTALL_DIR"
else
    # 本地安装模式（从仓库目录安装，用于开发测试）
    # 检测项目仓库目录
    REPO_DIR=""
    # 情况1：直接执行 dist/install.sh（$0 有效）
    SCRIPT_SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || true
    if [ -n "$SCRIPT_SELF" ] && [ -f "$SCRIPT_SELF/proxy-daemon.sh" ]; then
        REPO_DIR="$(cd "$SCRIPT_SELF/.." && pwd)"
    # 情况2：从项目根目录运行
    elif [ -f "./dist/install.sh" ]; then
        REPO_DIR="$(pwd)"
    # 情况3：从 dist/ 目录运行
    elif [ -f "./install.sh" ] && [ -f "./proxy-daemon.sh" ]; then
        REPO_DIR="$(cd .. && pwd)"
    fi

    if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/trace_agent.py" ]; then
        fail "请设置 RELEASE_BASE 环境变量指向下载地址，或从项目目录运行: bash dist/install.sh"
    fi

    info "本地安装模式 (从 ${REPO_DIR})"
    mkdir -p "$INSTALL_DIR/bin"

    # 检查是否有预构建的二进制
    if [ -f "$REPO_DIR/dist/$ARCH/claude-trace-proxy" ]; then
        cp "$REPO_DIR/dist/$ARCH/claude-trace-proxy" "$INSTALL_DIR/bin/claude-trace-proxy"
    elif [ -f "$REPO_DIR/dist/claude-trace-proxy" ]; then
        cp "$REPO_DIR/dist/claude-trace-proxy" "$INSTALL_DIR/bin/claude-trace-proxy"
    else
        warn "未找到预构建二进制，请先运行: ./build/build.sh"
        warn "将使用 Python 脚本模式作为替代..."

        # 创建一个 wrapper 脚本代替二进制
        cat > "$INSTALL_DIR/bin/claude-trace-proxy" <<'WRAPPER'
#!/bin/bash
# 开发模式 wrapper：直接调用 python3 统一采集入口
REPO_DIR="$(cat "$HOME/.claude-trace/.repo_dir" 2>/dev/null)"
if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/trace_agent.py" ]; then
    echo "错误：找不到 trace_agent.py，请重新安装或先构建二进制"
    exit 1
fi
exec python3 "$REPO_DIR/trace_agent.py" "$@"
WRAPPER
        echo "$REPO_DIR" > "$INSTALL_DIR/.repo_dir"
    fi

    chmod +x "$INSTALL_DIR/bin/claude-trace-proxy"

    # 复制其他文件
    cp "$REPO_DIR/collector.py"          "$INSTALL_DIR/collector.py"
    cp "$REPO_DIR/git_state.py"          "$INSTALL_DIR/git_state.py"
    cp "$REPO_DIR/channels.json.example" "$INSTALL_DIR/channels.json.example"
    cp "$REPO_DIR/dist/claude-trace"     "$INSTALL_DIR/claude-trace"
    cp "$REPO_DIR/dist/proxy-daemon.sh"  "$INSTALL_DIR/proxy-daemon.sh"
    cp "$REPO_DIR/version"               "$INSTALL_DIR/version"

    chmod +x "$INSTALL_DIR/claude-trace"
    chmod +x "$INSTALL_DIR/proxy-daemon.sh"

    ok "文件已安装到 $INSTALL_DIR"
fi

# 恢复备份的 channels.json
if [ "$IS_UPGRADE" = true ] && [ -f "$INSTALL_DIR/channels.json.upgrade-bak" ]; then
    mv "$INSTALL_DIR/channels.json.upgrade-bak" "$INSTALL_DIR/channels.json"
    info "已恢复 channels.json"
fi

# 确保数据目录存在
mkdir -p "$INSTALL_DIR/trajectories/sessions"

echo ""

# ─── 交互配置（仅首次安装） ───

if [ "$IS_UPGRADE" = false ] || [ ! -f "$INSTALL_DIR/channels.json" ]; then
    echo "=== 配置 ==="
    echo ""

    # API Token
    ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-}"
    prompt_input ANTHROPIC_AUTH_TOKEN "API Token (ANTHROPIC_AUTH_TOKEN)"
    if [ -z "$ANTHROPIC_AUTH_TOKEN" ]; then
        fail "API Token 不能为空"
    fi

    # 上游地址
    UPSTREAM_URL="${UPSTREAM_URL:-https://api.anthropic.com}"
    prompt_input UPSTREAM_URL "上游 API 地址" "$UPSTREAM_URL"

    # Force thinking
    FORCE_THINKING="${FORCE_THINKING:-0}"
    prompt_input FORCE_THINKING "强制 thinking (0=关闭, 1=开启)" "$FORCE_THINKING"

    # 用户标识（用于区分数据来源）
    echo ""
    _default_user="${TRAJ_USER_ID:-$(whoami)}"
    TRAJ_USER_ID="${TRAJ_USER_ID:-}"
    prompt_input TRAJ_USER_ID "你的名字（用于标识数据来源）" "$_default_user"

    # 上传配置（内置团队默认值）
    echo ""
    TRAJ_PLATFORM_URL="${TRAJ_PLATFORM_URL:-http://127.0.0.1/traj}"
    TRAJ_UPLOAD_TOKEN="${TRAJ_UPLOAD_TOKEN:-<REDACTED_TOKEN>}"
    info "轨迹上传服务器: $TRAJ_PLATFORM_URL (内置默认)"

    # 写入 channels.json
    python3 - "$INSTALL_DIR/channels.json" "$ANTHROPIC_AUTH_TOKEN" "$UPSTREAM_URL" "$FORCE_THINKING" "$TRAJ_PLATFORM_URL" "$TRAJ_UPLOAD_TOKEN" "$TRAJ_USER_ID" <<'PYEOF'
import json, sys, os

path = sys.argv[1]
token = sys.argv[2]
upstream = sys.argv[3]
force_thinking = int(sys.argv[4]) if sys.argv[4] else 0
platform_url = sys.argv[5] if len(sys.argv) > 5 else ""
upload_token = sys.argv[6] if len(sys.argv) > 6 else ""
user_id = sys.argv[7] if len(sys.argv) > 7 else os.getlogin()

config = {
    "active_channel": "default",
    "channels": {
        "default": {
            "name": "默认渠道",
            "token": token,
            "upstream": upstream,
            "force_thinking": force_thinking,
        }
    },
    "upload": {
        "platform_url": platform_url,
        "upload_token": upload_token,
        "user_id": user_id,
        "device_id": os.uname().nodename,
        "cleanup_after_upload": True,
    }
}

with open(path, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

    ok "channels.json 已生成"
    echo ""
else
    # 升级模式：从已有 channels.json 读取 token
    ANTHROPIC_AUTH_TOKEN="$(python3 -c "
import json
config = json.load(open('$INSTALL_DIR/channels.json'))
channels = config.get('channels', {})
active = config.get('active_channel', '')
if active and active in channels:
    print(channels[active].get('token', ''))
elif channels:
    print(next(iter(channels.values())).get('token', ''))
" 2>/dev/null || true)"
fi

# ─── 部署 collector.py ───

echo "=== 部署 Hooks ==="

mkdir -p "$(dirname "$COLLECTOR_DEST")"
cp "$INSTALL_DIR/collector.py" "$COLLECTOR_DEST"
chmod 755 "$COLLECTOR_DEST"
ok "collector.py → $COLLECTOR_DEST"

# git_state.py 是 collector.py 的同目录依赖（采集 git HEAD / 工作区脏状态）。
# hook 以 `python3 <hooks>/collector.py` 运行，sys.path[0] 即 hooks 目录；
# 缺这个文件 collector 会静默降级为不采集 git 状态。
if [ -f "$INSTALL_DIR/git_state.py" ]; then
    cp "$INSTALL_DIR/git_state.py" "$(dirname "$COLLECTOR_DEST")/git_state.py"
    ok "git_state.py → $(dirname "$COLLECTOR_DEST")/git_state.py"
else
    warn "git_state.py 缺失，git 状态采集将不可用"
fi

# ─── 配置 settings.json ───

echo ""
echo "=== 配置 Claude Code ==="

# 备份
if [ -f "$SETTINGS" ]; then
    cp "$SETTINGS" "${SETTINGS}.bak"
    info "已备份 settings.json"
fi

# 用 Python 合并 hooks 配置
python3 - "$SETTINGS" "$COLLECTOR_DEST" "$ANTHROPIC_AUTH_TOKEN" "$PORT" <<'PYEOF'
import json, sys, os
from pathlib import Path

settings_path = sys.argv[1]
collector_path = sys.argv[2]
token = sys.argv[3]
port = sys.argv[4]

# 加载已有配置
settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, ValueError):
        pass

# Hook 事件列表
hook_events = [
    "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop",
    "PostToolUse", "SubagentStart", "SubagentStop", "PostCompact",
    "PreToolUse", "PermissionRequest", "InstructionsLoaded", "StopFailure",
]

# 构建 hooks 配置
hooks = settings.get("hooks", {})
for event in hook_events:
    timeout = 10 if event == "Stop" else 5
    hook_entry = {
        "hooks": [{
            "type": "command",
            "command": f"python3 {collector_path}",
            "timeout": timeout,
        }]
    }
    # 检查是否已有该事件的 hook 配置
    if event in hooks:
        # 移除旧的 collector.py 条目，保留其他
        existing_groups = hooks[event]
        filtered = []
        for group in existing_groups:
            kept = [h for h in group.get("hooks", []) if "collector.py" not in h.get("command", "")]
            if kept:
                group["hooks"] = kept
                filtered.append(group)
        filtered.append(hook_entry)
        hooks[event] = filtered
    else:
        hooks[event] = [hook_entry]

settings["hooks"] = hooks

# 设置环境变量
env = settings.get("env", {})
env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
if token:
    env["ANTHROPIC_AUTH_TOKEN"] = token
settings["env"] = env

# 原子写入
Path(settings_path).parent.mkdir(parents=True, exist_ok=True)
tmp_path = settings_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.rename(tmp_path, settings_path)

print("  ✅ settings.json 已配置")
print(f"     hooks: {len(hook_events)} 个事件")
print(f"     ANTHROPIC_BASE_URL: http://127.0.0.1:{port}")
PYEOF

# ─── 安装 launchd 统一采集服务 ───

echo ""
echo "=== 启动统一采集服务 ==="

# 从 channels.json 读取上游配置
_svc_config=$(python3 -c "
import json, sys, os
config = json.load(open(sys.argv[1]))
channels = config.get('channels', {})
active = config.get('active_channel', '')
if active and active in channels:
    ch = channels[active]
elif channels:
    ch = next(iter(channels.values()))
else:
    ch = {}
upload = config.get('upload', {})
# 用换行分隔，避免 shell eval 问题
vals = [
    ch.get('upstream', 'https://api.anthropic.com'),
    str(ch.get('force_thinking', 0)),
    upload.get('platform_url', ''),
    upload.get('upload_token', ''),
    upload.get('user_id', ''),
    upload.get('device_id', ''),
    str(upload.get('cleanup_after_upload', 'true')),
]
print('\n'.join(vals))
" "$INSTALL_DIR/channels.json")

SVC_UPSTREAM="$(echo "$_svc_config" | sed -n '1p')"
SVC_FORCE_THINKING="$(echo "$_svc_config" | sed -n '2p')"
SVC_PLATFORM_URL="$(echo "$_svc_config" | sed -n '3p')"
SVC_UPLOAD_TOKEN="$(echo "$_svc_config" | sed -n '4p')"
SVC_USER_ID="$(echo "$_svc_config" | sed -n '5p')"
SVC_DEVICE_ID="$(echo "$_svc_config" | sed -n '6p')"
SVC_CLEANUP="$(echo "$_svc_config" | sed -n '7p')"

mkdir -p "$HOME/Library/LaunchAgents"

# 如果已有服务，先卸载
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

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
        <string>${INSTALL_DIR}/proxy-daemon.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PORT</key>
        <string>${PORT}</string>
        <key>UPSTREAM</key>
        <string>${SVC_UPSTREAM}</string>
        <key>OUTPUT</key>
        <string>${INSTALL_DIR}/trajectories</string>
        <key>FORCE_THINKING</key>
        <string>${SVC_FORCE_THINKING}</string>
        <key>TRAJ_PLATFORM_URL</key>
        <string>${SVC_PLATFORM_URL}</string>
        <key>TRAJ_UPLOAD_TOKEN</key>
        <string>${SVC_UPLOAD_TOKEN}</string>
        <key>TRAJ_USER_ID</key>
        <string>${SVC_USER_ID}</string>
        <key>TRAJ_DEVICE_ID</key>
        <string>${SVC_DEVICE_ID}</string>
        <key>TRAJ_CLEANUP_AFTER_UPLOAD</key>
        <string>${SVC_CLEANUP}</string>
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
    <string>${INSTALL_DIR}</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
ok "launchd 统一采集服务已启动"

# ─── 等待统一采集器健康检查 ───

info "等待统一采集器启动（首次启动约需 10 秒，Codex 可能顺带补采历史会话）..."
HEALTH_OK=false
for i in $(seq 1 20); do
    if curl -s "http://127.0.0.1:$PORT/_internal/health" &>/dev/null; then
        HEALTH_OK=true
        break
    fi
    sleep 1
done

echo ""
if [ "$HEALTH_OK" = true ]; then
    ok "统一采集器已就绪 (http://127.0.0.1:$PORT)"
else
    warn "统一采集器尚未响应，请检查日志: tail -20 $LOG_FILE"
fi

# ─── 配置 CLI 命令 ───

ensure_cli_command

CLI_START_CMD="claude-trace start"
CLI_STATUS_CMD="claude-trace status"
CLI_SWITCH_LIST_CMD="claude-trace switch list"
CLI_LOGS_CMD="claude-trace logs"
CLI_UNINSTALL_CMD="claude-trace uninstall"
if ! command -v claude-trace >/dev/null 2>&1; then
    CLI_START_CMD="$CLI_SYMLINK_PATH start"
    CLI_STATUS_CMD="$CLI_SYMLINK_PATH status"
    CLI_SWITCH_LIST_CMD="$CLI_SYMLINK_PATH switch list"
    CLI_LOGS_CMD="$CLI_SYMLINK_PATH logs"
    CLI_UNINSTALL_CMD="$CLI_SYMLINK_PATH uninstall"
fi

# ─── 完成 ───

NEW_VERSION="$(cat "$INSTALL_DIR/version" 2>/dev/null || echo "unknown")"

echo ""
echo "╔══════════════════════════════════════╗"
if [ "$IS_UPGRADE" = true ]; then
echo "║   升级完成！v$OLD_VERSION → v$NEW_VERSION"
else
echo "║   安装完成！v$NEW_VERSION"
fi
echo "╚══════════════════════════════════════╝"
echo ""
echo "  统一采集服务已默认启动。后续如需恢复或重启采集，只需一个命令："
echo "    $CLI_START_CMD"
echo ""
echo "  常用命令："
echo "    $CLI_STATUS_CMD       # 查看状态"
echo "    $CLI_SWITCH_LIST_CMD  # 列出渠道"
echo "    $CLI_LOGS_CMD         # 查看日志"
echo "    $CLI_UNINSTALL_CMD    # 卸载"
echo ""
echo "  现在可以正常使用 Claude Code，Claude + Codex 轨迹都会默认自动采集。"
echo ""

# 提示刷新 shell
if [ -n "$PATH_SNIPPET_RC" ]; then
    echo "  当前 shell 还未加载 PATH。若要直接使用 claude-trace，请先执行一次："
    echo "    source $PATH_SNIPPET_RC"
    echo ""
fi
