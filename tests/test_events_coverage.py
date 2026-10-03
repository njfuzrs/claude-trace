"""tools/events_coverage.py 分类口径测试

覆盖率的分母只取 main + orphan。分类错了数字就没有意义，所以锁死口径。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import events_coverage as ec  # noqa: E402

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"
SID_C = "cccccccc-1111-2222-3333-444444444444"
SID_GONE = "dddddddd-1111-2222-3333-444444444444"


def _user_id(sid):
    return json.dumps({"device_id": "x", "account_uuid": "", "session_id": sid})


def _mk(root, name, req, events=None, ts="2026-09-20T10:00:00"):
    d = root / name
    d.mkdir()
    (d / "raw.jsonl").write_text(json.dumps({"timestamp": ts, "request": req}) + "\n")
    if events is not None:
        (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_提取两种user_id格式():
    assert ec.extract_request_session_id({"metadata": {"user_id": _user_id(SID_A)}}) == SID_A
    concat = f"user_abc_account__session_{SID_B}"
    assert ec.extract_request_session_id({"metadata": {"user_id": concat}}) == SID_B
    assert ec.extract_request_session_id({"metadata": {}}) == ""
    assert ec.extract_request_session_id({}) == ""


def test_分类与覆盖率(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    hooks = tmp_path / "hooks"
    hooks.mkdir()

    # main：目录名 == 请求 sid，有 UserPromptSubmit
    _mk(sessions, SID_A, {"max_tokens": 1, "metadata": {"user_id": _user_id(SID_A)}},
        events=[{"event": "SessionStart"}, {"event": "UserPromptSubmit"}])
    # main：events 只有 SessionStart，算非空但不含 prompt
    _mk(sessions, SID_C, {"max_tokens": 1, "metadata": {"user_id": _user_id(SID_C)}},
        events=[{"event": "SessionStart"}])
    # child：sub-agent 带父会话 sid
    _mk(sessions, "child-1", {"max_tokens": 1, "metadata": {"user_id": _user_id(SID_A)}})
    # orphan：父 sid 只有 hook 文件、没有目录 —— 策略 0 之前的关联失败
    (hooks / f"{SID_GONE}.jsonl").write_text('{"event": "UserPromptSubmit"}\n')
    _mk(sessions, "random-1", {"max_tokens": 1, "metadata": {"user_id": _user_id(SID_GONE)}})
    # probe：count_tokens 请求没有 max_tokens
    _mk(sessions, "probe-1", {"metadata": {"user_id": _user_id(SID_B)}})
    # legacy：没有 session_id
    _mk(sessions, "legacy-1", {"max_tokens": 1})
    # 没有 raw.jsonl 的目录不计
    (sessions / "empty").mkdir()

    rows = ec.collect(sessions, hooks)
    cats = {r["sid"]: r["category"] for r in rows}
    assert cats == {
        SID_A: "main", SID_C: "main", "child-1": "child",
        "random-1": "orphan", "probe-1": "probe", "legacy-1": "legacy",
    }

    s = ec.summarize(rows)
    assert s["denominator"] == 3
    assert s["events_nonempty"] == 2
    assert s["has_user_prompt"] == 1
    assert s["by_category"]["orphan"] == 1


def test_since过滤(tmp_path):
    _mk(tmp_path, SID_A, {"max_tokens": 1, "metadata": {"user_id": _user_id(SID_A)}},
        ts="2026-08-01T00:00:00")
    _mk(tmp_path, SID_B, {"max_tokens": 1, "metadata": {"user_id": _user_id(SID_B)}},
        ts="2026-09-20T00:00:00")
    rows = ec.collect(tmp_path, None, since="2026-09-01")
    assert [r["sid"] for r in rows] == [SID_B]


def test_坏行不崩(tmp_path):
    d = tmp_path / SID_A
    d.mkdir()
    (d / "raw.jsonl").write_text("not json\n")
    rows = ec.collect(tmp_path, None)
    assert rows[0]["category"] == "unreadable"
    assert ec.summarize(rows)["denominator"] == 0
