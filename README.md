# claude-trace

![platform](https://img.shields.io/badge/platform-macOS%20only-lightgrey)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

> **仅支持 macOS。** 服务管理依赖 macOS 专有的 launchd，文件监听依赖 `fswatch`，
> 构建产物针对 `arm64` / `x86_64` 的 Darwin。Linux 与 Windows **尚未支持**，
> 也没有兼容层 —— 在这两个平台上安装脚本会直接失败，这是当前状态的如实描述。

Claude Code / Codex 轨迹采集工具。默认通过一个统一执行入口同时采集两类数据：Claude Code 走 HTTP 统一采集器 + Hooks，Codex 走本机 `~/.codex` rollout watcher + app-server 线程读取，统一输出 SWE-agent 兼容的 `.traj` 格式。

## 目录

- [快速开始](#快速开始)
- [这个工具会采集什么](#这个工具会采集什么)
- [数据会离开你的机器吗](#数据会离开你的机器吗)
- [工作原理](#工作原理)
- [安装](#安装)
  - [从 GitHub Release 安装（推荐）](#从-github-release-安装推荐)
  - [从源码安装（开发用）](#从源码安装开发用)
  - [升级](#升级)
- [使用方式](#使用方式)
  - [方式一：源码临时启动](#方式一源码临时启动)
  - [方式二：守护进程模式（推荐日常使用）](#方式二守护进程模式推荐日常使用)
  - [方式三：手动分步启动](#方式三手动分步启动)
  - [方式四：仅统一采集器模式（不配置 Hooks）](#方式四仅统一采集器模式不配置-hooks)
  - [方式五：导入 Codex 本地会话](#方式五导入-codex-本地会话)
- [配置说明](#配置说明)
  - [统一采集器参数](#统一采集器参数)
  - [Hooks 配置](#hooks-配置)
  - [第三方 API 统一采集器上游](#第三方-api-统一采集器上游)
- [渠道管理](#渠道管理)
- [数据目录结构](#数据目录结构)
- [数据处理管道](#数据处理管道)
  - [第零步（可选）：重建历史轨迹](#第零步可选重建历史轨迹)
  - [历史数据修复：会话复活截断 + 超大 traj](#历史数据修复会话复活截断--超大-traj)
  - [第一步：过滤](#第一步过滤)
  - [第二步：格式转换](#第二步格式转换)
  - [第三步：合并](#第三步合并)
  - [双通道数据合并](#双通道数据合并)
- [文件说明](#文件说明)
- [项目文档](#项目文档)
- [常见问题](#常见问题)
- [停止与清理](#停止与清理)

---

## 快速开始

**最终用户（推荐）：从 GitHub Release 安装。** 不需要 clone，不需要 Python 依赖。

```bash
curl -fsSL https://github.com/njfuzrs/claude-trace/releases/latest/download/install.sh | bash
```

安装器会询问 API Token，部署 Hooks，写入 `~/.claude/settings.json`，并拉起 launchd 服务。数据落到 `~/.claude-trace/trajectories/sessions/`。上传默认关闭，不填即不上传。之后：

```bash
claude-trace status
claude-trace logs
claude-trace version    # 同时打印版本号、二进制 sha256、mtime
```

目前只提供 macOS arm64 的预编译包。Intel Mac 请走下面的源码路径，或等对应架构的 Release。

**开发者：从源码装。** 仓库内跑 `bash dist/install.sh` 会拷 `dist/$ARCH/` 里刚构建的二进制，**不会**去 GitHub 下载。没有预构建二进制时会退回 `python3 trace_agent.py` 包装。

```bash
git clone https://github.com/njfuzrs/claude-trace.git
cd claude-trace
pip3 install -r requirements.txt
./build/build.sh                 # 产出 dist/arm64/claude-trace-proxy
bash dist/install.sh             # 本地安装模式
```

也可以不装到 `~/.claude-trace/`，直接从仓库跑守护进程：

```bash
./install-daemon.sh install            # 注册 launchd（开发入口，走 python3 trace_agent.py）
./install-daemon.sh install --watch    # 额外装文件监听（改 .py 自动重启；生产二进制模式不要装）
# 或
./proxy-daemon.sh                      # 前台跑，Ctrl-C 停
```

安装器会自动部署 Hooks、写入 `~/.claude/settings.json`，并立即拉起统一采集服务。配置完成后，以后每次正常启动 `claude` 即可，Claude 请求会自动经过本地统一采集器；同时统一守护进程也会默认持续监听本机 `~/.codex`，增量采集 Codex 会话到 `./trajectories/` 目录，无需额外配置。

> **核心原则：统一采集进程永不中断。** Claude Code 请求必须经过本地统一采集器（`localhost:4000`）转发到上游 API；Codex 会话则由同一守护进程默认持续监听 `~/.codex/sessions/**/rollout-*.jsonl`。统一进程通过 macOS launchd 服务管理，开机自启 + 崩溃自动重启，确保采集不丢数据。

---

## 这个工具会采集什么

**它采集完整对话，不是摘要。** 这是采集类工具的知情同意底线，所以说清楚：

| 采集内容 | 说明 |
| --- | --- |
| 完整请求体 | 你的每一条提示词、system prompt、全部历史消息 |
| 完整响应体 | 模型回复，含 thinking 内容与 token 用量 |
| 工具调用与结果 | `Read` / `Edit` / `Bash` 的**入参与输出** —— 也就是你的**源码内容、文件路径、命令与命令输出** |
| 文件路径 | 绝对路径，含你的用户名目录（如 `/Users/<你>/...`） |
| git 状态 | 会话起点的 HEAD、分支名、工作区是否有未提交改动（`git_state.py`，只读命令） |
| 会话事件 | SessionStart / Stop / PostToolUse 等 hook 事件时间线 |

**不采集**：你的 API Key 值（三个请求头会被截断脱敏，见[安全说明](#安全说明请如实理解脱敏范围)）。

⚠️ 反过来说：**除了那三个请求头，消息体不做内容级过滤**。你在对话里粘贴过什么，
轨迹里就有什么。在私有代码库上使用前请先明确这一点。

想知道究竟落了什么盘，直接看：

```bash
# 打包安装（路径 A）
ls ~/.claude-trace/trajectories/sessions/
python3 tools/viewer.py ~/.claude-trace/trajectories/sessions/<id>/session.traj

# 源码 / 开发守护进程（路径 B，默认 ./trajectories）
ls ./trajectories/sessions/
python3 tools/viewer.py ./trajectories/sessions/<id>/session.traj
```

---

## 数据会离开你的机器吗

**默认不会。** 所有轨迹只写在本机 `./trajectories/`（或 `$OUTPUT`）目录下，
本项目不含任何内置上传端点或内置凭据。

上传是 **opt-in** 的，需要你**同时**配置两个值才会启用：

| 变量 | 作用 |
| --- | --- |
| `TRAJ_PLATFORM_URL` | 你自己的接收服务器地址 |
| `TRAJ_UPLOAD_TOKEN` | 该服务器的上传凭据 |

任一为空即禁用上传（判据见 `proxy.py` 的 `DataCollector.__init__`），
未启用时日志会打印「上传未配置，数据仅保存在本地」。

采集进程内的自动上传由 `uploader.py` 负责。`uploader` 没跑时（历史积压、
其它机器上的目录）用 `tools/sync.py` 手动批量，**同一对变量必填**，同样
无内置端点。身份字段规则也相同：空则不上报。

```bash
export TRAJ_PLATFORM_URL=...     # 必填
export TRAJ_UPLOAD_TOKEN=...     # 必填
python3 tools/sync.py                  # 增量
python3 tools/sync.py --all            # 全量
```

另有两个身份字段，**默认留空即不上报**，不会回退到你的系统用户名或主机名：

| 变量 | 默认 |
| --- | --- |
| `TRAJ_USER_ID` | 空 |
| `TRAJ_DEVICE_ID` | 空 |

行为开关：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TRAJ_CLEANUP_AFTER_UPLOAD` | `false` | 上传成功后是否删除本地数据。**默认保留** —— 只有显式设为 `true` 才删。曾经默认 `true`，结果「上传成功」的判断一旦有偏差，数据就没了第二份 |
| `TRAJ_BACKFILL_ON_START` | `true` | 启动时扫描并补传盘上未上云的会话。这是上传链路的兜底，不建议关 |

自检当前是否处于「只存本地」状态：

```bash
./install-daemon.sh status
grep -E '上传未配置|可靠上传已启用' /tmp/claude-trace-proxy.log | tail -3
```

排查上传是否静默失效（关键：看「轨迹仍未上云」这个数字）：

```bash
# 打印积压状态后退出，不启动采集器（两个入口都支持，输出一致）
python3 trace_agent.py --output ~/.claude-trace/trajectories --upload-status

# 打包部署的场景用二进制查
~/.claude-trace/bin/claude-trace-proxy \
    --output ~/.claude-trace/trajectories --upload-status

# 采集器在跑时也可以直接查
curl -s http://127.0.0.1:4000/_internal/health | python3 -m json.tool
```

积压不为 0 且长期不降，说明上传链路有问题 —— 数据还在本地，用
`tools/recover_truncated.py` 排查修复（见下文「历史数据修复」）。

服务端协议（自建接收端所需）见 `docs/upload-protocol.md`。

---

## 工作原理

```
Claude Code
    │
    ├── API 请求 ──→ HTTP 统一采集器 (localhost:4000) ──→ 上游 API
    │                    │
    │                    └── 记录: 请求体 / SSE 响应 / token 用量
    │
    └── 生命周期事件 ──→ Hooks 采集器
                         │
                         └── 记录: session_id / 用户 prompt / 工具调用 / sub-agent
```

- **通道 A（HTTP 统一采集器）**：通过 `ANTHROPIC_BASE_URL` 环境变量劫持 API 请求，SSE Tee 模式零延迟转发 + 后台记录
- **通道 B（Hooks）**：通过 Claude Code 原生 hooks 机制采集会话事件（session 生命周期、用户 prompt、sub-agent 等）
- 两个通道通过 `session_id` 自动关联合并

Codex 不能直接复用 Claude 的 HTTP 统一采集器方案。统一守护进程会默认监听本机 `~/.codex/`，并在需要时结合 app-server + rollout + sqlite 做增量刷新；手工导入模式仍保留，便于补历史数据。Codex 数据链路如下：

```
Codex CLI
    │
    ├── app-server(thread/list, thread/read)
    │        └── 官方语义线程结构：turn / item / source / sub-agent
    │
    ├── ~/.codex/sessions/.../rollout-*.jsonl
    │        └── 原始事件流：旧版本工具调用、developer 指令、token 统计
    │
    └── ~/.codex/state_5.sqlite + logs_1.sqlite
             └── 线程索引 / 父子线程关系 / 异常日志兜底
```

- **主通道（app-server）**：优先读取官方 `thread/read(includeTurns=true)` 结果，拿到结构化 turns/items
- **补通道（rollout.jsonl）**：补 developer/system 指令、token 用量，并兼容旧版会话缺失的工具 item
- **异常兜底（sqlite）**：保留 `thread_spawn_edges`、失败日志、索引信息，减少遗漏

---

## 安装

要求：macOS。预编译包目前只打 arm64；源码路径需要 Python 3.10 或更高。

### 从 GitHub Release 安装（推荐）

```bash
curl -fsSL https://github.com/njfuzrs/claude-trace/releases/latest/download/install.sh | bash

# 指定版本
curl -fsSL https://github.com/njfuzrs/claude-trace/releases/download/v0.3.0/install.sh | bash
```

非交互：

```bash
ANTHROPIC_AUTH_TOKEN=sk-... \
  curl -fsSL https://github.com/njfuzrs/claude-trace/releases/latest/download/install.sh \
  | bash -s -- --non-interactive
# 必填：ANTHROPIC_AUTH_TOKEN
# 可选：UPSTREAM_URL / TRAJ_PLATFORM_URL / TRAJ_UPLOAD_TOKEN / PORT
```

### 从源码安装（开发用）

必须先构建，否则安装器会退回 Python 包装，而不是装二进制：

```bash
git clone https://github.com/njfuzrs/claude-trace.git
cd claude-trace
pip3 install -r requirements.txt
./build/build.sh
bash dist/install.sh
```

仓库内且未设置 `RELEASE_BASE` 时，安装器走本地拷贝，不会去 GitHub 下载。
显式设置 `RELEASE_BASE` 则强制走远程（用于模拟 Release 安装）。

### 升级

同一条 `curl | bash` 就是升级。安装器先把新包落到临时目录并校验，成功后再停旧服务、再替换 `~/.claude-trace/`，远程 404 时不停正在跑的采集。`channels.json` 和 `trajectories/` 会保留。

```bash
curl -fsSL https://github.com/njfuzrs/claude-trace/releases/latest/download/install.sh | bash
claude-trace version    # 对一下 version 文件、二进制 --version、sha256
```

版本号相同时仍可能换了二进制（曾经同一个 `0.2.0` 对应过行为差 13 天的两份包）。以 `claude-trace version` 打出来的 sha256 / 自报为准，不要只看数字。

开发机不要对正在跑的生产二进制装 `install-daemon.sh install --watch`：文件监听会重启 `~/.claude-trace` 里的旧包，还会把活会话按超时切碎。

---

## 使用方式

日常使用走上面的 [安装](#安装)：`curl | bash` 一次，之后直接开 `claude`。下面三种是源码路径，给开发或临时调试用。

### 方式一：源码临时启动

把采集器和 Claude Code 绑在同一次终端会话里，适合还没装 launchd、只想立刻试一下的时候。

```bash
./start.sh
```

它会依次执行：
1. 检查 aiohttp 依赖
2. 部署 `collector.py` 到 `~/.claude/hooks/`
3. 调用 `setup_hooks.py` 写入 hooks（**不**改 `ANTHROPIC_BASE_URL`）
4. **杀掉占用 `$PORT`（默认 4000）的进程**，再后台启动 `python3 trace_agent.py`
5. 前台启动 Claude Code，只给这一次进程设置 `ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT`

Claude Code 退出后，这次拉起的采集器也会停。数据写在 `./trajectories/`（可用 `--output` 改），不是 `~/.claude-trace/trajectories/`。

不要在已经用 `curl | bash` / launchd 采集的机器上跑它：第 4 步会把正在跑的二进制采集器杀掉，当前对话跟着断。本机正在采集时请继续用 `claude-trace status`，不要执行 `./start.sh`。

**自定义参数：**

```bash
./start.sh --port 5000              # 自定义端口（仍会清掉该端口上的进程）
./start.sh --output /data/traces    # 自定义输出目录
./start.sh --proxy-only             # 只启动统一采集器，不启动 Claude Code
./start.sh --upstream https://your-api.example.com
./start.sh --force-thinking 1
./start.sh -- -p "hello"            # -- 之后的参数传给 claude
```

### 方式二：守护进程模式（推荐日常使用）

统一采集进程常驻后台，开机自启 + 崩溃自动重启。配置一次后无需再管。

**方法 A：launchd 自启动（推荐 macOS）**

最终用户：

```bash
curl -fsSL https://github.com/njfuzrs/claude-trace/releases/latest/download/install.sh | bash
```

开发者（仓库内，需先 `./build/build.sh`）：

```bash
bash dist/install.sh
```

安装器会自动部署 Hooks、配置 `~/.claude/settings.json`，并启动统一采集服务。重启电脑后也会自动拉起，无需手动操作。默认行为是：

- Claude Code：通过本地统一采集器采集
- Codex：自动监听 `~/.codex/sessions/**/rollout-*.jsonl` 并增量刷新

```bash
# 管理命令
claude-trace status       # 查看状态
claude-trace restart      # 重启服务（bootout + bootstrap，会重读 plist）
claude-trace version      # 版本文件 + 二进制 --version + sha256
claude-trace uninstall    # 卸载服务
```

自定义参数通过环境变量传入：

```bash
# 自定义上游和端口（非交互）
PORT=5000 UPSTREAM_URL=https://api.anthropic.com \
  ANTHROPIC_AUTH_TOKEN=sk-... bash dist/install.sh --non-interactive
```

**方法 B：手动 nohup 启动**

```bash
# 启动守护进程（终端关闭后继续运行，但重启后需要重新启动）
UPSTREAM=https://api.anthropic.com nohup ./proxy-daemon.sh > /dev/null 2>&1 &
```

方法 A（`install.sh` / `claude-trace start`）会自己写 `ANTHROPIC_BASE_URL`。方法 B 和源码守护进程需要你自己改 `~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"
  }
}
```

配置完成后，以后每次正常启动 `claude` 即可，所有请求自动经过统一采集器采集；同一守护进程也会默认持续采集 Codex，无需额外配置。

支持的环境变量：

| 变量 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `PORT` | 4000 | 统一采集器监听端口 |
| `UPSTREAM` | `https://api.anthropic.com` | 上游 API 地址 |
| `OUTPUT` | `./trajectories` | 轨迹数据输出目录 |
| `FORCE_THINKING` | 0 | 非 0 时强制 thinking effort=max |

**停止守护进程：**

```bash
# launchd 方式
claude-trace uninstall

# nohup 方式：删除 PID 文件后 kill
rm /tmp/claude-trace-proxy.pid && kill $(lsof -ti :4000)
```

### 方式三：手动分步启动

适合调试或自定义场景。

```bash
# 终端 1：启动统一采集器
python3 trace_agent.py --port 4000 --output ./trajectories --verbose

# 终端 2：配置 Hooks（首次使用）
python3 setup_hooks.py

# 终端 2：通过统一采集器启动 Claude Code
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 claude
```

### 方式四：仅统一采集器模式（不配置 Hooks）

如果不想配置 Hooks，统一采集器也可以独立工作。会话识别退回到对话内容连续性匹配（准确度略低）。

```bash
python3 trace_agent.py --port 4000 --output ./trajectories --verbose
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 claude
```

此模式下缺少的数据：session_id（自动生成 UUID 替代）、用户原始 prompt、sub-agent 生命周期、context compaction 详情。

### 方式五：导入 Codex 本地会话

Codex 不走 Claude 的 HTTP 统一采集器链路。要尽可能完整地采集 Codex 对话，直接读取本机 `~/.codex` 数据并导出到统一 `.traj` 格式：

```bash
# 导出指定线程
python3 import_codex.py --thread-id <thread_id> --output ./trajectories/sessions

# 导出当前可见的全部线程
python3 import_codex.py --all --output ./trajectories/sessions

# 扫描 rollout 增量变化并导出一次（适合测试 / cron）
python3 import_codex.py --once --output ./trajectories/sessions

# 常驻 watcher：监听 ~/.codex/sessions/**/rollout-*.jsonl 的新增和追加
python3 import_codex.py --watch --output ./trajectories/sessions

# 安装 macOS launchd 守护进程
./install-codex-daemon.sh install
./install-codex-daemon.sh status
./install-codex-daemon.sh restart
./install-codex-daemon.sh uninstall
```

导出结果：

```text
trajectories/sessions/{thread_id}/
├── session.traj
├── codex_thread.json
├── rollout.jsonl
├── state_logs.jsonl
└── feedback_logs.jsonl
```

说明：

- `session.traj`：统一后的轨迹文件，已将 Codex `exit_status` 归一为 `end_turn / user_interrupt / error / partial`
- `codex_thread.json`：`thread/read` 返回的官方线程结构原文
- `rollout.jsonl`：原始事件流备份，便于后续补解析和回放
- `state_logs.jsonl` / `feedback_logs.jsonl`：sqlite 中按 `thread_id` 抽出的异常与反馈日志
- watcher 会监听 `~/.codex/sessions/**/rollout-*.jsonl` 的新增与追加，并按同一个 `thread_id` 增量刷新导出目录
- 每次刷新都会同步维护 `codex_thread.json / rollout.jsonl / state_logs.jsonl / feedback_logs.jsonl`，不只保留转换后的 `session.traj`
- watcher 状态默认保存在 `trajectories/sessions/.codex_watch_state.json`，重启后只补采新增或变化的 rollout 文件
- 为了尽量减少 sqlite 延迟带来的漏采，watcher 会在 rollout 变化后做一次快速导出，并在安静期后再做一次确认导出

---

## 配置说明

### 统一采集器参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | 4000 | 统一采集器监听端口 |
| `--host` | 127.0.0.1 | 监听地址（默认只监听本地，防止局域网访问） |
| `--output` | ./trajectories | 轨迹数据输出目录 |
| `--upstream` | https://api.anthropic.com | 上游 API 地址 |
| `--session-timeout` | 1800 | 会话超时时间（秒），超时后自动导出 .traj。默认 30 分钟：设太短（曾是 300s）会让「思考几分钟再提问」被判为过期，新建会话时 index 从 1 重来，轨迹被截断 |
| `--upload-status` | - | 打印上传积压状态后退出，不启动采集器。用于排查上传是否静默失效 |
| `--save-raw` | false | 额外把每轮请求/响应写成 `raw/NNN_request.json`。默认关：`raw.jsonl` 已有全部数据 |
| `--no-save-raw` | - | 显式关闭上面这项（默认行为） |
| `--version` | - | 打印版本号与构建指纹后退出。打包后的二进制读的是打进包内的 `version` 文件 |
| `--events-dir` | ~/.claude/trajectory_events | Hooks 事件数据目录 |
| `--force-thinking` | 0 | 非 0 时将 adaptive thinking 的 effort 改写为 max，提高 thinking blocks 产生概率（仅 Opus 4.6） |
| `--verbose` | false | 详细日志输出 |

### Hooks 配置

```bash
python3 setup_hooks.py              # 写入 ~/.claude/settings.json（全局）
python3 setup_hooks.py --local      # 写入 .claude/settings.local.json（当前项目）
python3 setup_hooks.py --show       # 只打印配置，不写入
python3 setup_hooks.py --remove     # 移除已配置的 hooks
python3 setup_hooks.py --collector /path/to/collector.py  # 指定 collector 路径
```

订阅的事件：

| 优先级 | 事件 | 采集内容 |
|--------|------|---------|
| P0 | SessionStart | session_id、model、启动方式 |
| P0 | SessionEnd | 会话结束原因 |
| P0 | UserPromptSubmit | 用户原始 prompt |
| P0 | Stop | turn 结束标记 |
| P1 | PostToolUse | 工具调用输入/输出 |
| P1 | SubagentStart / SubagentStop | sub-agent 生命周期 |
| P1 | PostCompact | context compaction 摘要 |
| P2 | PreToolUse | 工具调用前参数 |
| P2 | PermissionRequest | 权限交互 |
| P2 | InstructionsLoaded | CLAUDE.md 加载 |
| P2 | StopFailure | API 错误详情 |

### 第三方 API 统一采集器上游

如果你使用第三方 API 统一采集器上游（而非 Anthropic 官方 API），需要将 `--upstream` 设置为你的上游地址：

```bash
# 手动启动
python3 trace_agent.py --upstream https://your-proxy.com

# 或修改 proxy-daemon.sh 中的 --upstream 参数
# 或修改 start.sh 的 UPSTREAM 变量
UPSTREAM=https://your-proxy.com ./start.sh
```

同时确保 `~/.claude/settings.json` 中的 `ANTHROPIC_BASE_URL` 指向本地统一采集器：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"
  }
}
```

请求链路：`Claude Code → localhost:4000（统一采集器采集）→ your-proxy.com → Anthropic API`

---

## 渠道管理

如果你需要在多个 API 渠道（不同服务商、不同额度的账号等）之间切换：

- 源码开发：`./switch-channel.sh`
- 打包安装：`claude-trace switch`（读的是 `~/.claude-trace/channels.json`）

两条路径都会改 plist 并 `bootout + bootstrap` 重启采集器，让新的 `UPSTREAM` 生效。不要用 `launchctl kickstart`。

```bash
# 源码
./switch-channel.sh list       # 列出所有可用渠道
./switch-channel.sh default    # 切换到名为 default 的渠道
./switch-channel.sh status     # 查看当前渠道

# 安装版
claude-trace switch list
claude-trace switch default
claude-trace switch status
```

切换时会同时更新：`ANTHROPIC_AUTH_TOKEN`（token）、统一采集器上游地址（`UPSTREAM`）、`FORCE_THINKING` 参数，并自动重启统一采集器。

**渠道配置文件 `channels.json`**（复制 `channels.json.example` 创建）：

```json
{
  "channels": {
    "default": {
      "name": "默认渠道",
      "token": "sk-xxx",
      "upstream": "https://api.anthropic.com",
      "force_thinking": 0
    },
    "backup": {
      "name": "备用渠道",
      "token": "sk-xxx",
      "upstream": "https://api.anthropic.com",
      "force_thinking": 1
    }
  }
}
```

新增渠道只需在 `channels.json` 中添加 key，无需修改任何脚本。

> `channels.json` 含有 API Key，已加入 `.gitignore`，不会提交到 git。

---

## 数据目录结构

当前布局按会话目录组织（一个 session 的 traj / raw / events 放一起）。打包安装写在 `~/.claude-trace/trajectories/`，源码守护进程默认写在仓库的 `./trajectories/`。

```
trajectories/
└── sessions/
    └── {session_id}/
        ├── session.traj     # 构建好的轨迹
        ├── raw.jsonl        # 增量请求/响应（始终写）
        ├── events.jsonl     # Hook 事件（有的话）
        └── raw/             # 仅 --save-raw 时才有：每轮 NNN_request.json / NNN_response.json
~/.claude/trajectory_events/{session_id}.jsonl   # Hooks 原始事件（通道 B，合并前）
```

旧布局 `trajectories/raw/` + `trajectories/traj/` 仍能被 `tools/viewer.py` / `tools/filter_trajs.py` 读到，新采集不再往那里写。

Codex 离线导出输出：

```text
trajectories/
└── sessions/
    └── {thread_id}/
        ├── session.traj
        ├── codex_thread.json
        ├── rollout.jsonl
        ├── state_logs.jsonl
        └── feedback_logs.jsonl
```

**JSONL 格式说明：**

- 第一行保存完整 `request_body`（含 system prompt），后续行只保存增量 `new_messages`
- 避免 Claude Code 每次重发完整历史导致的 O(n²) 存储膨胀

**.traj 文件结构：**

```json
{
  "trajectory": [                    // TAO 步骤列表
    {"message_type": "action", "tool_name": "Bash", "tool_input": {...}, ...},
    {"message_type": "observation", "content": "...",
     "exit_code": 1,                 // 数值退出码；无法确定时为 null
     "status": "failure",            // success / failure / rejected
                                     // / interrupted / timeout / invalid_input / unknown
     "exit_code_source": "exit_code_prefix",   // 该结论的依据
     "is_error": true, ...}
  ],
  "history": [...],                  // 完整 LLM 对话历史（用于 SFT 训练）
  "info": {                          // 会话统计
    "model_stats": {"tokens_sent": ..., "tokens_received": ..., "api_calls": ..., "total_cost_usd": ...},
    "exit_status": "end_turn",
    "has_thinking": true,
    "data_quality": {
      "failed_actions": 2,           // 非 success 的工具执行数
      "exit_codes_known": 2,         // 拿到确定数值退出码的步骤数
      "failure_rate": 0.5,
      "has_git_state": true
    }
  },
  "metadata": {                      // 扩展元数据（含 Hooks 数据）
    "session_id": "...",
    "model": "claude-opus-4-6",
    "tools_used": ["Bash", "Read", "Write"],
    "user_prompts": ["..."],
    "subagent_spans": [...],
    "compactions": [...],
    "git_head": "c9f5e608963d...",   // 会话起点的 HEAD（扁平字段便于筛选）
    "git_branch": "master",
    "git_dirty": true,               // 工作区是否有未提交改动；null = 未采到
    "git_state": {...},              // 起点完整快照（见下）
    "git_state_end": {...}           // 终点完整快照
  }
}
```

**git 状态与退出码（为 benchmark 构造服务）**

这两组字段的共同点是「采集时点不记录，事后就再也拿不到」：

- `git_head` + `git_dirty` 决定 `base_commit` 是否可信。靠会话时间反查 commit
  在多分支仓库上很脆弱（未合并分支、squash 合并、多 worktree 都会让时间反查
  落到主线上不存在的中间状态）；而工作区脏不脏事后完全无从得知 —— 脏工作区
  意味着 HEAD 根本不代表会话的真实起点。
- `exit_code` + `status` 提供「改前失败 / 改后通过」的直接证据。

`git_state` / `git_state_end` 的完整快照字段：

| 字段 | 说明 |
|---|---|
| `head` / `head_short` | HEAD 的完整 sha 与短 sha |
| `branch` / `detached` | 分支名；detached HEAD 时 `branch` 为空、`detached` 为 true |
| `dirty` | 工作区是否有未提交改动（含未跟踪文件）；`null` 表示未采到 |
| `modified_files` / `untracked_files` / `staged_files` | 各类改动计数 |
| `dirty_file_list` | 脏文件明细 `{status, path}`，上限 50 条 |
| `upstream` / `ahead` / `behind` | 上游分支及领先/落后提交数，用于判断 HEAD 是否已推送 |
| `is_linked_worktree` | 是否为 linked worktree（非主工作树） |
| `source` | `hook`（SessionStart 时点采集，更准）或 `proxy`（代理侧兜底） |

`exit_code` 的推导依据（`exit_code_source`）：

| 取值 | 含义 |
|---|---|
| `exit_code_prefix` | tool_result 首行的 `Exit code N`，最强信号 |
| `no_error_shell` | Bash 类工具无错误且无前缀 → 退出 0 |
| `no_error_nonshell` | Read/Edit 等无退出码概念的工具，仅标注成功 |
| `is_error_flag` | 确定失败，但退出码无从得知 |
| `rejection_text` / `timeout_text` / `interrupt_text` | 命令未真正执行（被拒绝/超时/中断），`exit_code` 为 null |
| `tool_use_error` | 工具调用本身不合法（InputValidationError 等） |
| `orphan` | tool_result 缺失，状态 `unknown`；下游做判定时应排除 |

> Claude Code 本身不暴露数值退出码 —— PostToolUse 的 `tool_response` 只有
> `stdout`/`stderr`/`interrupted`/`isImage`/`noOutputExpected`，transcript 的
> `toolUseResult` 同样没有退出码字段。因此退出码在 `builder.py` 侧按上表推导，
> 并用 `exit_code_source` 标明依据，供下游按可信度筛选。

**安全说明（请如实理解脱敏范围）：**

- **已脱敏**：请求头中的 `x-api-key`、`authorization`、`proxy-authorization`
  三个字段，落盘时截断为前 10 字符 + `***`（实现见 `proxy.py` 的
  `SENSITIVE_HEADERS` / `sanitize_headers_for_storage`）。
- 🔴 **未脱敏**：**消息体不做任何内容级过滤**。轨迹会原样记录完整对话，
  因此如果你在对话里粘贴过 token、私钥、`.env` 内容或数据库连接串，
  它们会**明文落盘**在 `raw.jsonl` / `session.traj` 里。
- 分享或上传轨迹前请自查：

  ```bash
  # 在轨迹目录里粗筛常见凭据形态
  grep -rlnE 'sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{12,}|BEGIN [A-Z ]*PRIVATE KEY' \
    ./trajectories/sessions/ 2>/dev/null
  ```

- 内容级脱敏器（Scrubber）在路线图上，尚未实现。在它落地之前，
  **请把轨迹目录当作与源码同等敏感的数据来对待。**
- 统一采集器默认绑定 `127.0.0.1`，不暴露到局域网。

---

## 数据处理管道

采集完成后，可以通过三步管道将 `.traj` 转换为 SFT 训练数据：

```
.traj → 过滤 → 格式转换 → 合并 → training_data.jsonl
```

### 第零步（可选）：重建历史轨迹

早期版本的构建逻辑存在几个缺陷（tool_result 查找窗口过小导致 orphan、
`files_edited` 恒空、history 中 tool_result 重复、新模型成本算成 0），
且 `count_tokens` 探测请求曾被误当成真实会话采集。原始数据是完整的，
重跑构建即可修复：

```bash
# 先看会做什么（不写文件）
python3 tools/rebuild_trajs.py --dir trajectories/sessions --dry-run

# 重建（原文件备份为 session.traj.bak）+ 隔离垃圾目录到 _trash/
python3 tools/rebuild_trajs.py --dir trajectories/sessions --quarantine-garbage
```

含 sub-agent 子会话的会话会被自动跳过：子会话的 pair 只在导出时合并进
`session.traj`，从未单独落盘到 `raw.jsonl`，重建会丢数据。新采集的会话不受影响。

### 历史数据修复：会话复活截断 + 超大 traj

`tools/recover_truncated.py` 修的是两类历史损坏，都可重复运行、可先 `--dry-run`：

**一、会话复活截断。** 早期 `--session-timeout` 默认 300s，用户思考/开会超过
5 分钟，会话就被判过期清理；再提问时代理新建一个空 Session，`index` 从 1 重来，
而 `save_trajectory` 是覆盖写 —— 几小时的完整轨迹被只含最后几步的短轨迹冲掉，
且那份残缺版本还先上传过一次，被服务端 409 幂等锁死在云端。
`raw.jsonl` 是 append 写入、历史完好，所以全部可重建。

**二、超大 traj 传不上云。** 老版 builder 把 user message 里的 `tool_result`
原样留在 history，而 Claude Code 每轮重发完整历史 —— 同一个 `tool_result`
被写进 history 几百次。实测一个会话 42105 条 history 只有 2362 条唯一，
traj 涨到 210MB，超过网关上限（约 64MB）永远传不上去。
`dedup` 只删逐字节完全相同的重复条目，并校验 `tool_use_id` 集合与
trajectory 步数不变，是无损的。

```bash
S=~/.claude-trace/trajectories/sessions

python3 tools/recover_truncated.py --dir $S scan      # 只报告：哪些坏了、能恢复多少
python3 tools/recover_truncated.py --dir $S rebuild   # 用 raw.jsonl 重建（备份为 .traj.bak）
python3 tools/recover_truncated.py --dir $S dedup     # 无损去重超大 traj（备份为 .traj.predup）
python3 tools/recover_truncated.py --dir $S reupload  # 带 force=true 覆盖云端残缺版本
python3 tools/recover_truncated.py --dir $S purge     # 清理残留 .gz 和已上云的死队列项

python3 tools/recover_truncated.py --dir $S all       # 一条龙（建议先跑 scan）
```

`scan` 报告的「有复活截断指纹」是历史事实，修完也还在（指纹就在 `raw.jsonl` 里）；
判断修没修看的是「已重建 / 待重建」两行。

### 第一步：过滤

```bash
python3 tools/filter_trajs.py \
    --input trajectories/sessions/ \
    --output filtered/ \
    --min-steps 3 \
    --max-steps 100 \
    --require-end-turn \
    --require-tool-use \
    --report                    # 输出质量指标报告
```

| 参数 | 说明 |
|------|------|
| `--min-steps N` | 最少 N 个 TAO 步骤（过滤无效会话），默认 3 |
| `--max-steps N` | 最多 N 个步骤（过滤死循环），0=不限 |
| `--require-end-turn` | 必须正常结束；兼容 `end_turn` 和历史 Codex `completed` |
| `--require-tool-use` | 必须包含工具调用（过滤纯对话） |
| `--report` | 输出质量指标报告 |

### 第二步：格式转换

```bash
python3 tools/convert_trajs.py \
    --input filtered/ \
    --output sft/ \
    --style xml
```

三种输出格式：

| 格式 | 说明 | 适用场景 |
|------|------|---------|
| `xml` | `<function=name><parameter=k>v</parameter></function>` | SWE-agent-LM 默认，推荐 |
| `tool` | 原始 function calling 格式透传 | 支持 tool_calls 的模型 |
| `messages` | 通用 messages 格式 | 通用 SFT |

### 第三步：合并

```bash
python3 tools/combine_trajs.py \
    --input sft/ \
    --output training_data.jsonl \
    --max-per-session 3 \
    --shuffle \
    --seed 42
```

| 参数 | 说明 |
|------|------|
| `--max-per-session N` | 每个会话最多保留 N 条，0=不限 |
| `--shuffle` | 随机打乱 |
| `--seed N` | 随机种子 |

### 双通道数据合并

如果同时使用了统一采集器和 Hooks，可以用 `merger.py` 将两个通道的数据合并为增强版 `.traj`：

```bash
# 合并指定会话
python3 merger.py --session-id <session_id>

# 合并所有会话
python3 merger.py --all

# 自定义目录（新布局：raw / events / traj 都在 sessions/{id}/ 下）
python3 merger.py --all \
    --raw-dir ./trajectories/sessions \
    --events-dir ./trajectories/sessions \
    --output ./trajectories/sessions
```

合并后的 `.traj` 会包含 Hooks 提供的额外信息：用户原始 prompt、sub-agent 生命周期、compaction 摘要等。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `proxy.py` | HTTP 统一采集器服务器。日常入口是 `trace_agent.py`；本文件的 `main` 留给单测和 `--upload-status` |
| `trace_agent.py` | Claude + Codex 统一采集入口，默认同时启动 HTTP 统一采集器与 Codex watcher |
| `version_info.py` | 版本号解析：仓库 `version` 文件 → 打包后的 `sys._MEIPASS` |
| `builder.py` | 轨迹构建器，将请求/响应对转换为 .traj 格式 |
| `collector.py` | Hooks 采集脚本，部署到 `~/.claude/hooks/` |
| `setup_hooks.py` | 自动配置 settings.json 的 hooks |
| `merger.py` | 双通道数据合并器（运行时 lazy import；也可作 CLI） |
| `uploader.py` | 可靠上传管理器：gzip + SHA256 校验、指数退避重试、持久化队列、启动补传 |
| `tools/sync.py` | 手动批量补传（uploader 没跑时）。同一对 URL/token 必填，身份不回退系统用户名 |
| `tools/rebuild_trajs.py` | 用当前 builder 重建历史 .traj，并识别/隔离垃圾会话目录 |
| `tools/recover_truncated.py` | 历史数据修复：会话复活截断重建 + 超大 traj 无损去重 + 重新上云 |
| `tools/filter_trajs.py` | 轨迹过滤器 |
| `tools/convert_trajs.py` | 格式转换（.traj → SFT .jsonl） |
| `tools/combine_trajs.py` | 合并 + shuffle SFT 数据 |
| `tools/migrate_storage.py` | 旧布局 `raw/`+`traj/` → `sessions/` |
| `start.sh` | 源码临时启动：采集器与 Claude Code 同一次会话。会清掉 `$PORT` 上已有进程，日常采集不要用 |
| `proxy-daemon.sh` | 自动重启的守护进程脚本 |
| `dist/install.sh` | 一键安装器。仓库内走本地拷贝；`curl \| bash` 走 GitHub Releases |
| `dist/claude-trace` | 安装后的统一 CLI 入口，用于 `start/status/restart/logs/uninstall/version` |
| `install-daemon.sh` | 开发用 launchd 安装脚本（走 `python3 trace_agent.py`）。`--watch` 才装文件监听 |
| `version` | 版本号单一事实源。改版本用 `./build/release.sh --bump x.y.z` |
| `build/build.sh` | PyInstaller 打包，产出 `dist/{arch}/claude-trace-proxy` |
| `build/release.sh` | 打包 + 可选上传 GitHub Releases。`--bump` 同步改 version / pyproject / CHANGELOG |
| `import_codex.py` | Codex 手工导入 + rollout watcher 持续采集 |
| `codex-daemon.sh` | 自动重启的 Codex watcher 守护进程脚本 |
| `install-codex-daemon.sh` | 安装/卸载 Codex watcher 的 launchd 自启动服务（macOS） |
| `switch-channel.sh` | 快速切换 API 渠道（同步更新 token + 统一采集器上游） |
| `channels.json` | 渠道配置文件，含 token/upstream/force_thinking（已加入 .gitignore） |
| `channels.json.example` | 渠道配置模板，可提交到 git |
| `tools/viewer.py` | 轨迹数据 HTML 查看器，将 .traj 转为可视化 HTML |
| `git_state.py` | 会话起点 git 状态快照采集（`collector.py` 的同目录依赖） |
| `docs/upload-protocol.md` | 自建上传接收端所需的服务端协议 |
| `tests/` | 测试骨架（请求头脱敏 / session_id 防护 / porcelain 解析 / 安装器分流 / 版本事实源） |

---

## 项目文档

| 文件 | 内容 |
|------|------|
| [SECURITY.md](SECURITY.md) | 漏洞上报通道，以及**本工具的隐私边界**（采集范围 / 脱敏覆盖面 / 什么算漏洞） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、测试与门禁、三条有意的项目约定、不接受的改动 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 行为准则 |
| [docs/upload-protocol.md](docs/upload-protocol.md) | 上传协议与最小接收端实现 |
| [LICENSE](LICENSE) | MIT |

---

## 常见问题

### Claude Code 报 403 错误

最常见的原因是统一采集器未运行。排查步骤：

```bash
# 1. 检查统一采集器是否在运行
claude-trace status

# 2. 如果未运行，重启服务
claude-trace restart

# 3. 检查 settings.json 中 ANTHROPIC_BASE_URL 是否指向本地统一采集器
grep ANTHROPIC_BASE_URL ~/.claude/settings.json
# 应该是: "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"

# 4. 查看统一采集器日志排查具体错误
tail -20 /tmp/claude-trace-proxy.log
```

### Claude Code 没有走统一采集器

检查 `ANTHROPIC_BASE_URL` 是否生效：

```bash
# 方法 1：检查 settings.json
cat ~/.claude/settings.json | grep ANTHROPIC_BASE_URL

# 方法 2：检查统一采集器日志
tail -f /tmp/claude-trace-proxy.log

# 方法 3：检查统一采集器健康状态
curl http://127.0.0.1:4000/_internal/health
```

如果 settings.json 中已有全局的 `ANTHROPIC_BASE_URL`（如第三方上游地址），需要：
1. 将 settings.json 中的 `ANTHROPIC_BASE_URL` 改为 `http://127.0.0.1:4000`
2. 将统一采集器的 `--upstream` 设为原来的地址

### 统一采集器启动报端口占用

```bash
# 查看占用端口的进程
lsof -i :4000

# 杀掉占用进程
kill $(lsof -ti :4000)
```

### Hooks 不生效

```bash
# 检查 hooks 是否配置
cat ~/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); print('hooks' in d)"

# 重新配置
python3 setup_hooks.py

# 检查 collector.py 是否部署
ls -la ~/.claude/hooks/collector.py

# 检查事件文件是否生成
ls ~/.claude/trajectory_events/
```

### .traj 文件没有生成

.traj 在以下时机生成：
- 每次记录请求/响应后增量更新
- SessionEnd hook 触发时导出
- 会话超时（默认 30 分钟无活动）时导出
- 统一采集器 Ctrl+C 退出时导出所有活跃会话

如果 .traj 缺失，检查统一采集器日志中是否有 `构建 .traj 失败` 的错误。

### 如何查看采集到的数据

```bash
# 可视化查看 .traj（推荐，生成 HTML 在浏览器中查看）
python3 tools/viewer.py trajectories/sessions/<session_id>/session.traj
python3 tools/viewer.py trajectories/sessions/              # 目录索引模式
python3 tools/viewer.py trajectories/sessions/<id>/session.traj -o out.html

# 查看增量日志（始终有）
head -1 trajectories/sessions/<session_id>/raw.jsonl | python3 -m json.tool

# 查看每轮原始请求/响应（仅 --save-raw 时才有）
cat trajectories/sessions/<session_id>/raw/001_request.json | python3 -m json.tool

# 查看 .traj 轨迹摘要
python3 -c "
import json
t = json.load(open('trajectories/sessions/<session_id>/session.traj'))
m = t['metadata']
print(f\"model: {m['model']}\")
print(f\"steps: {m['total_steps']}\")
print(f\"tools: {m['tools_used']}\")
print(f\"cost: \${m['total_cost_usd']:.4f}\")
print(f\"exit: {m['exit_status']}\")
"
```

### 如何移除所有配置恢复原状

```bash
# 1. 卸载 launchd 服务
claude-trace uninstall

# 2. 移除 Hooks 配置
python3 setup_hooks.py --remove

# 3. 恢复 ANTHROPIC_BASE_URL（改回原来的上游地址或删除）
# 编辑 ~/.claude/settings.json

# 4. 删除 hooks 脚本
rm ~/.claude/hooks/collector.py

# 5. 删除事件数据（可选）
rm -rf ~/.claude/trajectory_events
```

---

## 停止与清理

```bash
# 停止 launchd 服务（推荐）
claude-trace uninstall

# 停止手动启动的守护进程
rm /tmp/claude-trace-proxy.pid && kill $(lsof -ti :4000)

# 停止手动启动的统一采集器
# 直接 Ctrl+C，会自动导出所有活跃会话的轨迹

# 清理采集数据（新布局）
rm -rf trajectories/sessions

# 旧布局残留（如果还有）
rm -rf trajectories/raw trajectories/traj

# 清理 Hooks 事件数据
rm -rf ~/.claude/trajectory_events

# 查看统一采集器日志
tail -f /tmp/claude-trace-proxy.log
```
