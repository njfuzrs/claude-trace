"""L1-E：StopFailure / PostToolUseFailure / InstructionsLoaded 提升进 metadata

只进 metadata，不改 exit_status、不改 Action/Observation 正文（convert 输出不变）。
"""

import hashlib
from types import SimpleNamespace

import collector
from builder import SessionMetadata, apply_hook_events_to_metadata, build_trajectory


def _pair(ts, content, stop="tool_use", messages=None):
    return SimpleNamespace(
        timestamp=ts,
        request_body={"messages": messages or [{"role": "user", "content": "go"}], "system": "sys"},
        response_body={"content": content, "stop_reason": stop,
                       "usage": {"input_tokens": 1, "output_tokens": 1}},
        new_messages=[], is_partial=False, stop_reason=stop,
    )


EVENTS = [
    {"event": "InstructionsLoaded", "timestamp": "t0", "file_path": "/p/CLAUDE.md",
     "memory_type": "Project", "load_reason": "session_start",
     "content_sha256": "ab", "content_bytes": 10},
    # 0.4.2 及以前的错字段事件：没有 file_path，必须跳过
    {"event": "InstructionsLoaded", "timestamp": "t0", "source": None, "content_length": 0},
    {"event": "PostToolUseFailure", "timestamp": "t1", "tool_use_id": "t1",
     "tool_name": "Bash", "error": "x" * 1000},
    {"event": "PostToolUseFailure", "timestamp": "t1", "tool_name": "Bash", "error": "无 id 跳过"},
    {"event": "StopFailure", "timestamp": "t2", "error": "invalid_request"},
    {"event": "StopFailure", "timestamp": "t3"},
]


def test_提升三类事件():
    meta = SessionMetadata(session_id="s")
    apply_hook_events_to_metadata(meta, EVENTS)
    assert meta.instructions_loaded == [{
        "file_path": "/p/CLAUDE.md", "memory_type": "Project", "load_reason": "session_start",
        "content_sha256": "ab", "content_bytes": 10,
    }]
    assert len(meta.tool_failures) == 1
    assert meta.tool_failures[0]["tool_use_id"] == "t1"
    assert len(meta.tool_failures[0]["error_preview"]) == 300
    assert meta.stop_failures == [{"timestamp": "t2", "error": "invalid_request"},
                                  {"timestamp": "t3", "error": "unknown"}]


def test_写入traj且不改exit_status():
    tool = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "false"}}
    # tool_result 没标 is_error，但 hook 说失败了 → hook_failure_not_error 计 1
    result_msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [tool]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom"}]},
    ]
    pairs = [_pair("t0", [tool]), _pair("t1", [{"type": "text", "text": "ok"}],
                                        stop="tool_use", messages=result_msgs)]
    meta = SessionMetadata(session_id="s")
    apply_hook_events_to_metadata(meta, EVENTS)
    traj = build_trajectory("s", pairs, meta)
    m = traj["metadata"]
    assert m["exit_status"] == "tool_use"
    assert [f["error"] for f in m["stop_failures"]] == ["invalid_request", "unknown"]
    assert m["tool_failures"][0]["tool_use_id"] == "t1"
    assert m["instructions_loaded"][0]["file_path"] == "/p/CLAUDE.md"
    assert traj["info"]["data_quality"]["hook_failure_not_error"] == 1
    # 正文不带新字段
    assert all("tool_failures" not in s and "stop_failures" not in s for s in traj["trajectory"])


def test_没有hook事件时为空():
    meta = SessionMetadata(session_id="s")
    traj = build_trajectory("s", [_pair("t0", [{"type": "text", "text": "hi"}], stop="end_turn")], meta)
    m = traj["metadata"]
    assert (m["stop_failures"], m["tool_failures"], m["instructions_loaded"]) == ([], [], [])
    assert traj["info"]["data_quality"]["hook_failure_not_error"] == 0


def test_collector指令文件只记hash不记正文(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("私有指令")
    d = collector.instruction_file_digest(str(f))
    assert d == {"content_bytes": len("私有指令".encode()),
                 "content_sha256": hashlib.sha256("私有指令".encode()).hexdigest()}
    assert collector.instruction_file_digest(str(tmp_path / "missing.md")) == {}
    assert collector.instruction_file_digest(None) == {}


def test_collector超大指令文件不读(tmp_path, monkeypatch):
    f = tmp_path / "big.md"
    f.write_text("abcd")
    monkeypatch.setattr(collector, "_INSTRUCTION_HASH_MAX_BYTES", 2)
    assert collector.instruction_file_digest(str(f)) == {"content_bytes": 4}
