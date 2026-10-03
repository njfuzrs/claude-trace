"""安装器分流与文件名拼接测试

这两处曾经都错且无人发现：
- 文件名：basename 拿到 GitHub tag 路径段 `v0.3.0`，正则要求以数字开头，
  带版本号的候选名被整段跳过，只去下一个构建从不产出的无版本名，发了 Release 照样 404。
- 分流：只要能读到仓库的 version 文件，RELEASE_BASE 就被默认填上，
  本地安装分支成了死代码，README 写的 `bash dist/install.sh` 在 Releases 为空时失败。

验证它们不该要求真装一遍，所以 install.sh 提供 --print-plan，只打印判定结果。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "dist" / "install.sh"


def _plan(env: dict | None = None, cwd: Path | None = None, via_stdin: bool = False) -> dict:
    merged = {**os.environ, **(env or {})}
    # 测试必须自带 ARCH，否则结果依赖跑测试的机器
    merged.setdefault("ARCH", "arm64")
    if via_stdin:
        proc = subprocess.run(
            ["bash", "-s", "--", "--print-plan"],
            input=INSTALLER.read_text(encoding="utf-8"),
            capture_output=True, text=True, cwd=cwd or "/tmp",
            env=merged, timeout=10,
        )
    else:
        proc = subprocess.run(
            ["bash", str(INSTALLER), "--print-plan"],
            capture_output=True, text=True, cwd=cwd or ROOT,
            env=merged, timeout=10,
        )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out: dict = {"candidates": []}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k == "candidate":
            out["candidates"].append(v)
        else:
            out[k] = v
    return out


def test_仓库内未设RELEASE_BASE走本地安装():
    """★核心：README 的 `bash dist/install.sh` 必须走本地拷贝，而不是去 github.com"""
    plan = _plan()
    assert plan["install_mode"] == "local"
    assert plan["repo_dir"] == str(ROOT)
    assert plan["release_base"] == ""
    assert plan["candidates"] == []


def test_显式RELEASE_BASE走远程且剥掉v前缀():
    """★核心：tag 路径段是 v0.3.0，候选名必须是 claude-trace-0.3.0-darwin-arm64.tar.gz"""
    plan = _plan({
        "RELEASE_BASE": "https://github.com/njfuzrs/claude-trace/releases/download/v0.3.0",
        "VERSION": "",  # 让脚本自己读仓库 version 文件；顺序上 tag 仍优先
        "ARCH": "arm64",
    })
    assert plan["install_mode"] == "remote"
    assert plan["release_base"].endswith("/v0.3.0")
    assert "claude-trace-0.3.0-darwin-arm64.tar.gz" in plan["candidates"]
    # 带版本号的名字必须排在不带版本号的兼容名前面
    assert plan["candidates"][0] == "claude-trace-0.3.0-darwin-arm64.tar.gz"
    assert plan["candidates"][-1] == "claude-trace-darwin-arm64.tar.gz"


def test_管道安装探测不到仓库走远程():
    """curl | bash 时 $0 是 bash，探测不到仓库，必须落到远程"""
    plan = _plan({"VERSION": "0.3.0", "ARCH": "arm64"}, via_stdin=True)
    assert plan["install_mode"] == "remote"
    assert plan["repo_dir"] == ""
    assert plan["release_base"].endswith("/v0.3.0")
    assert plan["candidates"][0] == "claude-trace-0.3.0-darwin-arm64.tar.gz"


def test_latest路径不把download当版本号():
    """latest/download 的最后一段是 download，不能被当成 0.0.0 拼进文件名"""
    plan = _plan({
        "RELEASE_BASE": "https://github.com/njfuzrs/claude-trace/releases/latest/download",
        "VERSION": "",
        "ARCH": "x86_64",
    })
    assert plan["install_mode"] == "remote"
    for name in plan["candidates"]:
        assert "download" not in name
    assert "claude-trace-darwin-x86_64.tar.gz" in plan["candidates"]


def test_发布版注入VERSION后管道安装请求带版本的包():
    """release.sh 会把 VERSION 固化进发布版。固化后经管道运行必须请求构建产出的那个名字。"""
    injected = INSTALLER.read_text(encoding="utf-8").replace(
        'VERSION="${VERSION:-$(cat "$(dirname "$0")/../version" 2>/dev/null || echo "")}"',
        'VERSION="${VERSION:-0.3.0}"',
        1,
    )
    proc = subprocess.run(
        ["bash", "-s", "--", "--print-plan"],
        input=injected, capture_output=True, text=True, cwd="/tmp",
        env={**os.environ, "ARCH": "arm64"}, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "candidate=claude-trace-0.3.0-darwin-arm64.tar.gz" in proc.stdout
    assert "install_mode=remote" in proc.stdout
    assert "release_base=" in proc.stdout and "/v0.3.0" in proc.stdout


def test_cleanup默认false写在安装器里():
    """路径 A 重装不能把「默认保留本地」改回「默认删本地」"""
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"cleanup_after_upload": False' in text
    assert "cleanup_after_upload': True" not in text
    assert "cleanup_after_upload', 'true')" not in text
    # 兜底也必须是 False
    assert "flag(upload.get('cleanup_after_upload'), False)" in text


def test_plist模板含ExitTimeOut与启动补传():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "<key>ExitTimeOut</key>" in text
    assert "TRAJ_BACKFILL_ON_START" in text


def test_cli重启不是kickstart():
    """kickstart 不重读 plist，改完配置执行 restart 会看到「已重启」但跑的还是旧配置"""
    cli = (ROOT / "dist" / "claude-trace").read_text(encoding="utf-8")
    daemon = (ROOT / "install-daemon.sh").read_text(encoding="utf-8")
    switcher = (ROOT / "switch-channel.sh").read_text(encoding="utf-8")
    # 注释里可以提 kickstart / unload（解释为什么不用），实际命令行不得再调用它们
    for text in (cli, daemon, switcher):
        assert "launchctl kickstart" not in text
        assert "launchctl unload" not in text
        assert "launchctl load " not in text
        assert "launchctl bootout" in text and "launchctl bootstrap" in text


def test_bootout之后先等job消失再bootstrap():
    """bootout 是异步的：采集器收尾最多 15 秒，期间 bootstrap 报 5: Input/output error，
    job 停在未加载状态，KeepAlive 管不到，服务静默死掉（v0.3.1 watch-reload 触发过）。
    restart 路径必须等 job 从域里消失，bootstrap 必须有重试且失败时非 0 退出。"""
    for rel in ("install-daemon.sh", "dist/claude-trace"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "wait_job_gone()" in text, rel
        assert "bootstrap_job()" in text, rel
        body = text[text.index("do_restart() {"):]
        body = body[: body.index("\n}\n")]
        assert body.index("launchctl bootout") < body.index("wait_job_gone"), rel
        assert "bootstrap_job" in body, rel
        assert 'launchctl bootstrap "gui' not in body, rel
    for rel in ("switch-channel.sh", "dist/install.sh"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert 'launchctl print "gui/$(id -u)/$LABEL"' in text, rel


def test_守护进程强杀时连孙进程一起杀():
    """PyInstaller onefile 是引导进程 + python 进程两层，SIGKILL 不转发。
    只杀 CHILD_PID 会留下占着 4000 端口的孤儿，新实例撞 EADDRINUSE。"""
    for rel in ("proxy-daemon.sh", "dist/proxy-daemon.sh"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        kill_child = text.index('pkill -KILL -P "$CHILD_PID"')
        kill_self = text.index('kill -KILL "$CHILD_PID"')
        assert kill_child < kill_self, rel


def test_watchReload在生产二进制模式下不重启():
    """watch-reload 监听仓库 .py，生产跑的是 ~/.claude-trace 的二进制，重启纯属打断采集。
    启动时和每次重启前都要核对 plist 是否指向本仓库 proxy-daemon.sh。"""
    text = (ROOT / "watch-reload.sh").read_text(encoding="utf-8")
    guard = 'grep -qF "$SCRIPT_DIR/proxy-daemon.sh" "$PROXY_PLIST"'
    assert text.count(guard) == 2
    assert text.index(guard) < text.index("while true")
    loop = text[text.index("while true"):]
    assert loop.index(guard) < loop.index("install-daemon.sh\" restart")
    # 安装生产包时卸掉旧监听
    installer = INSTALLER.read_text(encoding="utf-8")
    assert 'launchctl bootout "gui/$(id -u)/com.claude-trace.watch-reload"' in installer


def test_status区分已安装但未加载():
    """plist 在、job 不在 = bootstrap 失败后的死状态，KeepAlive 不会救。
    必须说后果（403、不会自动恢复），不能和「未安装」混在一句里。"""
    for rel in ("install-daemon.sh", "dist/claude-trace"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        body = text[text.index("do_status() {"):]
        body = body[: body.index("\n}\n")]
        assert "未安装或未运行" not in body, rel
        assert "已安装但服务未加载" in body and "403" in body and "不会自动恢复" in body, rel


def test_restart对未加载的服务不重写plist():
    """job 未加载但 plist 在时，restart 必须直接加载现有 plist，
    不能走 install —— 那会用当前 shell 环境重写 plist，把 UPSTREAM / 上传配置重置成默认。"""
    text = (ROOT / "install-daemon.sh").read_text(encoding="utf-8")
    body = text[text.index("do_restart() {"):]
    head = body[: body.index("launchctl bootout")]
    assert head.index('[ -f "$PLIST_PATH" ]') < head.index("do_install")
    assert "bootstrap_job" in head


def test_restart回显不把全角括号粘进变量名():
    """macOS /bin/bash 3.2 + set -u：`$PLIST_PATH）` 会被当成名为 PLIST_PATH\\xef\\xbc\\x89 的变量。
    bootstrap 已经跑完，echo 再炸，操作者会以为 restart 失败。必须用 ${PLIST_PATH}。"""
    for rel in ("install-daemon.sh", "dist/claude-trace"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "$PLIST_PATH）" not in text, rel
        assert "${PLIST_PATH}" in text, rel


def test_watchReload默认不装():
    """生产二进制模式禁止默认装文件监听"""
    text = (ROOT / "install-daemon.sh").read_text(encoding="utf-8")
    assert "INSTALL_WATCH=false" in text
    assert "--watch" in text
    # 默认路径不得无条件调用 install_watch
    assert "if [ \"$INSTALL_WATCH\" = true ]; then" in text


def test_开发入口走trace_agent而不是proxy():
    """两套入口曾经各修各的；开发 daemon / start.sh 必须与生产走同一入口"""
    daemon = (ROOT / "proxy-daemon.sh").read_text(encoding="utf-8")
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert 'python3 "$SCRIPT_DIR/trace_agent.py"' in daemon
    assert 'python3 "$SCRIPT_DIR/proxy.py"' not in daemon
    assert "trace_agent.py" in start
    assert 'python3 "$SCRIPT_DIR/proxy.py"' not in start
    hooks = (ROOT / "setup_hooks.py").read_text(encoding="utf-8")
    assert "python3 trace_agent.py" in hooks
    assert "python3 proxy.py" not in hooks
    watch = (ROOT / "watch-reload.sh").read_text(encoding="utf-8")
    assert "trace_agent.py" in watch


def test_cli_start合并plist而不是整文件覆盖():
    """手工加进 plist 的键不能被 claude-trace start 抹掉"""
    text = (ROOT / "dist" / "claude-trace").read_text(encoding="utf-8")
    assert "plistlib.load" in text
    assert 'cat > "$PLIST_PATH"' not in text
    assert "data.update(known)" in text


def test_channels_example默认不删本地():
    text = (ROOT / "channels.json.example").read_text(encoding="utf-8")
    assert '"cleanup_after_upload": false' in text
    assert '"cleanup_after_upload": true' not in text
    assert '"backfill_on_start": true' in text


@pytest.mark.skipif(sys.platform != "darwin", reason="install.sh 仅支持 macOS，Linux 会在平台检查处直接退出")
def test_远程失败不覆盖已有安装(tmp_path):
    """远程 404 / 解析失败时，INSTALL_DIR 里已有的二进制和 version 文件必须原样留下。"""
    install_dir = tmp_path / "install"
    (install_dir / "bin").mkdir(parents=True)
    (install_dir / "bin" / "claude-trace-proxy").write_text("KEEP-ME", encoding="utf-8")
    (install_dir / "version").write_text("0.2.0\n", encoding="utf-8")
    env = {
        **os.environ,
        "INSTALL_DIR": str(install_dir),
        "SKIP_SERVICE": "true",
        "ANTHROPIC_AUTH_TOKEN": "sk-test",
        "RELEASE_BASE": "https://example.invalid/no-such-release",
        "VERSION": "0.3.0",
        "ARCH": "arm64",
    }
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--non-interactive"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "https://example.invalid/no-such-release" in combined
    assert "已有安装未被改动" in combined
    assert (install_dir / "bin" / "claude-trace-proxy").read_text() == "KEEP-ME"
    assert (install_dir / "version").read_text().strip() == "0.2.0"


def test_远程失败退出在停服务之前():
    """源码顺序：远程失败必须先 exit，停服务的 bootout 在这之后。
    否则 Release 404 会把正在采集的进程掐掉，Claude Code 跟着 403。"""
    text = INSTALLER.read_text(encoding="utf-8")
    fail_at = text.find("已有安装未被改动")
    stop_at = text.find("已停止旧服务")
    assert fail_at != -1 and stop_at != -1
    assert fail_at < stop_at, "停服务写在远程失败 exit 之前，404 会掐正在跑的采集"


def test_发布脚本不以label伪装安装器文件名():
    """gh 的 path#label 只改展示名。下载 URL 用的是磁盘文件名。
    v0.3.0 第一次上传写成 install.sh.release#install.sh，curl | bash 404。"""
    text = (ROOT / "build" / "release.sh").read_text(encoding="utf-8")
    # 注释里会提到这个错误写法，真正传给 gh 的路径 basename 必须是 install.sh
    assert 'ASSETS=("${TARBALLS[@]}" "${INSTALL_RELEASE}#install.sh")' not in text
    assert 'INSTALL_ASSET="$INSTALL_ASSET_DIR/install.sh"' in text
    assert "prepare_install_asset" in text
