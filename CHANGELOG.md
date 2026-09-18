# 更新日志

本文件记录值得用户知道的变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 0.2.0 之前的历史只存在于 git log 中，没有回溯整理 —— 那段时期是内部工具，
> 没有外部用户，补写变更记录的收益不足以支撑代价。

## [Unreleased]

### 修正

- README：补升级步骤；`--save-raw` 默认改为 false；数据目录改成 `sessions/{id}/`；会话超时 FAQ 从 5 分钟改为 30 分钟；渠道切换区分安装版 `claude-trace switch` 与源码 `switch-channel.sh`。

## [0.3.0] - 2026-09-18

第一次对外发版。开源改造（9 月 14 日）与采集/上传修复（9 月 17 日）此前都堆在 `[Unreleased]`，从未切过版本；本机曾在同一个 `0.2.0` 数字下跑过相差 13 天、行为完全不同的两份二进制。不要在 `v0.2.0` 上覆盖 asset。

### 新增

- **GitHub Releases 安装入口。** `curl -fsSL …/install.sh | bash` 是对外主路径；源码路径标明开发用、需先 `./build/build.sh`。
- 二进制与源码入口都支持 `--version`，health 端点带 `version` / `build` 字段。跑着的进程能自报是哪一次构建。
- `./build/release.sh --bump x.y.z`：一次改 `version` 文件、`pyproject.toml`、CHANGELOG，禁止只改一处。
- 发布后验收：真的 curl 一次 assets，并核对安装器候选名与构建产物文件名一致。覆盖已有 Release 需 `ALLOW_CLOBBER=true`。
- `docs/upload-protocol.md`：自建上传接收端所需的服务端协议。
- 治理文件：`LICENSE`（MIT）、`CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、CI、pre-commit + gitleaks。

### 变更

- **上传改为 opt-in，移除全部内置端点与凭据。** 需同时配置 `TRAJ_PLATFORM_URL` 与 `TRAJ_UPLOAD_TOKEN` 才启用，交互式安装默认「否」。
- **身份字段默认留空，不再回退到真实身份。** `TRAJ_USER_ID` / `TRAJ_DEVICE_ID` 未配置即为空串，不再回退到系统用户名与主机名。
- 上游 API 默认地址改为 `https://api.anthropic.com`。
- 会话超时默认从 300 秒放宽到 1800 秒。5 分钟窗口会把开会/思考中的会话切碎，实测 140 个会话因此损坏。
- `TRAJ_CLEANUP_AFTER_UPLOAD` 默认 `false`（上传成功也保留本地）。曾经默认 `true`，409 幂等 bug 把「服务端拒绝覆盖」也算成成功，实测 7247 个会话目录只剩 `.uploaded` 标记。
- 启动补传默认开启（`TRAJ_BACKFILL_ON_START=true`）：启动时扫描盘上未上云的会话并补齐。
- 开发入口与生产入口合一：`proxy-daemon.sh` 调 `trace_agent.py`，不再直接调 `proxy.py`。
- `install-daemon.sh install` 不再默认装 watch-reload。开发时加 `--watch`。生产二进制模式禁止装它。
- `claude-trace start` 写 plist 时只更新已知键，保留人工加过的其他键。

### 修正

- **安装器远程 tarball 文件名对不上。** GitHub tag 路径段是 `v0.3.0`，正则要求以数字开头，带版本号的候选名被跳过，只去下一个构建从不产出的无版本名。剥掉前导 `v` 后，候选列表含 `claude-trace-${VERSION}-darwin-${ARCH}.tar.gz`。
- **安装器本地分支不可达。** 只要能读到仓库的 `version` 文件，`RELEASE_BASE` 就被默认填上，本地拷贝分支成了死代码。现在仓库内且未设 `RELEASE_BASE` 走本地安装。
- `claude-trace restart` / `install-daemon.sh restart` / `claude-trace switch` / `switch-channel.sh` 改为 bootout + bootstrap，不再 `kickstart` 或 `unload/load`。kickstart 不重读 plist；unload/load 对 bootstrap 装上的 job 经常是空操作。
- 安装器先把新包落到临时目录，校验成功后再停旧服务、再替换。远程 404 时不停正在跑的采集，也不覆盖正在映射的 Mach-O。
- 安装版 plist 补上 `ExitTimeOut=25` 与 `TRAJ_BACKFILL_ON_START`，与开发版对齐。
- 拒绝用更短的轨迹覆盖已落盘版本（`allow_shrink=False`）+ 原子写。会话超时被清理后再提问，不再把几小时的完整轨迹冲成只含最后几步。
- 会话复活从 `raw.jsonl` 重建，接住 SIGTERM / SIGHUP。
- 上传 409 幂等不再锁死、413 不再无限重试；启动补传与积压可观测（`--upload-status`、health 的 `upload.sessions_not_uploaded`）。
- 守护进程等采集器收尾再退出（最多 15 秒），launchd `ExitTimeOut=25`。老实现 kill 完立刻 exit，收尾被砍在半路。
- PyInstaller spec 补上运行期才 import 的 `merger` / `git_state` / `rebuild_trajs`，并把 `version` 文件打进包内。
- **README 的脱敏承诺改为如实描述。** 只脱三个请求头，消息体不做内容级过滤。

### 已知限制

- **内容级脱敏器（Scrubber）尚未实现。** 对话正文里的密钥会明文落盘。详见 `SECURITY.md`。
- 仅支持 macOS。目前只在本机 arm64 打包装，没有 CI 自动发版。
- 上传若指向明文 HTTP，token 仍明文传输。

## [0.2.0]

- 会话维度存储：数据按 `sessions/{session_id}/` 组织，traj、raw、events 放在一起
- 采集会话起点的 git 状态（HEAD / 分支 / 工作区是否脏）与工具退出码
- 可靠上传管理器：gzip 压缩、SHA256 校验、指数退避重试、持久化重试队列
