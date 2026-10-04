#!/usr/bin/env python3
"""transcript 补洞（S4）回归测试

夹具 tests/fixtures/transcript_sample.jsonl 是按真实 transcript 形状手写的脱敏切片：
4 轮主会话 + 拆分记录 + 侧链 + <synthetic> 占位 + 坏行 + 未知 type。
"""

import copy
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

import proxy as P
import transcript as T
from builder import SessionMetadata, build_trajectory
from merger import _AdaptedPair

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
SID = "00000000-0000-4000-8000-000000000001"
TOOLS = [{"name": n, "input_schema": {"required": []}} for n in ("Bash", "Read", "Edit")]
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0, "output_tokens_details": {"thinking_tokens": 0}}

# 与夹具一一对应的 raw 轮次：(时间戳, 本轮新增 user 消息, 响应 id, 响应 content, stop_reason)
_ROUNDS = [
    ("2026-10-01T00:00:02.000Z", [{"role": "user", "content": "请列出目录"}], "msg_t1",
     [{"type": "thinking", "thinking": "先看目录", "signature": "sig-raw"},
      {"type": "tool_use", "id": "toolu_1", "name": "Bash",
       "input": {"command": "cd /tmp/fake-repo && ls"}}], "tool_use"),
    ("2026-10-01T00:00:04.000Z",
     [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                    "content": "a.py\nb.py", "is_error": False}]}], "msg_t2",
     [{"type": "tool_use", "id": "toolu_2", "name": "Read",
       "input": {"file_path": "/tmp/fake-repo/a.py"}}], "tool_use"),
    ("2026-10-01T00:00:06.000Z",
     [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2",
                                    "content": "print('hi')"}]}], "msg_t3",
     [{"type": "tool_use", "id": "toolu_3", "name": "Edit",
       "input": {"file_path": "/tmp/fake-repo/a.py", "old_string": "hi", "new_string": "hello"}}],
     "tool_use"),
    ("2026-10-01T00:00:09.000Z",
     [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_3", "content": "ok"}]},
      {"role": "user", "content": "继续"}], "msg_t4",
     [{"type": "text", "text": "已改完"}], "end_turn"),
]


def _raw_pairs(n=4):
    pairs = []
    for i, (ts, users, mid, content, stop) in enumerate(_ROUNDS[:n]):
        req = {"model": "claude-opus-5-5", "tools": TOOLS, "system": "sys"}
        if i == 0:
            req["messages"] = copy.deepcopy(users)
            new = []
        else:
            new = copy.deepcopy(users)
        resp = {"id": mid, "model": "claude-opus-5-5", "role": "assistant", "type": "message",
                "content": copy.deepcopy(content), "stop_reason": stop, "usage": dict(USAGE),
                "_complete": True}
        pairs.append(_AdaptedPair(
            timestamp=ts, request_body=req, response_body=resp, usage=dict(USAGE),
            stop_reason=stop, is_partial=False, model="claude-opus-5-5",
            index=i + 1, new_messages=new,
        ))
    return pairs


def _build(pairs):
    return build_trajectory(SID, pairs, SessionMetadata(session_id=SID, model="claude-opus-5-5"))


def _shape(traj):
    """step 正文对比口径：类型 / 动作 / tool_use_id / 正文，不含时间戳与来源标记"""
    return [
        (s.get("message_type"), s.get("action"), s.get("tool_use_id"), s.get("content"))
        for s in traj["trajectory"]
    ]


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ─────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────

def test_解析未知type和坏行不崩并计数():
    t = T.parse_transcript(FIXTURE)
    assert t.session_id == SID
    assert [m.message_id for m in t.assistants] == ["msg_t1", "msg_t2", "msg_t3", "msg_t4"]
    assert t.unknown_types == {"future-unknown-type": 1}
    assert t.bad_lines == 1
    assert t.sidechain_skipped == 1
    assert t.synthetic_skipped == 1
    # atis-latch 等已知但不消费的 type 不算 unknown
    assert t.type_counts.get("atis-latch") == 1


def test_同一messageid的拆分记录合并回一条():
    t = T.parse_transcript(FIXTURE)
    first = t.assistants[0]
    assert [b["type"] for b in first.content] == ["thinking", "tool_use"]
    assert first.stop_reason == "tool_use"
    assert first.tool_use_ids() == ["toolu_1"]
    # message.content 里的 input 被 Claude Code 改写过，wireToolInputs 才是发给上游的原样
    assert first.content[1]["input"] == {"command": "cd /tmp/fake-repo && ls"}
    # 第 4 轮之前是 tool_result + 用户的「继续」
    assert [u["content"] if isinstance(u["content"], str) else "blocks"
            for u in t.assistants[3].preceding_users] == ["blocks", "继续"]


def test_权限模式时间线来自user记录边沿():
    t = T.parse_transcript(FIXTURE)
    assert t.permission_mode_timeline == [{
        "timestamp": "2026-10-01T00:00:08.000Z", "from": "default",
        "to": "acceptEdits", "source": "transcript",
    }]


# ─────────────────────────────────────────────
# 补洞
# ─────────────────────────────────────────────

def test_完整会话补洞幂等_traj不变():
    pairs = _raw_pairs()
    before = _build(pairs)
    filled, report = T.plan_fill(pairs, T.parse_transcript(FIXTURE))
    assert report is None
    assert len(filled) == len(pairs)
    assert _hash(_build(filled)) == _hash(before)


def test_丢掉最后两轮后补回且step正文一致():
    full = _build(_raw_pairs())
    t = T.parse_transcript(FIXTURE)
    filled, report = T.plan_fill(_raw_pairs(2), t)
    assert report["filled_pairs"] == 2
    assert report["filled_message_ids"] == ["msg_t3", "msg_t4"]
    assert report["missing_middle"] == 0
    assert [p.index for p in filled] == [1, 2, 3, 4]
    traj = _build(filled)
    assert len(traj["trajectory"]) == len(full["trajectory"])
    assert _shape(traj) == _shape(full)
    assert traj["info"]["data_quality"]["orphan_observations"] == 0
    # 补出来的 step 带来源标记，原有的不带
    sources = {s.get("tool_use_id"): s.get("_source") for s in traj["trajectory"]
               if s.get("message_type") == "action"}
    assert sources["toolu_1"] is None and sources["toolu_3"] == "transcript"


def test_raw优先_已有轮次不被transcript覆盖():
    pairs = _raw_pairs(2)
    filled, _ = T.plan_fill(pairs, T.parse_transcript(FIXTURE))
    # raw 里 thinking 有 signature，transcript 里为空；raw 那一轮必须原样保留
    assert filled[0] is pairs[0]
    assert filled[0].response_body["content"][0]["signature"] == "sig-raw"


def test_partial轮次用transcript补全():
    pairs = _raw_pairs()
    last = pairs[-1]
    last.is_partial = True
    last.stop_reason = ""
    last.response_body = {"id": "msg_t4", "content": [{"type": "text", "text": "已改"}],
                          "_complete": False}
    filled, report = T.plan_fill(pairs, T.parse_transcript(FIXTURE))
    assert report["completed_partial"] == 1 and report["filled_pairs"] == 0
    assert filled[-1] is not last and last.is_partial  # 不改调用方的对象
    assert filled[-1].is_partial is False
    assert filled[-1].response_body["content"] == [{"type": "text", "text": "已改完"}]


def test_中间缺口只计数不插入():
    pairs = [p for p in _raw_pairs() if p.response_body["id"] != "msg_t2"]
    filled, report = T.plan_fill(pairs, T.parse_transcript(FIXTURE))
    assert report is None
    assert len(filled) == 3


def test_无锚点默认不补_抢救模式才整段导入():
    t = T.parse_transcript(FIXTURE)
    assert T.plan_fill([], t)[1] is None
    filled, report = T.plan_fill([], t, allow_anchorless=True)
    assert report["filled_pairs"] == 4
    traj = _build(filled)
    assert [s.get("tool_use_id") for s in traj["trajectory"] if s.get("message_type") == "action"
            ][:3] == ["toolu_1", "toolu_2", "toolu_3"]
    assert report["thinking_without_signature"] == 1


def test_子会话已有的消息id不补进主轨迹():
    _, report = T.plan_fill(_raw_pairs(3), T.parse_transcript(FIXTURE), extra_known_ids=["msg_t4"])
    assert report is None


def test_tool_use_id作为兜底对齐键():
    pairs = _raw_pairs(2)
    for p in pairs:
        p.response_body = {**p.response_body, "id": "msg_other_" + p.response_body["id"]}
    _, report = T.plan_fill(pairs, T.parse_transcript(FIXTURE))
    # msg_t1 只有 tool_use 能对上；msg_t2 同理 → 锚点在第 2 轮
    assert report["filled_message_ids"] == ["msg_t3", "msg_t4"]


def test_补洞失败只打日志返回原pairs(tmp_path, monkeypatch):
    pairs = _raw_pairs(2)
    assert T.fill_pairs_from_transcript(SID, pairs, transcript_path=tmp_path / "nope.jsonl")[1] is None

    def boom(_):
        raise RuntimeError("坏了")
    monkeypatch.setattr(T, "parse_transcript", boom)
    out, report, timeline = T.fill_pairs_from_transcript(SID, pairs, transcript_path=FIXTURE)
    assert out == pairs and report is None and timeline == []


def test_定位transcript(tmp_path):
    assert T.find_transcript(SID, [{"event": "Stop", "transcript_path": str(FIXTURE)}]) == FIXTURE
    proj = tmp_path / "projects" / "-tmp-fake-repo"
    proj.mkdir(parents=True)
    target = proj / f"{SID}.jsonl"
    target.write_text("")
    assert T.find_transcript(SID, [], claude_dir=tmp_path) == target
    # transcript_path 失效时回退到按 sid 搜
    assert T.find_transcript(SID, [{"transcript_path": "/nope.jsonl"}], claude_dir=tmp_path) == target
    assert T.find_transcript("../evil", [], claude_dir=tmp_path) is None


# ─────────────────────────────────────────────
# 代理最终导出
# ─────────────────────────────────────────────

def _collector_with_events(tmp_path, events):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    (events_dir / f"{SID}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
    return P.DataCollector(tmp_path / "out", events_dir=events_dir)


def test_代理最终导出补洞并写报告(tmp_path):
    c = _collector_with_events(tmp_path, [{"event": "Stop", "transcript_path": str(FIXTURE)}])
    session = P.Session(id=SID, model="claude-opus-5-5")
    _, traj = c._build_traj_data(session, _raw_pairs(2), None, fill_from_transcript=True)
    assert traj["metadata"]["total_steps"] == len(_build(_raw_pairs())["trajectory"])
    assert traj["metadata"]["transcript_fill"]["filled_pairs"] == 2
    # hook 时间线为空时用 transcript 的
    assert traj["metadata"]["permission_mode_timeline"][0]["to"] == "acceptEdits"


def test_代理完整会话开着补洞_traj与关闭时一致(tmp_path):
    c = _collector_with_events(tmp_path, [{"event": "Stop", "transcript_path": str(FIXTURE)}])
    session = P.Session(id=SID, model="claude-opus-5-5")
    _, on = c._build_traj_data(session, _raw_pairs(), None, fill_from_transcript=True)
    _, off = c._build_traj_data(session, _raw_pairs(), None, fill_from_transcript=False)
    assert "transcript_fill" not in on["metadata"]
    # 唯一允许的差异：hook 没有时间线时由 transcript 补上
    on["metadata"].pop("permission_mode_timeline")
    off["metadata"].pop("permission_mode_timeline")
    for t in (on, off):
        t["metadata"].pop("end_time", None)
    assert _hash(on) == _hash(off)


def test_环境变量可关闭补洞(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJ_TRANSCRIPT_FILL", "false")
    c = _collector_with_events(tmp_path, [{"event": "Stop", "transcript_path": str(FIXTURE)}])
    _, traj = c._build_traj_data(P.Session(id=SID), _raw_pairs(2), None, fill_from_transcript=True)
    assert "transcript_fill" not in traj["metadata"]


def test_hook已有时间线时不被transcript覆盖(tmp_path):
    c = _collector_with_events(tmp_path, [
        {"event": "UserPromptSubmit", "permission_mode": "default", "timestamp": "t1"},
        {"event": "UserPromptSubmit", "permission_mode": "plan", "timestamp": "t2"},
        {"event": "Stop", "transcript_path": str(FIXTURE)},
    ])
    _, traj = c._build_traj_data(P.Session(id=SID), _raw_pairs(2), None, fill_from_transcript=True)
    assert traj["metadata"]["permission_mode_timeline"] == [
        {"timestamp": "t2", "from": "default", "to": "plan"}]


@pytest.mark.asyncio
async def test_最终导出异常不影响落盘(tmp_path, monkeypatch):
    c = _collector_with_events(tmp_path, [{"event": "Stop", "transcript_path": str(FIXTURE)}])

    def boom(*_a, **_k):
        raise RuntimeError("坏了")
    monkeypatch.setattr(T, "fill_pairs_from_transcript", boom)
    session = P.Session(id=SID, model="claude-opus-5-5")
    session.pairs = _raw_pairs(2)
    await c.export_session_async(session, is_final=True)
    traj = json.loads((c.sessions_dir / SID / "session.traj").read_text())
    assert traj["metadata"]["total_steps"] == len(_build(_raw_pairs(2))["trajectory"])


# ─────────────────────────────────────────────
# 离线 CLI
# ─────────────────────────────────────────────

def test_rebuild_from_transcript抢救无raw会话(tmp_path):
    from tools.rebuild_trajs import rebuild_one

    session_dir = tmp_path / "sessions" / SID
    session_dir.mkdir(parents=True)
    (session_dir / "events.jsonl").write_text(
        json.dumps({"event": "Stop", "transcript_path": str(FIXTURE)}) + "\n")
    assert rebuild_one(session_dir, backup=False) is None
    result = rebuild_one(session_dir, backup=False, from_transcript=True,
                         claude_dir=Path(tempfile.mkdtemp()))
    assert result is not None and result["transcript_filled"] == 4
    traj = json.loads((session_dir / "session.traj").read_text())
    assert traj["metadata"]["transcript_fill"]["anchorless_skipped"] == 0
    assert traj["metadata"]["total_steps"] > 0
