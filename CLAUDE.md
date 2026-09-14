# CLAUDE.md

## 项目概述

claude-trace 是一个 Claude Code 轨迹采集工具。通过 HTTP 代理 + Hooks 双通道采集 Claude Code 的完整交互数据，输出 SWE-agent 兼容的 `.traj` 格式，支持后续 SFT 训练数据生成。

## 核心原则：代理永不中断

代理是整个采集系统的核心，必须保证在任何情况下持续运行：

- **开机自启**：通过 macOS launchd 服务实现，登录后自动启动
- **崩溃重启**：launchd `KeepAlive=true` + `proxy-daemon.sh` 双重保障
- **终端无关**：代理运行在 launchd 管理下，不依赖任何终端会话
- **请求链路**：`Claude Code → localhost:4000（代理采集）→ 上游 API`
- **settings.json 中 `ANTHROPIC_BASE_URL` 必须始终指向 `http://127.0.0.1:4000`**，上游地址通过代理的 `--upstream` 参数配置

如果代理未运行而 `ANTHROPIC_BASE_URL` 指向 `localhost:4000`，Claude Code 会报 403 错误。排查步骤：
```bash
./install-daemon.sh status          # 检查服务状态
./install-daemon.sh restart         # 重启服务
tail -20 /tmp/claude-trace-proxy.log  # 查看日志
```

## 技术栈

- Python 3，唯一外部依赖：aiohttp
- Bash 脚本用于启动/守护进程管理
- macOS launchd 用于服务自启动
- 数据格式：JSON / JSONL / .traj

## 项目结构

```
├── migrate_storage.py  # 存储迁移脚本（旧 raw/+traj/ → 新 sessions/）
├── proxy.py            # HTTP 代理服务器，SSE Tee 模式采集 API 请求/响应
├── builder.py          # 轨迹构建器，请求/响应对 → .traj 格式
├── collector.py        # Hooks 采集脚本，部署到 ~/.claude/hooks/
├── setup_hooks.py      # 自动配置 settings.json 的 hooks
├── merger.py           # 双通道数据合并器（代理 + Hooks）
├── filter_trajs.py     # 轨迹过滤器
├── convert_trajs.py    # 格式转换（.traj → SFT .jsonl）
├── combine_trajs.py    # 合并 + shuffle SFT 数据
├── viewer.py           # 轨迹数据 HTML 查看器，将 .traj 转为可视化 HTML
├── start.sh            # 一键启动脚本（代理 + Claude Code 生命周期绑定）
├── proxy-daemon.sh     # 守护进程脚本（自动重启循环）
├── install-daemon.sh   # 安装/卸载 launchd 自启动服务（macOS）
├── watch-reload.sh     # 文件监听脚本，.py 变更后自动重启代理（需 fswatch）
├── switch-channel.sh   # 快速切换 API 渠道（token + 上游）
├── channels.json       # 渠道配置文件（含 token，已加入 .gitignore）
├── channels.json.example  # 渠道配置模板（可提交）
├── trajectories/
│   └── sessions/        # 按会话维度存储（每个 session_id 一个目录）
│       └── {session_id}/
│           ├── session.traj    # 构建好的轨迹文件
│           ├── raw.jsonl       # 原始 API 请求/响应（紧凑 JSONL）
│           ├── events.jsonl    # Hook 事件数据
│           └── raw/            # 每轮请求/响应 JSON 文件
└── docs/               # 设计文档
```

## 架构流水线

```
采集: proxy.py (通道A) + collector.py (通道B)
  ↓
构建: builder.py → .traj
  ↓
合并: merger.py（双通道关联）
  ↓
后处理: filter_trajs.py → convert_trajs.py → combine_trajs.py → training_data.jsonl
```

## 开发工具与门禁

```bash
brew install ruff gitleaks pre-commit   # macOS 上系统 Python 受 PEP 668 保护，用 brew 装
pre-commit install                      # 装 git hook

ruff check .                            # lint（只开 correctness 档，配置在 pyproject.toml）
pytest -q                               # 测试（tests/）
gitleaks detect --no-git --redact       # 密钥扫描
pre-commit run --all-files              # 一次跑全部
```

三条隐私默认值门禁同时存在于 `.pre-commit-config.yaml` 与 CI：身份字段不得回退到
真实用户名/主机名、上传端点与凭据不得有内置默认值、代理不得默认绑 `0.0.0.0`。
改这三处会被拦下，理由见 `SECURITY.md`。

## 编码规范

- 所有代码注释必须使用中文
- 扁平脚本结构，不使用包层级，所有核心文件在项目根目录
- 文件命名使用 `动词_名词.py` 风格（如 `filter_trajs.py`、`convert_trajs.py`）
- 数据交换统一使用 JSON/JSONL 格式
- 命令行参数使用 argparse，保持风格一致
- 不要生成零散的文档文件，文档集中在 README.md 和 docs/ 目录

## 关键设计决策

- 代理永不中断：launchd 自启动 + daemon 自动重启，确保采集不丢数据
- 会话维度存储：所有数据按 `sessions/{session_id}/` 组织，一个会话的 traj、raw、events 放在一起
- 增量存储：JSONL 首行保存完整 request_body，后续行只保存 new_messages，避免 O(n²) 膨胀
- 双通道采集：proxy（API 流量）+ hooks（会话事件）通过 session_id 关联
- 上传 opt-in：默认关闭，无内置端点与凭据。仅当 `TRAJ_PLATFORM_URL` 与 `TRAJ_UPLOAD_TOKEN`
  同时非空才启用；启用后会话结束时上传 session.traj + raw.jsonl + events.jsonl。
  `TRAJ_USER_ID` / `TRAJ_DEVICE_ID` 默认留空，不回退到系统用户名与主机名。
- 安全：**只脱三个请求头**（`x-api-key` / `authorization` / `proxy-authorization`），
  **消息体不做内容级过滤** —— 对话里的密钥会明文落盘，内容级 Scrubber 尚未实现。
  代理默认绑定 127.0.0.1。
- 代理使用 SSE Tee 模式，零延迟转发 + 后台记录，不影响 Claude Code 正常使用

## 渠道管理

多个 API 渠道（token + 上游地址）通过 `channels.json` 统一管理，使用 `switch-channel.sh` 一键切换：

```bash
./switch-channel.sh list       # 列出所有渠道
./switch-channel.sh default    # 切换到名为 default 的渠道
./switch-channel.sh backup     # 切换到名为 backup 的渠道
./switch-channel.sh status     # 查看当前渠道
```

切换时会同步更新：
1. `~/.claude/settings.json` 的 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL`（固定指向代理）
2. launchd plist 的 `UPSTREAM` 和 `FORCE_THINKING`
3. 自动重启代理使配置生效

新增渠道只需编辑 `channels.json`，无需修改脚本：

```json
{
  "channels": {
    "my-channel": {
      "name": "渠道名称",
      "token": "sk-xxx",
      "upstream": "https://your-api.example.com",
      "force_thinking": 0
    }
  }
}
```

> `channels.json` 含有 API Key，已加入 `.gitignore`，不会提交到 git。初始化时复制 `channels.json.example` 并填写真实值。

## 常用命令

```bash
# 服务管理（推荐方式）
./install-daemon.sh install         # 安装 launchd 服务（开机自启）
./install-daemon.sh status          # 查看服务状态
./install-daemon.sh restart         # 重启服务
./install-daemon.sh uninstall       # 卸载服务

# 渠道切换
./switch-channel.sh list            # 列出所有渠道
./switch-channel.sh <渠道名>        # 切换渠道（同步更新 token + 代理上游）

# 一键启动（代理 + Claude Code 生命周期绑定，适合临时使用）
./start.sh

# 数据处理管道
python3 filter_trajs.py --input trajectories/traj/ --output filtered/
python3 convert_trajs.py --input filtered/ --output sft/ --style xml
python3 combine_trajs.py --input sft/ --output training_data.jsonl --shuffle

# 双通道合并
python3 merger.py --all

# 可视化查看轨迹
python3 viewer.py trajectories/traj/<session_id>.traj
python3 viewer.py trajectories/traj/   # 目录索引模式
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | 4000 | 代理监听端口 |
| `UPSTREAM` | `https://api.anthropic.com` | 上游 API 地址 |
| `OUTPUT` | `./trajectories` | 轨迹数据输出目录 |
| `FORCE_THINKING` | 0 | 非 0 时强制 thinking effort=max |

## 数据目录

- 原始数据：`trajectories/raw/{session_id}.jsonl` + `trajectories/raw/{session_id}/NNN_request.json`
- 轨迹文件：`trajectories/traj/{session_id}.traj`
- Hooks 事件：`~/.claude/trajectory_events/{session_id}.jsonl`
