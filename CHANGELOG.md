# 更新日志

本文件记录值得用户知道的变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 0.2.0 之前的历史只存在于 git log 中，没有回溯整理 —— 那段时期是内部工具，
> 没有外部用户，补写变更记录的收益不足以支撑代价。

## [Unreleased]

### 变更（开源首发准备）

- **上传改为 opt-in，移除全部内置端点与凭据。** 此前 `dist/install.sh` 内置了一个
  默认上传服务器与一个共享 token，安装即生效。现在需同时配置
  `TRAJ_PLATFORM_URL` 与 `TRAJ_UPLOAD_TOKEN` 才启用上传，任一为空即禁用，
  交互式安装改为默认「否」的显式询问。
- **身份字段默认留空，不再回退到真实身份。** `TRAJ_USER_ID` 此前会回退到
  `os.getlogin()`、`TRAJ_DEVICE_ID` 回退到 `platform.node()`，未配置时会把
  系统用户名与主机名随每条轨迹上传。现在未配置即为空串。
- 上游 API 默认地址改为 `https://api.anthropic.com`。
- 发布链路从内网 GitLab 改为 GitHub Releases，改用 `gh` CLI 自带鉴权，
  脚本内不再持有任何 token。
- 版本号以仓库根目录 `version` 文件为单一事实源。

### 修正

- **README 的脱敏承诺改为如实描述。** 此前声称「所有 raw 文件中的 API Key 已自动脱敏」，
  实际只脱了 `x-api-key` / `authorization` / `proxy-authorization` 三个请求头，
  **消息体不做任何内容级过滤**。同一处错误声明也存在于 `CLAUDE.md`，一并修正。

### 新增

- 治理文件：`LICENSE`（MIT）、`CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、
  `.editorconfig`、GitHub issue/PR 模板、CI、dependabot。
- `docs/upload-protocol.md`：自建上传接收端所需的服务端协议。
- `pyproject.toml` + ruff（只开 correctness 档）、pytest 骨架、
  pre-commit + gitleaks 门禁。
- `.claude/settings.json.example`：原文件含作者绝对路径，已出库。

### 已知限制

- **内容级脱敏器（Scrubber）尚未实现。** 对话正文里的密钥会明文落盘。
  在它落地前请把轨迹目录当作与源码同等敏感的数据，详见 `SECURITY.md`。
- 仅支持 macOS。

## [0.2.0]

- 会话维度存储：数据按 `sessions/{session_id}/` 组织，traj、raw、events 放在一起
- 采集会话起点的 git 状态（HEAD / 分支 / 工作区是否脏）与工具退出码
- 可靠上传管理器：gzip 压缩、SHA256 校验、指数退避重试、持久化重试队列
