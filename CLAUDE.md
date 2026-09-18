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
├── proxy.py            # HTTP 代理服务器（单测 / --upload-status 仍可用；生产入口是 trace_agent.py）
├── trace_agent.py      # 统一采集入口（Claude 代理 + Codex watcher）
├── version_info.py     # 版本号解析：仓库 version 文件 → 打包后的 sys._MEIPASS
├── builder.py          # 轨迹构建器，请求/响应对 → .traj 格式
├── collector.py        # Hooks 采集脚本，部署到 ~/.claude/hooks/
├── setup_hooks.py      # 自动配置 settings.json 的 hooks
├── merger.py           # 双通道数据合并器（代理 + Hooks；运行时 lazy import）
├── uploader.py         # 可靠上传管理器（gzip + SHA256 + 重试队列 + 启动补传）
├── git_state.py        # git 快照；collector.py 的同目录依赖
├── import_codex.py     # Codex 导入 + rollout watcher
├── tools/              # 离线 CLI（采集进程从不 import）
│   ├── viewer.py           # 轨迹数据 HTML 查看器
│   ├── sync.py             # 手动批量补传（URL/token 必填，身份不回退）
│   ├── rebuild_trajs.py    # 用当前 builder 重建历史 .traj
│   ├── recover_truncated.py # 历史数据修复（复活截断 / 超大 traj 去重 / 重传）
│   ├── filter_trajs.py     # 轨迹过滤器
│   ├── convert_trajs.py    # 格式转换（.traj → SFT .jsonl）
│   ├── combine_trajs.py    # 合并 + shuffle SFT 数据
│   └── migrate_storage.py  # 存储迁移（旧 raw/+traj/ → 新 sessions/）
├── start.sh            # 一键启动脚本（代理 + Claude Code 生命周期绑定）
├── proxy-daemon.sh     # 守护进程脚本（开发版：python3 trace_agent.py）
├── install-daemon.sh   # 安装/卸载 launchd 自启动服务（macOS）。--watch 才装文件监听
├── watch-reload.sh     # 文件监听脚本，.py 变更后自动重启（需 fswatch；默认不装）
├── build/build.sh      # PyInstaller 打包
├── build/release.sh    # 发版：--bump 改版本，--upload 发 GitHub Releases
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
采集: trace_agent.py → proxy.py (通道A) + collector.py (通道B)
  ↓
构建: builder.py → .traj
  ↓
合并: merger.py（双通道关联）
  ↓
后处理: tools/filter_trajs.py → tools/convert_trajs.py → tools/combine_trajs.py → training_data.jsonl
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
- 运行时模块平铺在仓库根（不引入包层级）；离线 CLI 在 `tools/`
- 文件命名使用 `动词_名词.py` 风格（如 `tools/filter_trajs.py`、`tools/convert_trajs.py`）
- 数据交换统一使用 JSON/JSONL 格式
- 命令行参数使用 argparse，保持风格一致
- 不要生成零散的文档文件，文档集中在 README.md 和 docs/ 目录

## 关键设计决策

- 代理永不中断：launchd 自启动 + daemon 自动重启，确保采集不丢数据
- 会话维度存储：所有数据按 `sessions/{session_id}/` 组织，一个会话的 traj、raw、events 放在一起
- 增量存储：JSONL 首行保存完整 request_body，后续行只保存 new_messages，避免 O(n²) 膨胀
- 双通道采集：proxy（API 流量）+ hooks（会话事件）通过 session_id 关联
- 官方 OpenTelemetry 是可选的旁路观测，**不是**采集通道。训练数据主源永远是
  `raw.jsonl`（Claude）和 rollout / app-server（Codex）。禁止把
  `ANTHROPIC_BASE_URL` 改离 `http://127.0.0.1:4000` 去「改用官方监控」——
  代理首先是路由器，没起就是 403。
- 上传 opt-in：默认关闭，无内置端点与凭据。仅当 `TRAJ_PLATFORM_URL` 与 `TRAJ_UPLOAD_TOKEN`
  同时非空才启用；启用后会话结束时由 `uploader.py` 上传 session.traj + raw.jsonl + events.jsonl。
  `TRAJ_USER_ID` / `TRAJ_DEVICE_ID` 默认留空，不回退到系统用户名与主机名。
  手动批量补传走 `tools/sync.py`（同一对变量必填，不并进 uploader 的异步队列）。
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
./install-daemon.sh install         # 安装 launchd 服务（开机自启；不装文件监听）
./install-daemon.sh install --watch # 开发用：同时装文件监听
./install-daemon.sh status          # 查看服务状态
./install-daemon.sh restart         # 重启服务（bootout + bootstrap，不是 kickstart）
./install-daemon.sh uninstall       # 卸载服务

# 渠道切换
./switch-channel.sh list            # 列出所有渠道
./switch-channel.sh <渠道名>        # 切换渠道（同步更新 token + 代理上游）

# 一键启动（代理 + Claude Code 生命周期绑定，适合临时使用。
# 会杀掉占用 4000 的进程，本机已有 launchd 采集时不要跑）
./start.sh

# 数据处理管道
python3 tools/filter_trajs.py --input trajectories/sessions/ --output filtered/
python3 tools/convert_trajs.py --input filtered/ --output sft/ --style xml
python3 tools/combine_trajs.py --input sft/ --output training_data.jsonl --shuffle

# 双通道合并
python3 merger.py --all

# 可视化查看轨迹
python3 tools/viewer.py trajectories/sessions/<session_id>/session.traj
python3 tools/viewer.py trajectories/sessions/   # 目录索引模式

# 手动批量补传（uploader 没跑时；须 export TRAJ_PLATFORM_URL + TRAJ_UPLOAD_TOKEN）
python3 tools/sync.py
python3 tools/sync.py --all
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | 4000 | 代理监听端口 |
| `UPSTREAM` | `https://api.anthropic.com` | 上游 API 地址 |
| `OUTPUT` | `./trajectories` | 轨迹数据输出目录 |
| `FORCE_THINKING` | 0 | 非 0 时强制 thinking effort=max |
| `TRAJ_PLATFORM_URL` | 空 | 上传目标；与 token 两者皆非空才启用上传（uploader 与 tools/sync.py 共用） |
| `TRAJ_UPLOAD_TOKEN` | 空 | 上传凭据 |
| `TRAJ_USER_ID` / `TRAJ_DEVICE_ID` | 空 | 身份字段，留空即不上报（不回退到系统用户名/主机名） |
| `TRAJ_LOCAL_DIR` | 安装器落点，否则 `./trajectories/sessions` | `tools/sync.py` 读取的会话目录 |
| `TRAJ_CLEANUP_AFTER_UPLOAD` | `false` | 上传成功后是否删本地。**默认保留**，只有显式 `true` 才删 |
| `TRAJ_BACKFILL_ON_START` | `true` | 启动时补传盘上未上云的会话（上传链路的兜底） |

## 上传链路的三条铁律

这三条都是踩过坑换来的，改动上传相关代码前先读：

1. **上传不能挂在退出关键路径上。** 退出时只做落盘（约 30ms），上传交给
   启动补传兜底。sid-code 的教训：上传挂在 SessionEnd、退出预算 1.2s 而
   上传要 10s，结果 52 个会话一次都没传上去，且无人发现。
2. **不能只有一个触发点。** 触发器（hook 通知 / 超时清理 / 优雅退出）都可能
   不灵，所以必须有启动补传按磁盘现状兜底：目录在、traj 非空、`.uploaded` 缺
   → 补传。判据不依赖内存状态，也不依赖重试队列（队列文件本身可能丢）。
3. **告警要说后果，不说现象。** 说「N 个会话的轨迹仍未上云」，不说
   「N 个会话未正常收尾」。后者是正确的现象描述，但省掉了后果，
   于是唯一的线索被当成背景噪音 —— sid-code 就是这么错过的。

自检：`python3 trace_agent.py --output <dir> --upload-status`（proxy.py 亦可），
或 `curl -s localhost:4000/_internal/health | python3 -m json.tool`。

## 数据目录

当前布局按会话组织（打包安装在 `~/.claude-trace/trajectories/`）：

- `trajectories/sessions/{session_id}/session.traj`
- `trajectories/sessions/{session_id}/raw.jsonl`（始终写）
- `trajectories/sessions/{session_id}/events.jsonl`
- `trajectories/sessions/{session_id}/raw/NNN_*.json`（仅 `--save-raw`）
- Hooks 原始事件：`~/.claude/trajectory_events/{session_id}.jsonl`

旧布局 `trajectories/raw/` + `trajectories/traj/` 只存在于历史数据。
