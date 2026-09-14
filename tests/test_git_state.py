"""git status --porcelain 解析测试

_parse_porcelain 是纯函数、无 IO，最容易起步。它决定了轨迹里
「会话起点工作区是否脏」这个字段的正确性 —— 该字段事后无从重建，
错了下游就会把脏工作区的会话当成干净起点用。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from git_state import _MAX_DIRTY_FILES, _parse_porcelain  # noqa: E402


def test_空输出即干净工作区():
    r = _parse_porcelain("")
    assert r["modified_files"] == 0
    assert r["untracked_files"] == 0
    assert r["staged_files"] == 0
    assert r["dirty_file_list"] == []
    assert r["dirty_file_list_truncated"] is False


def test_未跟踪文件计入untracked():
    r = _parse_porcelain("?? new_file.py")
    assert r["untracked_files"] == 1
    assert r["modified_files"] == 0
    assert r["staged_files"] == 0


def test_工作区修改未stage():
    # " M path"：索引位为空格 = 未 stage
    r = _parse_porcelain(" M builder.py")
    assert r["modified_files"] == 1
    assert r["staged_files"] == 0


def test_已stage的修改():
    # "M  path"：索引位非空格 = 已 stage
    r = _parse_porcelain("M  builder.py")
    assert r["modified_files"] == 1
    assert r["staged_files"] == 1


def test_混合状态():
    text = "\n".join([
        "M  staged.py",       # 已 stage
        " M unstaged.py",     # 仅工作区改动
        "MM both.py",         # stage 后又改
        "?? untracked.py",
        "A  added.py",        # 新增已 stage
        "D  deleted.py",
    ])
    r = _parse_porcelain(text)
    assert r["untracked_files"] == 1
    assert r["modified_files"] == 5
    assert r["staged_files"] == 4   # M / MM / A / D 的索引位均非空格
    assert len(r["dirty_file_list"]) == 6


def test_路径与状态码被保留():
    r = _parse_porcelain(" M src/deep/path.py")
    assert r["dirty_file_list"] == [{"status": " M", "path": "src/deep/path.py"}]


def test_过短行被跳过():
    """porcelain 行至少 3 字符（XY + 空格）；残行不应让解析崩溃。"""
    r = _parse_porcelain("\n".join(["M", "", " M ok.py", "  "]))
    assert r["modified_files"] == 1
    assert r["dirty_file_list"] == [{"status": " M", "path": "ok.py"}]


def test_文件列表在超限时截断且标记():
    text = "\n".join(f" M file{i}.py" for i in range(_MAX_DIRTY_FILES + 10))
    r = _parse_porcelain(text)
    # 计数不截断，只截断明细列表
    assert r["modified_files"] == _MAX_DIRTY_FILES + 10
    assert len(r["dirty_file_list"]) == _MAX_DIRTY_FILES
    assert r["dirty_file_list_truncated"] is True


def test_刚好等于上限时不标记截断():
    text = "\n".join(f" M file{i}.py" for i in range(_MAX_DIRTY_FILES))
    r = _parse_porcelain(text)
    assert len(r["dirty_file_list"]) == _MAX_DIRTY_FILES
    assert r["dirty_file_list_truncated"] is False
