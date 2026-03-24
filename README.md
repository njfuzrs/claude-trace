# claude-trace

Claude Code 轨迹采集工具。通过 HTTP 代理 + Hooks 双通道采集 Claude Code 的完整交互数据，输出 SWE-agent 兼容的 `.traj` 格式。

## 目录

- [快速开始](#快速开始)
- [工作原理](#工作原理)
- [安装](#安装)
- [使用方式](#使用方式)
  - [方式一：一键启动](#方式一一键启动)
  - [方式二：守护进程模式（推荐日常使用）](#方式二守护进程模式推荐日常使用)
  - [方式三：手动分步启动](#方式三手动分步启动)
  - [方式四：仅代理模式（不配置 Hooks）](#方式四仅代理模式不配置-hooks)
- [配置说明](#配置说明)
  - [代理参数](#代理参数)
  - [Hooks 配置](#hooks-配置)
  - [第三方 API 代理](#第三方-api-代理)
- [数据目录结构](#数据目录结构)
- [数据处理管道](#数据处理管道)
  - [第一步：过滤](#第一步过滤)
  - [第二步：格式转换](#第二步格式转换)
  - [第三步：合并](#第三步合并)
  - [双通道数据合并](#双通道数据合并)
- [文件说明](#文件说明)
- [常见问题](#常见问题)
- [停止与清理](#停止与清理)

---

## 快速开始

```bash
# 1. 安装依赖
pip install aiohttp

# 2. 一键启动（自动配置 Hooks + 启动代理 + 启动 Claude Code）
./start.sh
```

启动后正常使用 Claude Code 即可，所有交互数据会自动采集到 `./trajectories/` 目录。

---

## 工作原理

```
Claude Code
    │
    ├── API 请求 ──→ HTTP 代理 (localhost:4000) ──→ 上游 API
    │                    │
    │                    └── 记录: 请求体 / SSE 响应 / token 用量
    │
    └── 生命周期事件 ──→ Hooks 采集器
                         │
                         └── 记录: session_id / 用户 prompt / 工具调用 / sub-agent
```

- **通道 A（HTTP 代理）**：通过 `ANTHROPIC_BASE_URL` 环境变量劫持 API 请求，SSE Tee 模式零延迟转发 + 后台记录
- **通道 B（Hooks）**：通过 Claude Code 原生 hooks 机制采集会话事件（session 生命周期、用户 prompt、sub-agent 等）
- 两个通道通过 `session_id` 自动关联合并

---

## 安装

```bash
git clone <repo-url> claude-trace
cd claude-trace
pip install -r requirements.txt   # 只需要 aiohttp
```

---

## 使用方式

### 方式一：一键启动

最简单的方式，自动完成所有配置：

```bash
./start.sh
```

它会依次执行：
1. 检查 aiohttp 依赖
2. 部署 `collector.py` 到 `~/.claude/hooks/`
3. 配置 `~/.claude/settings.json` 的 hooks
4. 后台启动代理（端口 4000）
5. 前台启动 Claude Code（自动设置 `ANTHROPIC_BASE_URL`）

Claude Code 退出后，代理自动停止。

**自定义参数：**

```bash
./start.sh --port 5000              # 自定义端口
./start.sh --output /data/traces    # 自定义输出目录
./start.sh --proxy-only             # 只启动代理，不启动 Claude Code
./start.sh -- -p "hello"            # -- 之后的参数传给 claude
```

### 方式二：守护进程模式（推荐日常使用）

代理常驻后台，被 kill 后自动重启。适合长期使用，不需要每次手动启动代理。

**第一步：启动守护进程**

```bash
nohup ./proxy-daemon.sh > /dev/null 2>&1 &
```

**第二步：配置 Hooks（只需执行一次）**

```bash
python3 setup_hooks.py
```

**第三步：配置 settings.json（只需执行一次）**

编辑 `~/.claude/settings.json`，将 `ANTHROPIC_BASE_URL` 指向代理：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"
  }
}
```

配置完成后，以后每次正常启动 `claude` 即可，所有请求自动经过代理采集。

**切换上游地址：** `proxy-daemon.sh` 通过环境变量配置，不需要改文件：

```bash
# 默认上游是 https://api.anthropic.com，直接启动即可
nohup ./proxy-daemon.sh > /dev/null 2>&1 &

# 使用第三方代理（如 api.anthropic.com）
UPSTREAM=https://api.anthropic.com nohup ./proxy-daemon.sh > /dev/null 2>&1 &

# 同时自定义端口和输出目录
PORT=5000 OUTPUT=/data/traces UPSTREAM=https://api.anthropic.com nohup ./proxy-daemon.sh > /dev/null 2>&1 &
```

支持的环境变量：

| 变量 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `PORT` | 4000 | 代理监听端口 |
| `UPSTREAM` | `https://api.anthropic.com` | 上游 API 地址 |
| `OUTPUT` | `./trajectories` | 轨迹数据输出目录 |

切换上游时需要重启守护进程：

```bash
# 停掉旧的
rm /tmp/claude-trace-proxy.pid && kill $(lsof -ti :4000)

# 用新上游启动
UPSTREAM=https://新地址 nohup ./proxy-daemon.sh > /dev/null 2>&1 &
```

**停止守护进程：**

```bash
# 方法 1：删除 PID 文件后 kill（彻底停止，不会重启）
rm /tmp/claude-trace-proxy.pid && kill $(lsof -ti :4000)

# 方法 2：直接 kill 守护进程
kill $(cat /tmp/claude-trace-proxy.pid)
```

### 方式三：手动分步启动

适合调试或自定义场景。

```bash
# 终端 1：启动代理
python3 proxy.py --port 4000 --output ./trajectories --verbose

# 终端 2：配置 Hooks（首次使用）
python3 setup_hooks.py

# 终端 2：通过代理启动 Claude Code
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 claude
```

### 方式四：仅代理模式（不配置 Hooks）

如果不想配置 Hooks，代理可以独立工作。会话识别退回到对话内容连续性匹配（准确度略低）。

```bash
python3 proxy.py --port 4000
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 claude
```

此模式下缺少的数据：session_id（自动生成 UUID 替代）、用户原始 prompt、sub-agent 生命周期、context compaction 详情。

---

## 配置说明

### 代理参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | 4000 | 代理监听端口 |
| `--host` | 127.0.0.1 | 监听地址（默认只监听本地，防止局域网访问） |
| `--output` | ./trajectories | 轨迹数据输出目录 |
| `--upstream` | https://api.anthropic.com | 上游 API 地址 |
| `--session-timeout` | 300 | 会话超时时间（秒），超时后自动导出 .traj |
| `--save-raw` | true | 保存原始请求/响应 JSON 文件 |
| `--no-save-raw` | - | 只保留 JSONL + .traj，不保存单独的 JSON 文件 |
| `--verbose` | false | 详细日志输出 |

### Hooks 配置

```bash
python3 setup_hooks.py              # 写入 ~/.claude/settings.json（全局）
python3 setup_hooks.py --local      # 写入 .claude/settings.local.json（当前项目）
python3 setup_hooks.py --show       # 只打印配置，不写入
python3 setup_hooks.py --remove     # 移除已配置的 hooks
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

### 第三方 API 代理

如果你使用第三方 API 代理（而非 Anthropic 官方 API），需要将 `--upstream` 设置为你的代理地址：

```bash
# 手动启动
python3 proxy.py --upstream https://your-proxy.com

# 或修改 proxy-daemon.sh 中的 --upstream 参数
# 或修改 start.sh 的 UPSTREAM 变量
UPSTREAM=https://your-proxy.com ./start.sh
```

同时确保 `~/.claude/settings.json` 中的 `ANTHROPIC_BASE_URL` 指向本地代理：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"
  }
}
```

请求链路：`Claude Code → localhost:4000（代理采集）→ your-proxy.com → Anthropic API`

---

## 数据目录结构

```
trajectories/
├── raw/                                    # 原始数据
│   ├── {session_id}.jsonl                  # 增量日志（每行一个请求/响应对）
│   └── {session_id}/
│       ├── 001_request.json                # 第 1 个请求（含完整 body + 脱敏 headers）
│       ├── 001_response.json               # 第 1 个响应（SSE 重组后）
│       ├── 002_request.json
│       ├── 002_response.json
│       └── ...
├── traj/                                   # SWE-agent 兼容格式
│   └── {session_id}.traj                   # 轨迹文件（trajectory + history + info + metadata）
~/.claude/trajectory_events/                # Hooks 事件数据（通道 B）
    └── {session_id}.jsonl                  # 按 session 分文件的事件流
```

**JSONL 格式说明：**

- 第一行保存完整 `request_body`（含 system prompt），后续行只保存增量 `new_messages`
- 避免 Claude Code 每次重发完整历史导致的 O(n²) 存储膨胀

**.traj 文件结构：**

```json
{
  "trajectory": [                    // TAO 步骤列表
    {"message_type": "action", "tool_name": "Bash", "tool_input": {...}, ...},
    {"message_type": "observation", "content": "...", ...}
  ],
  "history": [...],                  // 完整 LLM 对话历史（用于 SFT 训练）
  "info": {                          // 会话统计
    "model_stats": {"tokens_sent": ..., "tokens_received": ..., "api_calls": ..., "total_cost_usd": ...},
    "exit_status": "end_turn",
    "has_thinking": true
  },
  "metadata": {                      // 扩展元数据（含 Hooks 数据）
    "session_id": "...",
    "model": "claude-opus-4-6",
    "tools_used": ["Bash", "Read", "Write"],
    "user_prompts": ["..."],
    "subagent_spans": [...],
    "compactions": [...]
  }
}
```

**安全说明：**

- 所有 raw 文件中的 API Key 已自动脱敏（`Authorization: Bearer sk-***`）
- 代理默认绑定 `127.0.0.1`，不暴露到局域网

---

## 数据处理管道

采集完成后，可以通过三步管道将 `.traj` 转换为 SFT 训练数据：

```
.traj → 过滤 → 格式转换 → 合并 → training_data.jsonl
```

### 第一步：过滤

```bash
python3 filter_trajs.py \
    --input trajectories/traj/ \
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
| `--require-end-turn` | 必须以 end_turn 正常结束 |
| `--require-tool-use` | 必须包含工具调用（过滤纯对话） |
| `--report` | 输出质量指标报告 |

### 第二步：格式转换

```bash
python3 convert_trajs.py \
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
python3 combine_trajs.py \
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

如果同时使用了代理和 Hooks，可以用 `merger.py` 将两个通道的数据合并为增强版 `.traj`：

```bash
# 合并指定会话
python3 merger.py --session-id <session_id>

# 合并所有会话
python3 merger.py --all

# 自定义目录
python3 merger.py --all \
    --raw-dir ./trajectories/raw \
    --events-dir ~/.claude/trajectory_events \
    --output ./trajectories/traj
```

合并后的 `.traj` 会包含 Hooks 提供的额外信息：用户原始 prompt、sub-agent 生命周期、compaction 摘要等。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `proxy.py` | HTTP 代理服务器，SSE Tee 模式采集 API 请求/响应 |
| `builder.py` | 轨迹构建器，将请求/响应对转换为 .traj 格式 |
| `collector.py` | Hooks 采集脚本，部署到 `~/.claude/hooks/` |
| `setup_hooks.py` | 自动配置 settings.json 的 hooks |
| `merger.py` | 双通道数据合并器 |
| `filter_trajs.py` | 轨迹过滤器 |
| `convert_trajs.py` | 格式转换（.traj → SFT .jsonl） |
| `combine_trajs.py` | 合并 + shuffle SFT 数据 |
| `start.sh` | 一键启动脚本 |
| `proxy-daemon.sh` | 自动重启的守护进程脚本 |

---

## 常见问题

### Claude Code 没有走代理

检查 `ANTHROPIC_BASE_URL` 是否生效：

```bash
# 方法 1：检查 settings.json
cat ~/.claude/settings.json | grep ANTHROPIC_BASE_URL

# 方法 2：检查代理日志
tail -f /tmp/claude-trace-proxy.log

# 方法 3：检查代理健康状态
curl http://127.0.0.1:4000/_internal/health
```

如果 settings.json 中已有全局的 `ANTHROPIC_BASE_URL`（如第三方代理地址），需要：
1. 将 settings.json 中的 `ANTHROPIC_BASE_URL` 改为 `http://127.0.0.1:4000`
2. 将代理的 `--upstream` 设为原来的地址

### 代理启动报端口占用

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
- 会话超时（默认 5 分钟无活动）时导出
- 代理 Ctrl+C 退出时导出所有活跃会话

如果 .traj 缺失，检查代理日志中是否有 `构建 .traj 失败` 的错误。

### 如何查看采集到的数据

```bash
# 查看原始请求/响应
cat trajectories/raw/<session_id>/001_request.json | python3 -m json.tool

# 查看 JSONL 增量日志
head -1 trajectories/raw/<session_id>.jsonl | python3 -m json.tool

# 查看 .traj 轨迹摘要
python3 -c "
import json
t = json.load(open('trajectories/traj/<session_id>.traj'))
m = t['metadata']
print(f\"model: {m['model']}\")
print(f\"steps: {m['total_steps']}\")
print(f\"tools: {m['tools_used']}\")
print(f\"cost: \${m['total_cost_usd']:.4f}\")
print(f\"exit: {m['exit_status']}\")
"

# 查看 Hooks 事件
cat ~/.claude/trajectory_events/<session_id>.jsonl | python3 -m json.tool --json-lines
```

### 如何移除所有配置恢复原状

```bash
# 1. 停止守护进程
rm /tmp/claude-trace-proxy.pid && kill $(lsof -ti :4000) 2>/dev/null

# 2. 移除 Hooks 配置
python3 setup_hooks.py --remove

# 3. 恢复 ANTHROPIC_BASE_URL（改回原来的值或删除）
# 编辑 ~/.claude/settings.json

# 4. 删除 hooks 脚本
rm ~/.claude/hooks/collector.py

# 5. 删除事件数据（可选）
rm -rf ~/.claude/trajectory_events
```

---

## 停止与清理

```bash
# 停止守护进程（彻底停止，不重启）
rm /tmp/claude-trace-proxy.pid && kill $(lsof -ti :4000)

# 停止手动启动的代理
# 直接 Ctrl+C，会自动导出所有活跃会话的轨迹

# 清理采集数据
rm -rf trajectories/raw trajectories/traj

# 清理 Hooks 事件数据
rm -rf ~/.claude/trajectory_events

# 查看代理日志
tail -f /tmp/claude-trace-proxy.log
```
