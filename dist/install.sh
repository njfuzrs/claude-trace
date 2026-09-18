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
#   TRAJ_PLATFORM_URL     — 轨迹上传服务器（留空则禁用上传，默认留空）
#   TRAJ_UPLOAD_TOKEN     — 上传 Token（留空则禁用上传，默认留空）
#   TRAJ_USER_ID          — 上传时携带的用户标识（默认留空 = 不上报）
#   TRAJ_DEVICE_ID        — 上传时携带的设备标识（默认留空 = 不上报）
#   FORCE_THINKING        — 强制 thinking（默认 0）
#   RELEASE_BASE          — 下载地址前缀（覆盖默认）

set -euo pipefail

# ─── 配置 ───

INSTALL_DIR="${INSTALL_DIR:-$HOME/.claude-trace}"
# 版本号单一事实源：仓库根的 version 文件；脱离仓库运行时由 release.sh 注入 VERSION
VERSION="${VERSION:-$(cat "$(dirname "$0")/../version" 2>/dev/null || echo "")}"
GH_REPO="${GH_REPO:-njfuzrs/claude-trace}"
# RELEASE_BASE 是否由调用方显式给出 —— 这是「远程下载」与「仓库内本地安装」的分流依据。
#
# 老逻辑把默认值也算成「已设置」：只要能读到仓库的 version 文件，RELEASE_BASE 就非空，
# 于是 `if [ -n "$RELEASE_BASE" ]` 永远为真，后面整段本地安装分支成了死代码 ——
# README 写的 `bash dist/install.sh` 在 Releases 为空时直接失败，也不会用旁边刚
# 构建好的 dist/$ARCH/。分流必须看「调用方有没有显式指定下载地址」，不能看默认值。
if [ -n "${RELEASE_BASE:-}" ]; then
    RELEASE_BASE_EXPLICIT=true
else
    RELEASE_BASE_EXPLICIT=false
    RELEASE_BASE=""
fi
PORT="${PORT:-4000}"
LABEL="${LABEL:-com.claude-trace.proxy}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
SETTINGS="${SETTINGS:-$HOME/.claude/settings.json}"
COLLECTOR_DEST="${COLLECTOR_DEST:-$HOME/.claude/hooks/collector.py}"
LOG_FILE="${LOG_FILE:-/tmp/claude-trace-proxy.log}"
CLI_SYMLINK_DIR="${CLI_SYMLINK_DIR:-$HOME/.local/bin}"
CLI_SYMLINK_PATH="$CLI_SYMLINK_DIR/claude-trace"
# SKIP_SERVICE=true：只落盘文件，不写 launchd、不改本机 settings、不杀端口上的进程。
# 给「本地冒充 Release 安装」用，避免验收把正在采集的服务停掉。
SKIP_SERVICE="${SKIP_SERVICE:-false}"
NON_INTERACTIVE=false
PATH_SNIPPET_RC=""
# --print-plan：只解析「装哪一份、从哪儿取」并打印，不碰磁盘。
# 存在的理由：安装器的分流与文件名拼接曾经两处都错且无人发现（发了 Release 也 404），
# 而验证它们不该要求真装一遍。有了它，CI 能在 Linux 上直接断言候选名。
PRINT_PLAN=false

# Hook 事件列表（与 setup_hooks.py 保持一致）
HOOK_EVENTS="SessionStart SessionEnd UserPromptSubmit Stop PostToolUse SubagentStart SubagentStop PostCompact PreToolUse PermissionRequest InstructionsLoaded StopFailure"

# ─── 参数解析 ───

for arg in "$@"; do
    case "$arg" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --print-plan) PRINT_PLAN=true ;;
        --help|-h)
            echo "用法: curl -fsSL <url>/install.sh | bash"
            echo ""
            echo "选项："
            echo "  --non-interactive  非交互模式（通过环境变量传入配置）"
            echo "  --print-plan       只打印安装模式与下载候选名后退出，不改动任何文件"
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

# 解析「装哪一份、从哪儿取」。只做判定，不碰磁盘，供安装流程与 --print-plan 共用。
# 输出：REPO_DIR / INSTALL_MODE / RELEASE_BASE / TARBALL_CANDIDATES
resolve_install_plan() {
    # 先探测仓库目录 —— 分流依据是「调用方有没有显式给下载地址」。
    # curl | bash 时 $0 是 bash 或 /dev/stdin，探测不到仓库，必然落到远程。
    REPO_DIR=""
    local script_self
    script_self="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || true
    if [ -n "$script_self" ] && [ -f "$script_self/proxy-daemon.sh" ] && [ -f "$script_self/../trace_agent.py" ]; then
        # 情况1：直接执行 dist/install.sh
        REPO_DIR="$(cd "$script_self/.." && pwd)"
    elif [ -f "./dist/install.sh" ] && [ -f "./trace_agent.py" ]; then
        # 情况2：从项目根目录运行
        REPO_DIR="$(pwd)"
    elif [ -f "./install.sh" ] && [ -f "./proxy-daemon.sh" ] && [ -f "../trace_agent.py" ]; then
        # 情况3：从 dist/ 目录运行
        REPO_DIR="$(cd .. && pwd)"
    fi

    if [ "$RELEASE_BASE_EXPLICIT" = true ]; then
        INSTALL_MODE=remote
    elif [ -n "$REPO_DIR" ]; then
        INSTALL_MODE=local
    else
        INSTALL_MODE=remote
    fi

    if [ "$INSTALL_MODE" = remote ] && [ "$RELEASE_BASE_EXPLICIT" = false ]; then
        if [ -n "$VERSION" ]; then
            RELEASE_BASE="https://github.com/${GH_REPO}/releases/download/v${VERSION}"
        else
            RELEASE_BASE="https://github.com/${GH_REPO}/releases/latest/download"
        fi
    fi

    # 候选文件名：构建产出的是 claude-trace-${VERSION}-darwin-${ARCH}.tar.gz。
    #
    # 老逻辑拿 basename "$RELEASE_BASE" 当版本号，可 GitHub 下载路径的最后一段是 tag
    # （`v0.3.0`），而正则要求以数字开头 —— 带版本号的候选名被整段跳过，只去下一个
    # 构建从不产出的 claude-trace-darwin-${ARCH}.tar.gz，发了 Release 照样 404。
    # 现在剥掉 tag 的前导 v，并优先用已知的 VERSION（release.sh 会把它固化进发布版）。
    TARBALL_CANDIDATES=()
    if [ "$INSTALL_MODE" != remote ]; then
        return 0
    fi

    local release_version
    release_version="$(basename "$RELEASE_BASE")"
    release_version="${release_version#v}"

    # 顺序：先按 RELEASE_BASE 指向的 tag，再按已知 VERSION，最后是不带版本号的兼容名。
    # tag 优先是因为它是调用方明确要装的那一版；VERSION 可能只是当前仓库/注入的环境值，
    # 用它去 v0.3.0 的目录里找 0.2.0 的包只会白跑一次 404。
    local name existing found
    for name in \
        "$( [[ "$release_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9._-]+)?$ ]] \
              && echo "claude-trace-${release_version}-darwin-${ARCH}.tar.gz" )" \
        "${VERSION:+claude-trace-${VERSION}-darwin-${ARCH}.tar.gz}" \
        "claude-trace-darwin-${ARCH}.tar.gz"
    do
        # 最后一个不带版本号的名字留作兼容：走 latest/download，
        # 或发布侧额外传了稳定名时，仍然能装上。
        [ -z "$name" ] && continue
        found=false
        for existing in ${TARBALL_CANDIDATES[@]+"${TARBALL_CANDIDATES[@]}"}; do
            [ "$existing" = "$name" ] && found=true && break
        done
        [ "$found" = false ] && TARBALL_CANDIDATES+=("$name")
    done
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

# ─── --print-plan：只判定不落盘 ───

# 放在 macOS 前置检查之前：这条路径不装任何东西，没有理由要求在 macOS 上跑，
# 这样 CI 在 ubuntu 上也能断言候选名与分流。ARCH 可由调用方覆盖以模拟另一架构。
if [ "$PRINT_PLAN" = true ]; then
    ARCH="${ARCH:-$(uname -m)}"
    resolve_install_plan
    echo "install_mode=$INSTALL_MODE"
    echo "repo_dir=${REPO_DIR:-}"
    echo "release_base=${RELEASE_BASE:-}"
    echo "version=${VERSION:-}"
    echo "arch=$ARCH"
    for name in ${TARBALL_CANDIDATES[@]+"${TARBALL_CANDIDATES[@]}"}; do
        echo "candidate=$name"
    done
    exit 0
fi

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

# 检查端口（验收 / 冒充安装时不碰正在跑的采集）
if [ "$SKIP_SERVICE" != true ] && lsof -ti tcp:"$PORT" -sTCP:LISTEN &>/dev/null; then
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
OLD_VERSION=""
OLD_BIN_SHA=""
if [ -f "$INSTALL_DIR/version" ]; then
    OLD_VERSION="$(cat "$INSTALL_DIR/version")"
    IS_UPGRADE=true
    info "检测到已有安装: v$OLD_VERSION"

    # 记下旧二进制指纹：版本号相同时，这是唯一能回答「二进制到底换没换」的依据。
    # 只看 version 文件的话，同一个 0.2.0 可能对应行为完全不同的两份构建。
    if [ -f "$INSTALL_DIR/bin/claude-trace-proxy" ]; then
        OLD_BIN_SHA="$(shasum -a 256 "$INSTALL_DIR/bin/claude-trace-proxy" 2>/dev/null | cut -d' ' -f1)"
    fi

    # 备份 channels.json。只备份、不停服务：远程下载失败时旧进程必须还在跑，
    # 否则「Release 404」会把正在采集的 Claude Code 一起掐断。
    if [ -f "$INSTALL_DIR/channels.json" ]; then
        cp "$INSTALL_DIR/channels.json" "$INSTALL_DIR/channels.json.upgrade-bak"
        info "已备份 channels.json"
    fi
    echo ""
fi

# ─── 决定安装模式（远程下载 / 仓库内本地安装） ───

resolve_install_plan

echo "=== 安装文件 ==="

# 新文件先落到临时目录，校验成功后再停旧服务、再替换 INSTALL_DIR。
# 两个曾经踩过的坑：
# 1. 先停服务再下载：Release 404 时采集已经断了，Claude Code 跟着 403。
# 2. 先覆盖正在跑的 Mach-O 再停：macOS 上正在映射的二进制被 cp 覆盖后，
#    再 exec 同一路径会被 SIGKILL（退出码 137）。正确顺序是先 bootout。
STAGING="$(mktemp -d)"
cleanup_staging() { rm -rf "$STAGING"; }
trap cleanup_staging EXIT

if [ "$INSTALL_MODE" = remote ]; then
    TARBALL_URL=""
    for tarball_name in "${TARBALL_CANDIDATES[@]}"; do
        candidate_url="${RELEASE_BASE}/${tarball_name}"
        info "尝试下载: $candidate_url"
        if curl -fsSL "$candidate_url" -o "$STAGING/claude-trace.tar.gz"; then
            # 校验是完整的 gzip 包再算成功：半截下载 / 被改写成 HTML 错误页的情况下，
            # 直接解压会把 INSTALL_DIR 弄成半安装状态。
            if tar -tzf "$STAGING/claude-trace.tar.gz" >/dev/null 2>&1; then
                TARBALL_URL="$candidate_url"
                break
            fi
            warn "下载内容不是完整的 tar.gz，跳过: $candidate_url"
            rm -f "$STAGING/claude-trace.tar.gz"
        fi
    done

    if [ -z "$TARBALL_URL" ]; then
        # 报错要能区分「Release 不存在」和「文件名不匹配」，否则排查只能靠猜。
        echo ""
        echo "  ❌ 远程安装失败，以下地址都取不到可用的包："
        for tarball_name in "${TARBALL_CANDIDATES[@]}"; do
            echo "     ${RELEASE_BASE}/${tarball_name}"
        done
        echo ""
        if curl -fsI "$RELEASE_BASE/install.sh" >/dev/null 2>&1; then
            echo "  该 Release 存在，但没有当前架构（${ARCH}）的包 —— 文件名或架构对不上。"
        else
            echo "  该 Release 本身取不到（可能尚未发布，或 tag 名有误）：$RELEASE_BASE"
        fi
        echo "  已有安装未被改动。可改用源码安装：git clone 后 ./build/build.sh && bash dist/install.sh"
        exit 1
    fi

    mkdir -p "$STAGING/payload"
    tar -xzf "$STAGING/claude-trace.tar.gz" -C "$STAGING/payload" --strip-components=1
    rm -f "$STAGING/claude-trace.tar.gz"
    if [ ! -f "$STAGING/payload/bin/claude-trace-proxy" ] && [ ! -f "$STAGING/payload/proxy-daemon.sh" ]; then
        echo "  ❌ 下载的包解压后缺少必要文件，已有安装未被改动。"
        exit 1
    fi
    xattr -cr "$STAGING/payload" 2>/dev/null || true
else
    # 本地安装模式（从仓库目录安装，用于开发与自建）
    info "本地安装模式 (从 ${REPO_DIR})"
    mkdir -p "$STAGING/payload/bin"

    if [ -f "$REPO_DIR/dist/$ARCH/claude-trace-proxy" ]; then
        cp "$REPO_DIR/dist/$ARCH/claude-trace-proxy" "$STAGING/payload/bin/claude-trace-proxy"
    elif [ -f "$REPO_DIR/dist/claude-trace-proxy" ]; then
        cp "$REPO_DIR/dist/claude-trace-proxy" "$STAGING/payload/bin/claude-trace-proxy"
    else
        warn "未找到预构建二进制，请先运行: ./build/build.sh"
        warn "将使用 Python 脚本模式作为替代..."
        cat > "$STAGING/payload/bin/claude-trace-proxy" <<'WRAPPER'
#!/bin/bash
# 开发模式 wrapper：直接调用 python3 统一采集入口
REPO_DIR="$(cat "$HOME/.claude-trace/.repo_dir" 2>/dev/null)"
if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/trace_agent.py" ]; then
    echo "错误：找不到 trace_agent.py，请重新安装或先构建二进制"
    exit 1
fi
exec python3 "$REPO_DIR/trace_agent.py" "$@"
WRAPPER
        echo "$REPO_DIR" > "$STAGING/payload/.repo_dir"
    fi

    cp "$REPO_DIR/collector.py"          "$STAGING/payload/collector.py"
    cp "$REPO_DIR/git_state.py"          "$STAGING/payload/git_state.py"
    cp "$REPO_DIR/channels.json.example" "$STAGING/payload/channels.json.example"
    cp "$REPO_DIR/dist/claude-trace"     "$STAGING/payload/claude-trace"
    cp "$REPO_DIR/dist/proxy-daemon.sh"  "$STAGING/payload/proxy-daemon.sh"
    cp "$REPO_DIR/version"               "$STAGING/payload/version"
    chmod +x "$STAGING/payload/bin/claude-trace-proxy"
    chmod +x "$STAGING/payload/claude-trace"
    chmod +x "$STAGING/payload/proxy-daemon.sh"
fi

# staging 已齐，这才停旧服务。SKIP_SERVICE 路径（验收 / 冒充 Release）完全不碰。
if [ "$SKIP_SERVICE" != true ]; then
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        info "已停止旧服务"
    fi
    kill $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null) 2>/dev/null || true
fi

mkdir -p "$INSTALL_DIR"
# 只覆盖包内文件，不碰 trajectories / channels.json（后者在下面从备份恢复）。
# 不用 ditto：安装器只在 macOS 上跑，但 cp -R 更直白，也不依赖额外工具。
cp -R "$STAGING/payload/." "$INSTALL_DIR/"
ok "文件已安装到 $INSTALL_DIR"

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

    # ─── 上传配置（默认关闭，opt-in） ───
    # 无内置默认端点与凭据：两者留空即禁用上传，数据只留在本机。
    echo ""
    TRAJ_PLATFORM_URL="${TRAJ_PLATFORM_URL:-}"
    TRAJ_UPLOAD_TOKEN="${TRAJ_UPLOAD_TOKEN:-}"
    TRAJ_USER_ID="${TRAJ_USER_ID:-}"
    TRAJ_DEVICE_ID="${TRAJ_DEVICE_ID:-}"

    if [ "$NON_INTERACTIVE" = false ]; then
        echo "  轨迹上传（可选，默认关闭）"
        echo "  ⚠️  开启后会把【完整对话内容】上传到你指定的服务器，"
        echo "      其中包含你的提示词、代码、文件路径与工具调用结果。"
        echo "      不配置则数据只保存在本机 $INSTALL_DIR/trajectories。"
        read -rp "  是否配置轨迹上传？(y/N): " _enable_upload
        case "$_enable_upload" in
            [yY]|[yY][eE][sS])
                prompt_input TRAJ_PLATFORM_URL "上传服务器地址（留空取消）"
                if [ -n "$TRAJ_PLATFORM_URL" ]; then
                    prompt_input TRAJ_UPLOAD_TOKEN "上传 Token（留空取消）"
                fi
                echo "  以下两个标识会随每条轨迹上传，可直接回车留空："
                prompt_input TRAJ_USER_ID "用户标识（可留空）"
                prompt_input TRAJ_DEVICE_ID "设备标识（可留空）"
                ;;
        esac
    fi

    if [ -n "$TRAJ_PLATFORM_URL" ] && [ -n "$TRAJ_UPLOAD_TOKEN" ]; then
        info "轨迹上传已启用: $TRAJ_PLATFORM_URL"
    else
        info "轨迹上传未配置，数据仅保存在本地"
    fi

    # 写入 channels.json
    python3 - "$INSTALL_DIR/channels.json" "$ANTHROPIC_AUTH_TOKEN" "$UPSTREAM_URL" "$FORCE_THINKING" "$TRAJ_PLATFORM_URL" "$TRAJ_UPLOAD_TOKEN" "$TRAJ_USER_ID" "$TRAJ_DEVICE_ID" <<'PYEOF'
import json, sys

path = sys.argv[1]
token = sys.argv[2]
upstream = sys.argv[3]
force_thinking = int(sys.argv[4]) if sys.argv[4] else 0
platform_url = sys.argv[5] if len(sys.argv) > 5 else ""
upload_token = sys.argv[6] if len(sys.argv) > 6 else ""
# 身份字段默认留空：不回退到系统用户名/主机名（常含真实姓名与资产编号）
user_id = sys.argv[7] if len(sys.argv) > 7 else ""
device_id = sys.argv[8] if len(sys.argv) > 8 else ""

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
        "device_id": device_id,
        # 默认 False：上传成功也保留本地数据。
        # 曾经硬编码 True，而「上传成功」的判断一旦有偏差数据就没了第二份 ——
        # 409 幂等 bug 恰恰把「服务端拒绝覆盖」也算成了成功，实测 7247 个会话
        # 目录只剩一个 .uploaded 标记。这里必须与 uploader.py / proxy.py /
        # install-daemon.sh 的默认值一致，否则重装会把「保留」改回「删除」。
        "cleanup_after_upload": False,
        # 启动补传：扫描盘上未上云的会话并补齐，上传链路的兜底
        "backfill_on_start": True,
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

# ─── 部署 collector.py / 配置 Claude Code / 启动服务 ───

if [ "$SKIP_SERVICE" = true ]; then
    info "SKIP_SERVICE=true：只落盘文件，不部署 hooks、不改 settings、不启动 launchd"
else
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


def flag(value, default):
    '''把 JSON 里的 true/false 归一成 launchd 认的 'true'/'false' 字符串'''
    if value is None:
        value = default
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return 'true' if str(value).strip().lower() in ('1', 'true', 'yes', 'on') else 'false'


# 用换行分隔，避免 shell eval 问题
vals = [
    ch.get('upstream', 'https://api.anthropic.com'),
    str(ch.get('force_thinking', 0)),
    upload.get('platform_url', ''),
    upload.get('upload_token', ''),
    upload.get('user_id', ''),
    upload.get('device_id', ''),
    # 兜底 false：与 uploader.py / proxy.py / install-daemon.sh 一致。
    # 这里曾兜底 'true'，于是老配置（没有这个键）一重装就变成「上传成功即删本地」。
    flag(upload.get('cleanup_after_upload'), False),
    flag(upload.get('backfill_on_start'), True),
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
SVC_BACKFILL="$(echo "$_svc_config" | sed -n '8p')"

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
        <key>TRAJ_BACKFILL_ON_START</key>
        <string>${SVC_BACKFILL}</string>
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
fi  # SKIP_SERVICE

# ─── 完成 ───

NEW_VERSION="$(cat "$INSTALL_DIR/version" 2>/dev/null || echo "unknown")"
NEW_BIN_SHA=""
if [ -f "$INSTALL_DIR/bin/claude-trace-proxy" ]; then
    NEW_BIN_SHA="$(shasum -a 256 "$INSTALL_DIR/bin/claude-trace-proxy" 2>/dev/null | cut -d' ' -f1)"
fi

echo ""
echo "╔══════════════════════════════════════╗"
if [ "$IS_UPGRADE" = true ]; then
echo "║   升级完成！v$OLD_VERSION → v$NEW_VERSION"
else
echo "║   安装完成！v$NEW_VERSION"
fi
echo "╚══════════════════════════════════════╝"
echo ""

# 版本号相同时说清二进制换没换 —— 否则「v0.2.0 → v0.2.0」这行什么都没告诉用户。
if [ "$IS_UPGRADE" = true ] && [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    if [ -n "$OLD_BIN_SHA" ] && [ -n "$NEW_BIN_SHA" ] && [ "$OLD_BIN_SHA" != "$NEW_BIN_SHA" ]; then
        info "版本号未变，但二进制已更新（sha256 ${OLD_BIN_SHA:0:12} → ${NEW_BIN_SHA:0:12}）"
    elif [ -n "$NEW_BIN_SHA" ] && [ "$OLD_BIN_SHA" = "$NEW_BIN_SHA" ]; then
        info "版本号与二进制均未变化（sha256 ${NEW_BIN_SHA:0:12}），本次只重写了配置与服务"
    fi
fi
if [ -n "$NEW_BIN_SHA" ]; then
    echo "  二进制: ${NEW_BIN_SHA:0:12}  ($INSTALL_DIR/bin/claude-trace-proxy)"
    echo ""
fi
if [ "$SKIP_SERVICE" = true ]; then
    echo "  SKIP_SERVICE=true：文件已落到 $INSTALL_DIR，服务未启动。"
    echo ""
else
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
fi

# 提示刷新 shell
if [ -n "$PATH_SNIPPET_RC" ]; then
    echo "  当前 shell 还未加载 PATH。若要直接使用 claude-trace，请先执行一次："
    echo "    source $PATH_SNIPPET_RC"
    echo ""
fi
