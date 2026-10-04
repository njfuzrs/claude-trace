# 更新日志

本文件记录值得用户知道的变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 0.2.0 之前的历史只存在于 git log 中，没有回溯整理 —— 那段时期是内部工具，
> 没有外部用户，补写变更记录的收益不足以支撑代价。

## [Unreleased]

## [0.4.2] - 2026-10-04

### 新增

- traj `info.data_quality` 新增 `parse_error_actions`（tool_use 输入 JSON 截断的 action 数）与
  `parse_error_repaired`（其中被修复的）。差值 = 仍带 `_raw_partial`、不可重建的调用。
- 截断输入的有限修复：只补齐末尾未闭合的 `}` / `]`，且必须 `json.loads` round-trip、结果含工具
  `input_schema.required` 的全部键才算修好（修好的留 `_parse_repaired=true`）。截在字符串 / 数字 /
  字面量中间、或拿不到 tools schema 时一律不修。按本机历史 raw 实测，89 处截断全部截在大字段中途，
  修复数为 0 —— 这一版的价值是让截断可见，不是修好它们。
- `traj_schema` 新增 `parse_error_counts=true`。
- `raw.jsonl` 与 SSE 转发路径不变：修复只发生在 builder 导出 traj 时。

## [0.4.1] - 2026-10-04

### 新增

- traj `metadata.git_state_end` 在工作区脏时带有界 diff：`diff`（`git diff --no-color HEAD` 全文，
  含 staged、不含未跟踪文件内容）与 `diff_bytes`。超过 64 KiB 或 2 秒时 `diff=null`，
  写 `diff_omitted_reason=too_large|timeout|error` 和 `diff_stat`。起点 `git_state` 不带 diff。
- `traj_schema` 新增 `git_end_diff=true`。

### 隐私

- diff 是源码原文，会随 session.traj 落盘并在启用上传时离开本机。阈值与理由见 `git_state.py` 顶部。

## [0.4.0] - 2026-10-03

### 新增

- traj `trajectory` 新增 `message_type=user` 用户输入步，插在对应 action 之前，带 `content`（原文）、
  `content_clean`（去掉 system-reminder / 本地命令回显 / compaction 摘要 / 离开回来 recap 后的文本）、
  `is_system_noise`、`prompt_id`（对上 hook UserPromptSubmit 时为 `metadata.user_prompts` 下标）。
  不带 tools 的旁路请求（标题生成等）不产生 user step；同一句的请求重发不重复产生。
  raw.jsonl 丢了首条输入时用 hook 原文补，`content_source=hook`。
- history 条目补 `traj_step`（指向 trajectory 下标），user 条目补 `message_type` / `timestamp`。
- traj `metadata.test_runs`：按固定 argv0 表识别的测试命令及其结果，`source=inferred_argv0`；
  对应 observation 带 `is_test_command=true`。
- traj `metadata.collector_ver` / `traj_schema`：清洗侧按版本筛「切分还要不要读 raw.jsonl」。
- `tools/events_coverage.py`：只读统计 hook events 对代理会话的覆盖率。

### 兼容性

- `tools/convert_trajs.py` 三种 style 跳过 user step，旧轨迹 convert 输出不变。
- `metadata.total_steps` 现在包含 user step；`tools/filter_trajs.py` 的步数阈值与上传侧空轨迹判定
  不计 user step，口径与旧版一致。

## [0.3.2] - 2026-10-03

### 修复

- 重启后代理静默停在「未加载」状态、Claude Code 403 且不会自动恢复。事故链与对应修复：
  - 采集器退出被在途 SSE 拖住：aiohttp `shutdown_timeout` 默认 60 秒（最坏用两次），
    远超守护进程 15 秒收尾预算。两个入口统一设为 `SHUTDOWN_TIMEOUT_SEC=3`。
  - 守护进程超时强杀只杀 PyInstaller 引导进程，python 子进程成孤儿占端口。现在先杀子进程。
  - `launchctl bootout` 是异步的，紧跟的 `bootstrap` 报 `5: Input/output error`。
    restart / install / 切渠道 / 安装器 / Codex watcher / 文件监听安装都改为先等 job 从域里消失，`bootstrap` 失败重试，
    仍失败则非 0 退出并说明后果。
  - `watch-reload.sh` 在生产二进制模式下也会重启代理（改仓库 .py 根本进不了二进制）。
    现在只在 plist 指向本仓库 `proxy-daemon.sh` 时才工作；安装生产包时卸掉旧监听。
- `status` 区分「未安装」与「已安装但未加载」，后者明确提示 403、不会自动恢复。
- `install-daemon.sh restart` 遇到未加载的服务时直接加载现有 plist，不再重写 plist
  （重写会把 UPSTREAM / 上传配置重置成默认值）。

## [0.3.1] - 2026-10-03

### 新增

- traj `metadata.permission_decisions`：权限决策结果。Claude Code 2.1.276 没有决策后 hook，
  由 PermissionRequest + 是否随后执行推断 `accept`/`reject`，`source` 为
  `inferred_executed` / `inferred_not_executed`（不冒充官方 `user_permanent` 等枚举）。
  官方一旦在 hook 载荷里带上 `decision`，采集器会原样抄。
- traj `metadata.permission_mode_timeline`：hooks 相邻事件的 `permission_mode` 边沿。
  collector 另写一条合成事件 `PermissionModeChanged`；`trigger` 拿不到就省略。
- CLAUDE.md / README 写明：官方 OpenTelemetry 不是采集通道，禁止把
  `ANTHROPIC_BASE_URL` 改离 `:4000` 去「改用官方监控」。

### 变更

- 离线 CLI（`viewer` / `sync` / `rebuild_trajs` / `recover_truncated` / `filter_trajs` / `convert_trajs` / `combine_trajs` / `migrate_storage`）从仓库根迁到 `tools/`。采集进程入口与 hooks 部署文件仍平铺在根，不改包结构。调用改为 `python3 tools/<脚本>.py`。

### 修正

- **停掉 `--force-thinking` 的请求体改写。** 该开关曾把 `thinking.type=adaptive` 改写成带 `effort=max` 的对象再 `json.dumps` 整份 body 转发。`effort` 不属于 `thinking`（应在 `output_config`），且 `json.dumps` 会改变 UTF-8 / 字段顺序；Claude Code 2.1.275+ 因此收到 `Invalid tool use format` 400。代理现在永远按原始字节转发 messages 请求。CLI / plist / `channels.json` 里的开关保留为废弃 no-op，以免旧 launchd 配置 unrecognized arguments。
- CI 补装 `pytest-asyncio`（`asyncio_mode = auto` 依赖它，缺了 async 测试在 GitHub 上全红）；`test_远程失败不覆盖已有安装` 在非 macOS 上 skip（`install.sh` 会先以「仅支持 macOS」退出）。
- README：补升级步骤；`--save-raw` 默认改为 false；数据目录改成 `sessions/{id}/`；会话超时 FAQ 从 5 分钟改为 30 分钟；渠道切换区分安装版 `claude-trace switch` 与源码 `switch-channel.sh`。`./start.sh` 从「最简单的一键启动」改成源码临时入口，并写明会杀掉占用 4000 的进程。

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
