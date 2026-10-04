"""tools/thinking_coverage.py 口径测试

分母、空正文、上游指纹任何一项错了，覆盖率就会误导 thinking-SFT 选数，所以锁死。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import thinking_coverage as tc  # noqa: E402

ANTHROPIC_ID = "msg_01Sx5WiGHymxSNe3QmTuKMQd"
GW_TS_ID = "msg_20261004100734"

THINK = {"type": "adaptive"}
TOOL = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}


def _rec(content, msg_id=GW_TS_ID, thinking=THINK, ts="2026-10-01T10:00:00", **req):
    request = {"model": "m", "max_tokens": 1, **req}
    if thinking is not None:
        request["thinking"] = thinking
    return {"timestamp": ts, "model": "m", "request": request,
            "response": {"id": msg_id, "content": content}}


def _mk(root, name, records):
    d = root / name
    d.mkdir()
    (d / "raw.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


def test_上游指纹():
    assert tc.classify_upstream(ANTHROPIC_ID) == "anthropic"
    assert tc.classify_upstream("msg_bdrk_01HwqUAizPGNeUiafQRCoKLx") == "bedrock"
    assert tc.classify_upstream(GW_TS_ID) == "gw-timestamp"
    assert tc.classify_upstream("msg_nCd0fThKbbzSK0pzqxXCoGBH") == "gw-random"
    assert tc.classify_upstream("") == "unknown"


def test_分类口径(tmp_path):
    # covered：有正文的 thinking
    _mk(tmp_path, "a", [_rec([{"type": "thinking", "thinking": "想一想"}, TOOL])])
    # covered：redacted_thinking 也算（正文在签名里，可回放）
    _mk(tmp_path, "b", [_rec([{"type": "redacted_thinking", "data": "x"}, TOOL])])
    # omitted：有块但正文为空，不算 CoT
    _mk(tmp_path, "c", [_rec([{"type": "thinking", "thinking": ""}, TOOL])])
    # missing：开了 thinking、有 tool_use、没有任何 thinking 块（网关剥掉）
    _mk(tmp_path, "d", [_rec([TOOL])])
    # not_requested：thinking disabled，不进分母
    _mk(tmp_path, "e", [_rec([TOOL], thinking={"type": "disabled"})])
    # no_tool_use：纯对话，不进分母
    _mk(tmp_path, "f", [_rec([{"type": "text", "text": "hi"}])])
    # 跳过：count_tokens 探测（无 max_tokens）
    (tmp_path / "probe").mkdir()
    (tmp_path / "probe" / "raw.jsonl").write_text(json.dumps({"request": {"model": "m"}}) + "\n")
    # 跳过：只有 error 响应
    _mk(tmp_path, "g", [{"timestamp": "2026-10-01", "request": {"max_tokens": 1},
                         "response": {"error": {"type": "overloaded"}}}])

    rows = tc.collect(tmp_path)
    assert sorted(r["session"] for r in rows) == list("abcdef")
    [g] = tc.summarize(rows, ["upstream"], streak=99)
    assert g["group"] == {"upstream": "gw-timestamp"}
    assert (g["covered"], g["omitted"], g["missing"]) == (2, 1, 1)
    assert (g["not_requested"], g["no_tool_use"]) == (1, 1)
    assert g["eligible"] == 4
    assert g["coverage"] == 0.5


def test_增量行的thinking配置与跨行响应(tmp_path):
    # 首行没开 thinking、也没有 tool_use；第二行（增量行）开了，且 thinking 在第二个响应
    first = _rec([{"type": "text", "text": "ok"}], thinking=None)
    second = _rec([{"type": "thinking", "thinking": "嗯"}, TOOL])
    second["request"].pop("max_tokens")  # 增量行字段不全也要能读
    _mk(tmp_path, "s", [first, second])
    [row] = tc.collect(tmp_path)
    assert tc._status(row) == "covered"


def test_坏行与截断尾行不崩(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "raw.jsonl").write_text(json.dumps(_rec([TOOL])) + "\n{\"timestamp\": \"2026-1")
    [row] = tc.collect(tmp_path)
    assert tc._status(row) == "missing"


def test_尾部连续无CoT告警(tmp_path):
    # 早期有 CoT，之后连续 3 个缺失 → streak=3 告警；中间夹的 not_requested 不打断计数
    _mk(tmp_path, "s0", [_rec([{"type": "thinking", "thinking": "x"}, TOOL], ts="2026-09-01")])
    _mk(tmp_path, "s1", [_rec([TOOL], ts="2026-09-02")])
    _mk(tmp_path, "s2", [_rec([TOOL], thinking={"type": "disabled"}, ts="2026-09-03")])
    _mk(tmp_path, "s3", [_rec([TOOL], ts="2026-09-04")])
    _mk(tmp_path, "s4", [_rec([{"type": "thinking", "thinking": ""}, TOOL], ts="2026-09-05")])
    [g] = tc.summarize(tc.collect(tmp_path), ["upstream"], streak=3)
    assert g["tail_no_cot"] == 3
    assert g["alert"] is True
    [g] = tc.summarize(tc.collect(tmp_path), ["upstream"], streak=4)
    assert g["alert"] is False


def test_since与按模型月份分组(tmp_path):
    _mk(tmp_path, "old", [_rec([TOOL], msg_id=ANTHROPIC_ID, ts="2026-03-01T00:00:00")])
    _mk(tmp_path, "new", [_rec([TOOL], ts="2026-10-01T00:00:00")])
    rows = tc.collect(tmp_path, since="2026-09-01")
    assert [r["session"] for r in rows] == ["new"]
    groups = tc.summarize(tc.collect(tmp_path), ["upstream", "model", "month"])
    assert [g["group"] for g in groups] == [
        {"upstream": "anthropic", "model": "m", "month": "2026-03"},
        {"upstream": "gw-timestamp", "model": "m", "month": "2026-10"},
    ]
