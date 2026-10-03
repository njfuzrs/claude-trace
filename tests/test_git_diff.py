"""会话终点有界 git diff（S2-2）

用真实临时仓库跑 git，锁住三条行为：
脏工作区小改动 → diff 含 hunk；超限 → 只留 diff_stat；干净 / 起点 → 不跑 diff。
"""

import subprocess

import pytest

import git_state
from git_state import collect_git_diff, collect_git_state, collect_git_state_end


def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _run(tmp_path, "init", "-q")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    _run(tmp_path, "add", "a.py")
    _run(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_脏工作区小改动_终点含hunk(repo):
    (repo / "a.py").write_text("x = 2\n")
    end = collect_git_state_end(str(repo), source="hook")
    assert end["dirty"] is True
    assert "a.py" in end["diff"]
    assert "+x = 2" in end["diff"]
    assert end["diff_bytes"] == len(end["diff"].encode())
    assert "diff_omitted_reason" not in end


def test_staged改动也在diff里(repo):
    (repo / "a.py").write_text("x = 3\n")
    _run(repo, "add", "a.py")
    end = collect_git_state_end(str(repo))
    assert "+x = 3" in end["diff"]


def test_超限只留stat(repo):
    (repo / "a.py").write_text("".join(f"line{i} = {i}\n" for i in range(200)))
    state = collect_git_state(str(repo))
    r = collect_git_diff(str(repo), state, max_bytes=100)
    assert r["diff"] is None
    assert r["diff_omitted_reason"] == "too_large"
    assert r["diff_bytes"] > 100
    assert "a.py" in r["diff_stat"]


def test_超时只留stat(repo, monkeypatch):
    (repo / "a.py").write_text("x = 4\n")
    state = collect_git_state(str(repo))

    def boom(args, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

    monkeypatch.setattr(git_state, "_git_bytes", boom)
    r = collect_git_diff(str(repo), state)
    assert r["diff"] is None
    assert r["diff_omitted_reason"] == "timeout"
    assert "a.py" in r["diff_stat"]


def test_干净工作区不跑diff(repo, monkeypatch):
    called = []
    monkeypatch.setattr(git_state, "_git_bytes", lambda *a, **k: called.append(a))
    end = collect_git_state_end(str(repo))
    assert end["dirty"] is False
    assert "diff" not in end
    assert called == []


def test_只有未跟踪文件时不带内容(repo):
    # git diff HEAD 不含未跟踪文件：文件名在 dirty_file_list，内容不落
    (repo / "secret.txt").write_text("TOKEN=abc\n")
    end = collect_git_state_end(str(repo))
    assert end["dirty"] is True
    assert end["diff"] == ""
    assert "TOKEN" not in end["diff"]


def test_起点快照不带diff(repo):
    (repo / "a.py").write_text("x = 5\n")
    start = collect_git_state(str(repo))
    assert "diff" not in start
    assert "diff_omitted_reason" not in start


def test_dirty未知或非仓库返回空(tmp_path):
    assert collect_git_diff(str(tmp_path), {"dirty": None, "head": "abc"}) == {}
    assert collect_git_diff("", {"dirty": True, "head": "abc"}) == {}
    assert collect_git_state_end(str(tmp_path)) == {}


def test_命令失败不抛异常(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(git_state, "_git_bytes", boom)
    r = collect_git_diff(str(repo), {"dirty": True, "head": "abc"})
    assert r == {"diff": None, "diff_omitted_reason": "error"}
