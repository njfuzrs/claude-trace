"""权限决策元数据（S1）：推断 / 真实字段 / 模式边沿 / SFT convert 不变。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from builder import (
    SessionMetadata,
    apply_hook_events_to_metadata,
    build_trajectory,
    infer_permission_decisions,
    infer_permission_mode_timeline,
)
import collector

ROOT = Path(__file__).resolve().parent.parent


def _load_convert():
    path = ROOT / "tools" / "convert_trajs.py"
    spec = importlib.util.spec_from_file_location("convert_trajs", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


convert_trajs = _load_convert()


def _ev(event, **kwargs):
    row = {"event": event, "timestamp": kwargs.pop("timestamp", "t")}
    row.update(kwargs)
    return row


# ── 决策推断 ──────────────────────────────────────────────


def test_permission_request后有Post则推断accept():
    events = [
        _ev("PreToolUse", tool_name="Bash", tool_use_id="id-1", timestamp="1"),
        _ev("PermissionRequest", tool_name="Bash", timestamp="2"),
        _ev("PostToolUse", tool_name="Bash", tool_use_id="id-1", timestamp="3"),
    ]
    got = infer_permission_decisions(events)
    assert len(got) == 1
    assert got[0]["tool_use_id"] == "id-1"
    assert got[0]["tool_name"] == "Bash"
    assert got[0]["decision"] == "accept"
    assert got[0]["source"] == "inferred_executed"


def test_permission_request后无执行则推断reject():
    events = [
        _ev("PreToolUse", tool_name="Bash", tool_use_id="id-2", timestamp="1"),
        _ev("PermissionRequest", tool_name="Bash", timestamp="2"),
        _ev("UserPromptSubmit", prompt="换一个", timestamp="3"),
    ]
    got = infer_permission_decisions(events)
    assert len(got) == 1
    assert got[0]["tool_use_id"] == "id-2"
    assert got[0]["decision"] == "reject"
    assert got[0]["source"] == "inferred_not_executed"


def test_PostToolUseFailure也算已执行():
    events = [
        _ev("PreToolUse", tool_name="Bash", tool_use_id="id-3", timestamp="1"),
        _ev("PermissionRequest", tool_name="Bash", timestamp="2"),
        _ev("PostToolUseFailure", tool_name="Bash", tool_use_id="id-3", timestamp="3"),
    ]
    got = infer_permission_decisions(events)
    assert got[0]["decision"] == "accept"
    assert got[0]["source"] == "inferred_executed"


def test_真实decision字段优先于推断():
    events = [
        _ev("PreToolUse", tool_name="Bash", tool_use_id="id-4", timestamp="1"),
        _ev(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="id-4",
            decision="accept",
            decision_source="user_permanent",
            timestamp="2",
        ),
        _ev("PermissionRequest", tool_name="Bash", timestamp="3"),
    ]
    got = infer_permission_decisions(events)
    assert len(got) == 1
    assert got[0]["source"] == "user_permanent"
    assert got[0]["decision"] == "accept"
    assert got[0]["tool_use_id"] == "id-4"


def test_SessionStart的source不当决策来源():
    events = [
        _ev("SessionStart", source="startup", timestamp="0"),
        _ev("PermissionRequest", tool_name="AskUserQuestion", timestamp="1"),
    ]
    got = infer_permission_decisions(events)
    assert len(got) == 1
    assert got[0]["source"] == "inferred_not_executed"
    assert got[0]["source"] not in {
        "config", "hook", "user_permanent", "user_temporary", "user_abort", "user_reject",
    }


def test_推断不使用官方source枚举():
    events = [
        _ev("PermissionRequest", tool_name="Bash", timestamp="1"),
    ]
    got = infer_permission_decisions(events)
    assert got[0]["source"].startswith("inferred_")


def test_无PermissionRequest则无推断项():
    events = [
        _ev("PreToolUse", tool_name="Read", tool_use_id="r1"),
        _ev("PostToolUse", tool_name="Read", tool_use_id="r1"),
    ]
    assert infer_permission_decisions(events) == []


def test_同名后续工具不被前一次未执行的请求误匹配():
    events = [
        _ev("PermissionRequest", tool_name="Bash", timestamp="1"),
        _ev("UserPromptSubmit", prompt="算了", timestamp="2"),
        _ev("PreToolUse", tool_name="Bash", tool_use_id="later", timestamp="3"),
        _ev("PostToolUse", tool_name="Bash", tool_use_id="later", timestamp="4"),
    ]
    got = infer_permission_decisions(events)
    assert len(got) == 1
    assert got[0]["decision"] == "reject"
    assert got[0]["source"] == "inferred_not_executed"


# ── 权限模式时间线 ────────────────────────────────────────


def test_首次出现的mode不算变更():
    events = [
        _ev("SessionStart", permission_mode="bypassPermissions"),
        _ev("PreToolUse", permission_mode="bypassPermissions"),
    ]
    assert infer_permission_mode_timeline(events) == []


def test_边沿检测记录from和to():
    events = [
        _ev("PostToolUse", permission_mode="bypassPermissions", timestamp="1"),
        _ev("PostToolUse", permission_mode="plan", timestamp="2"),
    ]
    got = infer_permission_mode_timeline(events)
    assert got == [{"timestamp": "2", "from": "bypassPermissions", "to": "plan"}]


def test_PermissionModeChanged优先且不编造trigger():
    events = [
        _ev("PreToolUse", permission_mode="default", timestamp="1"),
        _ev(
            "PermissionModeChanged",
            from_mode="default",
            to_mode="plan",
            permission_mode="plan",
            timestamp="2",
        ),
        _ev("PreToolUse", permission_mode="plan", timestamp="3"),
    ]
    got = infer_permission_mode_timeline(events)
    assert len(got) == 1
    assert got[0]["from"] == "default"
    assert got[0]["to"] == "plan"
    assert "trigger" not in got[0]


def test_trigger只有事件里真有才带上():
    events = [
        _ev(
            "PermissionModeChanged",
            from_mode="plan",
            to_mode="bypassPermissions",
            trigger="shift_tab",
            timestamp="1",
        ),
    ]
    got = infer_permission_mode_timeline(events)
    assert got[0]["trigger"] == "shift_tab"


def test_空事件返回空列表():
    assert infer_permission_decisions([]) == []
    assert infer_permission_mode_timeline([]) == []


# ── metadata 提升 + traj 序列化 ───────────────────────────


def test_apply_hook_events写入metadata():
    metadata = SessionMetadata(session_id="s")
    events = [
        _ev("PreToolUse", tool_name="Bash", tool_use_id="id-5", timestamp="1"),
        _ev("PermissionRequest", tool_name="Bash", timestamp="2"),
        _ev("PostToolUse", tool_name="Bash", tool_use_id="id-5",
            permission_mode="default", timestamp="3"),
        _ev("PostToolUse", tool_name="Read", tool_use_id="id-6",
            permission_mode="plan", timestamp="4"),
    ]
    apply_hook_events_to_metadata(metadata, events)
    assert metadata.permission_decisions[0]["decision"] == "accept"
    assert metadata.permission_mode_timeline[0]["from"] == "default"
    assert metadata.permission_mode_timeline[0]["to"] == "plan"


def test_build_trajectory把决策写进metadata而不进步骤():
    metadata = SessionMetadata(session_id="s")
    apply_hook_events_to_metadata(
        metadata,
        [
            _ev("PermissionRequest", tool_name="Bash", timestamp="1"),
            _ev("PostToolUse", tool_name="Bash", tool_use_id="id-7", timestamp="2"),
        ],
    )
    pair = SimpleNamespace(
        timestamp="2026-09-18T00:00:00",
        request_body={"messages": [{"role": "user", "content": "hi"}]},
        response_body={
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "id-7", "name": "Bash",
                 "input": {"command": "ls"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        new_messages=[],
        is_partial=False,
        stop_reason="tool_use",
    )
    traj = build_trajectory("s", [pair], metadata)
    assert traj["metadata"]["permission_decisions"][0]["source"] == "inferred_executed"
    assert "permission_mode_timeline" in traj["metadata"]
    for step in traj["trajectory"]:
        assert "decision" not in step
        assert "permission_decisions" not in step


# ── convert 三种 style 对旧夹具 hash 不变 ─────────────────


_OLD_TRAJ = {
    "trajectory": [
        {
            "message_type": "action",
            "thought": "list files",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "content": "list files",
        },
        {
            "message_type": "observation",
            "content": "a.py",
            "tool_use_id": "id-old",
            "is_error": False,
        },
    ],
    "metadata": {
        "session_id": "old-sid",
        "model": "claude-opus-5",
        "exit_status": "end_turn",
        "tools_used": ["Bash"],
        "total_steps": 2,
    },
}


def _convert_hash(traj: dict, style: str) -> str:
    record = convert_trajs.convert_traj(traj, style)
    blob = json.dumps(record["messages"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def test_convert三种style在加入决策字段后hash不变():
    enriched = json.loads(json.dumps(_OLD_TRAJ))
    enriched["metadata"]["permission_decisions"] = [{
        "tool_use_id": "id-old",
        "tool_name": "Bash",
        "decision": "accept",
        "source": "inferred_executed",
        "timestamp": "t",
    }]
    enriched["metadata"]["permission_mode_timeline"] = [{
        "timestamp": "t", "from": "default", "to": "plan",
    }]
    for style in ("xml", "tool", "messages"):
        assert _convert_hash(_OLD_TRAJ, style) == _convert_hash(enriched, style)


# ── collector 抄字段与边沿 ────────────────────────────────


def test_copy_decision_fields抄真实决策():
    event = {}
    collector.copy_decision_fields(
        event,
        {"decision": "reject", "source": "user_reject", "tool_name": "Bash"},
    )
    assert event["decision"] == "reject"
    assert event["decision_source"] == "user_reject"


def test_copy_decision_fields不把startup当成决策source():
    event = {}
    collector.copy_decision_fields(event, {"source": "startup"})
    assert "decision" not in event
    assert "decision_source" not in event


def test_permission_mode_change_event空trigger省略():
    changed = collector.permission_mode_change_event(
        "default", "plan", "t", "sid", "/tmp",
    )
    assert changed is not None
    assert changed["event"] == "PermissionModeChanged"
    assert changed["from"] == "default"
    assert changed["to"] == "plan"
    assert "trigger" not in changed
    assert collector.permission_mode_change_event("plan", "plan", "t", "sid", None) is None
    assert collector.permission_mode_change_event(None, "plan", "t", "sid", None) is None


def test_collector跨进程边沿写入Changed事件(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "EVENTS_DIR", tmp_path)

    def run(payload: dict) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        collector.main()

    sid = "sess-mode"
    run({
        "hook_event_name": "PreToolUse",
        "session_id": sid,
        "permission_mode": "default",
        "tool_name": "Bash",
        "tool_use_id": "a",
        "tool_input": {"command": "ls"},
    })
    run({
        "hook_event_name": "PreToolUse",
        "session_id": sid,
        "permission_mode": "plan",
        "tool_name": "Bash",
        "tool_use_id": "b",
        "tool_input": {"command": "pwd"},
    })
    events = [
        json.loads(line)
        for line in (tmp_path / f"{sid}.jsonl").read_text().splitlines()
        if line.strip()
    ]
    changed = [e for e in events if e["event"] == "PermissionModeChanged"]
    assert len(changed) == 1
    assert changed[0]["from"] == "default"
    assert changed[0]["to"] == "plan"
    assert "trigger" not in changed[0]


def test_collector_SessionEnd删除sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "EVENTS_DIR", tmp_path)
    sid = "sess-end"
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "permission_mode": "default",
            "tool_name": "Read",
            "tool_use_id": "r",
            "tool_input": {},
        })),
    )
    collector.main()
    assert (tmp_path / f".{sid}.perm_mode").exists()
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": sid,
            "permission_mode": "default",
            "source": "prompt_input_exit",
        })),
    )
    collector.main()
    assert not (tmp_path / f".{sid}.perm_mode").exists()
