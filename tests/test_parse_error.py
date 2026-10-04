"""S2-3 _parse_error 可见 + 有限修复

修复只允许补齐截断的 `}` / `]`；截在字符串、数字、半截字面量里一律不修，
保留 `_raw_partial`。data_quality 必须能看见两类计数。
"""

import json
from types import SimpleNamespace

import pytest

from builder import SessionMetadata, build_trajectory, repair_tool_input, repair_truncated_json
from proxy import reassemble_sse_response


@pytest.mark.parametrize("partial, expected", [
    ('{"file_path": "/a.py", "content": "x"', {"file_path": "/a.py", "content": "x"}),
    ('{"edits": [{"old": "a", "new": "b"}', {"edits": [{"old": "a", "new": "b"}]}),
    ('{"edits": [{"old": "a", "new": "b"}]', {"edits": [{"old": "a", "new": "b"}]}),
    ('{"a": true', {"a": True}),
    ('{"s": "含 \\" 转义 }"', {"s": '含 " 转义 }'}),
])
def test_补括号可修(partial, expected):
    assert repair_truncated_json(partial) == expected


@pytest.mark.parametrize("partial", [
    '{"content": "半截字符',     # 截在字符串里：补引号 = 伪造截短的值
    '{"n": 12',                  # 数字可能被截短
    '{"a": tr',                  # 半截字面量
    '{"a": "x",',                # 逗号之后
    '{"a":',                     # 冒号之后
    '{"a"',                      # 只有键
    '',
    '{"a": 1}}',                 # 括号不配对
    '["x"',                      # 修出来不是 dict
    '{"a": "x"}',                # 本来就完整，不该走修复
])
def test_不可修保持失败(partial):
    assert repair_truncated_json(partial) is None


WRITE_REQ = {"file_path", "content"}


def test_repair_tool_input_状态():
    assert repair_tool_input({"command": "ls"}) == ({"command": "ls"}, "")
    bad = {"_raw_partial": '{"content": "半', "_parse_error": True}
    assert repair_tool_input(bad, WRITE_REQ) == (bad, "unrepaired")
    ok = {"_raw_partial": '{"file_path": "/a", "content": "x"', "_parse_error": True}
    fixed, status = repair_tool_input(ok, WRITE_REQ)
    assert status == "repaired"
    assert fixed == {"file_path": "/a", "content": "x", "_parse_repaired": True}
    # 幂等：已修过的再进来仍计为 repaired
    assert repair_tool_input(fixed, WRITE_REQ) == (fixed, "repaired")


def test_语法可修但缺required键_不修():
    # 历史数据的主形态：Write 截在 content 里，补括号后只剩 file_path
    only_path = {"_raw_partial": '{"file_path": "/a"', "_parse_error": True}
    assert repair_tool_input(only_path, WRITE_REQ) == (only_path, "unrepaired")


def test_没有schema_不修():
    ok = {"_raw_partial": '{"file_path": "/a", "content": "x"', "_parse_error": True}
    assert repair_tool_input(ok, None) == (ok, "unrepaired")


def _sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def test_sse截断仍只打标记_原文落盘():
    # proxy 不改：raw.jsonl 里保留原始 _raw_partial，修复只在 builder 做
    raw = _sse(
        {"type": "message_start", "message": {"id": "m", "role": "assistant"}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t1", "name": "Write", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "/a.py"'}},
        {"type": "content_block_stop", "index": 0},
    )
    msg = reassemble_sse_response(raw)
    assert msg["content"][0]["input"] == {"_raw_partial": '{"file_path": "/a.py"', "_parse_error": True}
    assert msg["_complete"] is False


def _pair(ts, content):
    return SimpleNamespace(
        timestamp=ts,
        request_body={"messages": [{"role": "user", "content": "go"}], "system": "sys",
                      "tools": [
                          {"name": "Write", "input_schema": {"required": ["file_path", "content"]}},
                          {"name": "Bash", "input_schema": {"required": ["command"]}},
                      ]},
        response_body={"content": content, "stop_reason": "tool_use",
                       "usage": {"input_tokens": 1, "output_tokens": 1}},
        new_messages=[],
        is_partial=True,
        stop_reason="tool_use",
    )


def test_data_quality_计数与修复落到action():
    good = {"_raw_partial": '{"file_path": "/a.py", "content": "x"', "_parse_error": True}
    bad = {"_raw_partial": '{"file_path": "/b.py", "content": "半', "_parse_error": True}
    pairs = [_pair("t0", [
        {"type": "tool_use", "id": "t1", "name": "Write", "input": good},
        {"type": "tool_use", "id": "t2", "name": "Write", "input": bad},
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "ls"}},
    ])]
    meta = SessionMetadata(session_id="s")
    traj = build_trajectory("s", pairs, meta)
    dq = traj["info"]["data_quality"]
    assert dq["parse_error_actions"] == 2
    assert dq["parse_error_repaired"] == 1

    actions = {s["tool_use_id"]: s for s in traj["trajectory"] if s.get("message_type") == "action"}
    assert actions["t1"]["tool_input"] == {"file_path": "/a.py", "content": "x", "_parse_repaired": True}
    # 修好的路径要进 files_edited；修不了的拿不到路径
    assert meta.files_edited == ["/a.py"]
    assert actions["t2"]["tool_input"] == bad
    # 不改调用方传进来的原始 pair（raw 数据不能被 builder 改写）
    assert pairs[0].response_body["content"][0]["input"] is good
    assert "_raw_partial" in good


def test_无截断时计数为零():
    pairs = [_pair("t0", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}])]
    dq = build_trajectory("s", pairs, SessionMetadata(session_id="s"))["info"]["data_quality"]
    assert dq["parse_error_actions"] == 0
    assert dq["parse_error_repaired"] == 0
