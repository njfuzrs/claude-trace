#!/usr/bin/env python3
"""版本号单一事实源测试

`version` 文件被声明为单一事实源，但 `pyproject.toml` 另写了一份，靠注释
「改版本时两处同步」—— 人肉同步，改一处漏一处是注定的。这里把两处锁死，
再锁住「二进制能自报版本」这条能力：它是排查「我到底在跑哪一份」的唯一依据。
"""

import re
import subprocess
import sys
from pathlib import Path

from version_info import UNKNOWN, resolve_version, version_string

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z._-]+)?$")


def _pyproject_version() -> str:
    path = ROOT / "pyproject.toml"
    # 优先用 tomllib（3.11+）：文件顶部的注释里也出现了「[project]」字样，
    # 按文本切段会切到注释上，只有真正解析 TOML 才不会认错段头。
    try:
        import tomllib
    except ModuleNotFoundError:  # 3.10 没有 tomllib，退回正则找行首段头
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        assert m, "pyproject.toml 里找不到 [project] 段"
        v = re.search(r'^\s*version\s*=\s*"([^"]+)"', m.group(1), re.M)
        assert v, "pyproject.toml 的 [project] 段里找不到 version"
        return v.group(1)

    with path.open("rb") as f:
        data = tomllib.load(f)
    version = data.get("project", {}).get("version")
    assert version, "pyproject.toml 的 [project] 段里找不到 version"
    return version


def test_version文件与pyproject一致():
    """★核心：两处版本号必须相同，否则发布产物与包元数据会对不上"""
    file_version = (ROOT / "version").read_text(encoding="utf-8").strip()
    assert file_version == _pyproject_version(), (
        f"version 文件是 {file_version}，pyproject.toml 是 {_pyproject_version()}。"
        "两处必须同步 —— 改版本请用 ./build/release.sh --bump <x.y.z>"
    )


def test_version文件是合法语义化版本():
    file_version = (ROOT / "version").read_text(encoding="utf-8").strip()
    assert SEMVER.match(file_version), f"{file_version!r} 不是合法的 x.y.z"


def test_resolve_version读到仓库版本():
    assert resolve_version() == (ROOT / "version").read_text(encoding="utf-8").strip()
    assert resolve_version() != UNKNOWN


def test_version_string包含版本号与运行形态():
    s = version_string()
    assert resolve_version() in s
    # 源码运行时必须标明是 source，否则分不清「跑的是源码还是装好的二进制」
    assert "source" in s


def test_两个入口都支持version参数():
    """--version 是「跑着的进程自报构建」的入口，不能退化"""
    for entry in ("trace_agent.py", "proxy.py"):
        out = subprocess.run(
            [sys.executable, str(ROOT / entry), "--version"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, f"{entry} --version 退出码 {out.returncode}: {out.stderr}"
        assert resolve_version() in out.stdout, f"{entry} --version 没打印版本号: {out.stdout!r}"


def test_spec把version文件打进包内():
    """打包后 resolve_version() 依赖 datas 里的 version 文件，漏了就只能报 unknown"""
    spec = (ROOT / "build" / "claude-trace-proxy.spec").read_text(encoding="utf-8")
    assert "'version'" in spec and "datas=[(" in spec, "spec 的 datas 里必须包含 version 文件"
    assert "version_info" in spec, "spec 的 hiddenimports 里必须包含 version_info"


def test_changelog为发版留有未发布区段或已切版本():
    """CHANGELOG 必须能对应上当前版本，否则发布说明是空的"""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    file_version = (ROOT / "version").read_text(encoding="utf-8").strip()
    assert f"[{file_version}]" in text or "[Unreleased]" in text, (
        f"CHANGELOG.md 既没有 [{file_version}] 区段，也没有 [Unreleased]"
    )
