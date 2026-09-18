#!/usr/bin/env python3
"""sync.py 闸门：缺 URL/token 退出；身份不回退系统用户名/主机名。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "tools" / "sync.py"


def _run(env_updates: dict, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # 清掉可能从开发机继承的真实凭据，避免测试误打网
    for k in ("TRAJ_PLATFORM_URL", "TRAJ_UPLOAD_TOKEN", "TRAJ_USER_ID", "TRAJ_DEVICE_ID"):
        env.pop(k, None)
    env.update(env_updates)
    return subprocess.run(
        [sys.executable, str(SYNC), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def test_missing_url_exits():
    proc = _run({"TRAJ_UPLOAD_TOKEN": "tok"})
    assert proc.returncode != 0
    assert "TRAJ_PLATFORM_URL" in (proc.stderr + proc.stdout)


def test_missing_token_exits():
    proc = _run({"TRAJ_PLATFORM_URL": "http://example.invalid/traj"})
    assert proc.returncode != 0
    assert "TRAJ_UPLOAD_TOKEN" in (proc.stderr + proc.stdout)


def test_source_does_not_call_getlogin():
    """只认 TRAJ_USER_ID / TRAJ_DEVICE_ID，不回退系统身份（与 uploader 同一条铁律）。"""
    src = SYNC.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "getlogin" not in code
    assert "platform.node" not in code
    assert "import platform" not in code
