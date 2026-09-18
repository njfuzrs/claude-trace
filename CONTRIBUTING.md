# 贡献指南

先说三件容易被当成笔误、其实是有意决策的事，免得你按常规改法提了 PR 又被要求改回去。

## 三条有意的项目约定

### 1. 代码注释一律用中文

这不是笔误，是项目约定（见 `CLAUDE.md` 的编码规范）。新增代码请继续用中文注释。
标识符、日志 key、commit message 的类型前缀用英文，注释与文档正文用中文。

### 2. 运行时平铺在根，不使用包层级

运行时模块（`trace_agent.py` / `proxy.py` / `builder.py` / `collector.py` /
`git_state.py` 等）都在仓库根目录，没有 `src/`、没有 `claude_trace/__init__.py`。
离线 CLI（查看、过滤、补传、历史修复）在 `tools/`。

理由：运行时文件相当一部分要能被**单独拷走执行** —— `collector.py` 会被安装器
复制到 `~/.claude/hooks/` 下由 Claude Code 直接调用，`git_state.py` 是它的
同目录依赖。把运行时收成标准 Python 包会破坏这个部署方式。

所以请**不要**提交「把运行时重构成标准 Python 包」的 PR。
文件命名沿用 `动词_名词.py` 风格（`tools/filter_trajs.py`、`tools/convert_trajs.py`）。

### 3. 仅支持 macOS

服务管理依赖 launchd，文件监听依赖 `fswatch`，构建产物是 Darwin 二进制。
Linux / Windows 支持不在当前范围内。纯 Python 的数据处理部分（`builder.py`、
`tools/filter_trajs.py` 等）在 Linux 上能跑，CI 的 ubuntu job 也只跑这部分。

---

## 环境要求

- **macOS**（开发与运行）
- **Python 3.10 或更高**

  > 下限是 3.10 而不是 3.9：`trace_agent.py` 用了 PEP 604 的 `X | None`
  > 类型联合，且写在函数签名里、定义时即求值，3.9 下会直接
  > `TypeError: unsupported operand type(s) for |`。

- 唯一运行时依赖：`aiohttp>=3.9.0`

```bash
git clone https://github.com/njfuzrs/claude-trace.git
cd claude-trace
pip3 install -r requirements.txt
```

开发工具（lint / 测试 / 密钥扫描）：

```bash
brew install ruff gitleaks pre-commit
pre-commit install          # 装 git hook，提交前自动跑
```

> macOS 上系统 Python 受 PEP 668 保护，`pip3 install ruff` 会被拒。用 brew 装，
> 或自己建 venv。

---

## 跑测试与检查

```bash
ruff check .                       # lint（只开 correctness 档，见 pyproject.toml）
pytest -q                          # 测试（async 用例需要 pytest-asyncio）
gitleaks detect --no-git --redact  # 密钥扫描
pre-commit run --all-files         # 一次跑全部门禁
```

`pyproject.toml` 开了 `asyncio_mode = auto`，本地跑 async 测试需要 `pytest-asyncio`（CI 会装）。macOS 上系统 Python 受 PEP 668 保护时：`brew install ruff` 之后用自己的 venv 装 `pytest pytest-asyncio`。

三者都必须绿才提 PR。CI 会在 macOS + Ubuntu × Python 3.10/3.12 上重跑。

### 关于测试覆盖

现有测试是**骨架，不是完整覆盖**，如实说明。已锁定的是最该锁的几处：

| 测试 | 对象 |
| --- | --- |
| `tests/test_sanitize.py` | `proxy.py` 的请求头脱敏 —— 目前唯一的脱敏能力 |
| `tests/test_session_id.py` | `session_id` 的路径遍历防护 |
| `tests/test_git_state.py` | `git_state.py` 的 porcelain 解析（纯函数） |
| `tests/test_version_source.py` | `version` 文件与 `pyproject.toml` 必须一致；`--version` 能自报 |
| `tests/test_install_plan.py` | 安装器分流与 tarball 文件名拼接（不真装） |
| `tests/test_proxy_passthrough.py` | 代理按原始字节转发 messages，不得改写/重序列化请求体 |

补测试的 PR 一律欢迎。**唯一硬要求**：将来若实现内容级脱敏器（Scrubber），
没有测试的实现不会被合并 —— 脱敏器是「没测试就等于没有」的那类模块，
它漏掉一种密钥形态，用户是不会收到任何报错的。

---

## 提交与 PR

- commit message 用 `类型: 简述` 格式（`feat:` / `fix:` / `chore:` / `docs:` / `test:`）
- 一个 PR 做一件事。脱敏、重构、加功能请分开提
- 改了行为就同步改 README / CLAUDE.md —— 这个项目吃过「文档声称已脱敏、实际只脱了三个请求头」的亏，
  文档与实现不一致在这里算 bug
- 改了采集内容（多记录了什么字段）请在 PR 里明确写出来。
  这是采集工具，**扩大采集范围属于需要用户知情的变更**，会被重点 review

## 不接受的改动

- 加回内置的上传端点或内置凭据。上传必须保持 opt-in、无默认值
- 让身份字段（`TRAJ_USER_ID` / `TRAJ_DEVICE_ID`）回退到系统用户名或主机名
- 把代理默认绑定从 `127.0.0.1` 改成 `0.0.0.0`

这三条都是隐私默认值，见 `SECURITY.md` 的「本工具的隐私边界」。

## 发版

版本号的单一事实源是仓库根的 `version` 文件。`pyproject.toml` 必须与它一致，
改版本请走脚本，不要人肉只改一处：

```bash
./build/release.sh --bump 0.4.0     # 同步改 version / pyproject.toml / CHANGELOG
# 检查 diff，把用户可见的变更填进 CHANGELOG 的 [0.4.0] 段
git add version pyproject.toml CHANGELOG.md && git commit -m "chore(release): v0.4.0"
./build/release.sh --upload --tag   # 构建、上传 GitHub Releases、打本地 tag
```

覆盖已有 Release 需要 `ALLOW_CLOBBER=true`。不要在同一个 tag 下覆盖行为不同的二进制。

目前只在本机 arm64 打包装，没有 CI 自动发版（GitHub-hosted macos-latest 是 Intel）。
Intel Mac 请走源码路径，或等对应架构的 Release。

## 上报安全问题

**不要开公开 issue。** 走 GitHub 私密通道，见 [SECURITY.md](SECURITY.md)。
