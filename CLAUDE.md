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
├── trajectories/
│   ├── raw/            # 原始请求/响应数据
│   └── traj/           # 构建好的 .traj 文件
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

## 编码规范

- 所有代码注释必须使用中文
- 扁平脚本结构，不使用包层级，所有核心文件在项目根目录
- 文件命名使用 `动词_名词.py` 风格（如 `filter_trajs.py`、`convert_trajs.py`）
- 数据交换统一使用 JSON/JSONL 格式
- 命令行参数使用 argparse，保持风格一致
- 不要生成零散的文档文件，文档集中在 README.md 和 docs/ 目录

## 关键设计决策

- 代理永不中断：launchd 自启动 + daemon 自动重启，确保采集不丢数据
- 增量存储：JSONL 首行保存完整 request_body，后续行只保存 new_messages，避免 O(n²) 膨胀
- 双通道采集：proxy（API 流量）+ hooks（会话事件）通过 session_id 关联
- 安全：API Key 自动脱敏，代理默认绑定 127.0.0.1
- 代理使用 SSE Tee 模式，零延迟转发 + 后台记录，不影响 Claude Code 正常使用

## 常用命令

```bash
# 服务管理（推荐方式）
./install-daemon.sh install         # 安装 launchd 服务（开机自启）
./install-daemon.sh status          # 查看服务状态
./install-daemon.sh restart         # 重启服务
./install-daemon.sh uninstall       # 卸载服务

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
