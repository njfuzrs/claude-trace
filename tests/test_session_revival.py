#!/usr/bin/env python3
"""会话复活（超时清理后用户又提问）的端到端回归测试

对应 P0-A 的源头：--session-timeout 默认 300s，用户思考超过 5 分钟后
cleanup_expired 就把会话清掉；再提问时代理新建一个 pairs 为空的 Session。
修复前 index 从 1 重来 + save_trajectory 覆盖写 → 长轨迹被短轨迹冲掉。
"""

import json
import tempfile
from pathlib import Path

import pytest

import proxy as P


def _req(n_msgs: int, model: str = "claude-opus-4-6") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": f"msg-{i}"} for i in range(n_msgs)],
        "system": [{"type": "text", "text": "sys"}],
        "tools": [{"name": "Read"}],
    }


def _resp(text: str = "done") -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "_complete": True,
        "model": "claude-opus-4-6",
    }


@pytest.fixture
def collector():
    return P.DataCollector(Path(tempfile.mkdtemp()))


@pytest.mark.asyncio
async def test_revival_continues_index(collector):
    """★核心：复活后 index 必须续排，不能从 1 重来"""
    sid = "sess-revive"
    s1 = P.Session(id=sid, model="claude-opus-4-6")
    for _ in range(3):
        pair = collector.record_request(s1, _req(2), {})
        await collector.record_response_async(s1, pair, _resp())
    assert [p.index for p in s1.pairs] == [1, 2, 3]

    # 模拟超时清理：Session 对象被丢弃，目录留在盘上
    s2 = P.Session(id=sid, model="claude-opus-4-6")
    pair = collector.record_request(s2, _req(8), {})
    assert s2.revived is True
    assert s2.prior_pair_count == 3
    assert pair.index == 4, "复活后 index 必须从 4 续排"
    # 复活首个请求没有增量基线，必须打上 replay 标记供 builder 去重
    assert pair.is_full_replay is True

    # raw.jsonl 里不能出现重复 index
    await collector.record_response_async(s2, pair, _resp())
    raw = (collector.sessions_dir / sid / "raw.jsonl").read_text().splitlines()
    idxs = [json.loads(line)["index"] for line in raw if line.strip()]
    assert idxs == [1, 2, 3, 4], idxs


@pytest.mark.asyncio
async def test_revival_final_export_recovers_full_traj(collector):
    """★核心：复活会话的最终导出必须从 raw.jsonl 拿回完整轨迹"""
    sid = "sess-full"
    s1 = P.Session(id=sid, model="claude-opus-4-6")
    for _ in range(6):
        pair = collector.record_request(s1, _req(2), {})
        await collector.record_response_async(s1, pair, _resp())

    traj_path = collector.sessions_dir / sid / "session.traj"
    before = len(json.loads(traj_path.read_text())["trajectory"])
    assert before >= 6

    # 复活后只提一个问题就结束会话
    s2 = P.Session(id=sid, model="claude-opus-4-6")
    pair = collector.record_request(s2, _req(14), {})
    await collector.record_response_async(s2, pair, _resp())
    await collector.export_session_async(s2, is_final=True)

    after = len(json.loads(traj_path.read_text())["trajectory"])
    # 修复前这里会掉到 1 步（只剩复活后那一段）
    assert after >= before, f"复活后最终导出把轨迹从 {before} 步截断到 {after} 步"


@pytest.mark.asyncio
async def test_incremental_write_does_not_shrink(collector):
    """复活后的每轮增量写也不能把盘上的长轨迹冲短"""
    sid = "sess-inc"
    s1 = P.Session(id=sid, model="claude-opus-4-6")
    for _ in range(5):
        pair = collector.record_request(s1, _req(2), {})
        await collector.record_response_async(s1, pair, _resp())
    traj_path = collector.sessions_dir / sid / "session.traj"
    before = len(json.loads(traj_path.read_text())["trajectory"])

    s2 = P.Session(id=sid, model="claude-opus-4-6")
    pair = collector.record_request(s2, _req(12), {})
    await collector.record_response_async(s2, pair, _resp())
    after = len(json.loads(traj_path.read_text())["trajectory"])
    assert after >= before, f"增量写把轨迹从 {before} 步冲到 {after} 步"


@pytest.mark.asyncio
async def test_fresh_session_not_marked_revived(collector):
    """全新会话不能被误判为复活（否则 index 起点就错了）"""
    s = P.Session(id="sess-fresh", model="claude-opus-4-6")
    pair = collector.record_request(s, _req(1), {})
    assert s.revived is False
    assert s.prior_pair_count == 0
    assert pair.index == 1
    assert pair.is_full_replay is False


@pytest.mark.asyncio
async def test_subagent_not_revived(collector):
    """子会话不参与复活检测：它们的 pair 从不单独落盘"""
    s = P.Session(id="sess-sub", model="claude-haiku-4-5")
    s.is_subagent = True
    (collector.sessions_dir / "sess-sub").mkdir(parents=True, exist_ok=True)
    (collector.sessions_dir / "sess-sub" / "raw.jsonl").write_text('{"index":1}\n')
    pair = collector.record_request(s, _req(2), {})
    assert s.revived is False
    assert pair.index == 1


@pytest.mark.asyncio
async def test_revival_keeps_original_start_time(collector):
    """复活会话的 start_time 必须是复活前首轮的时间，不是复活时刻"""
    sid = "sess-start"
    s1 = P.Session(id=sid, model="claude-opus-4-6")
    pair = collector.record_request(s1, _req(2), {})
    await collector.record_response_async(s1, pair, _resp())
    first_ts = pair.timestamp

    s2 = P.Session(id=sid, model="claude-opus-4-6", start_time="2099-01-01T00:00:00")
    collector.record_request(s2, _req(4), {})
    assert s2.start_time == first_ts


@pytest.mark.asyncio
async def test_revival_invalidates_upload_marker(collector):
    """复活后旧 .uploaded 必须作废，否则启动补传会跳过复活后的新增轮次"""
    sid = "sess-marker"
    s1 = P.Session(id=sid, model="claude-opus-4-6")
    pair = collector.record_request(s1, _req(2), {})
    await collector.record_response_async(s1, pair, _resp())
    d = collector.sessions_dir / sid
    (d / ".uploaded").write_text('{"files": {}}')

    s2 = P.Session(id=sid, model="claude-opus-4-6")
    collector.record_request(s2, _req(4), {})
    assert not (d / ".uploaded").exists()
    assert (d / ".uploaded.prev").exists(), "旧标记应改名保留以便对账"


@pytest.mark.asyncio
async def test_revival_turn_export_includes_new_rounds(collector):
    """★复活后 turn 结束（stop）的导出必须带上复活后的新轮次，不能停在旧版本"""
    sid = "sess-turn"
    s1 = P.Session(id=sid, model="claude-opus-4-6")
    for _ in range(4):
        pair = collector.record_request(s1, _req(2), {})
        await collector.record_response_async(s1, pair, _resp())
    traj_path = collector.sessions_dir / sid / "session.traj"
    before = len(json.loads(traj_path.read_text())["trajectory"])

    s2 = P.Session(id=sid, model="claude-opus-4-6")
    for n in (6, 8):
        pair = collector.record_request(s2, _req(n), {})
        await collector.record_response_async(s2, pair, _resp())
    # 增量写跳过复活会话，traj 仍是旧版（不被短轨迹冲掉）
    assert len(json.loads(traj_path.read_text())["trajectory"]) == before

    await collector.export_session_async(s2, is_final=False)
    traj = json.loads(traj_path.read_text())
    assert len(traj["trajectory"]) > before, "turn 导出应从 raw.jsonl 重建出包含新轮次的完整轨迹"
    assert traj["metadata"]["total_api_calls"] == 6
