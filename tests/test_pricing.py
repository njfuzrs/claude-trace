"""builder 成本估算测试

旧实现 cache_creation 按 0.25× 计（应为 1.25×，少算 5 倍），cache_read 写死 0.1×
（Fable 5.1 应为 0.025×、Opus 5.5 应为 0.05×），且没有任何断言能拦下。这里锁死。
价目来源：https://platform.claude.com/docs/en/about-claude/pricing（2026-10-04）
"""

import math
from types import SimpleNamespace

import builder

M = 1_000_000


def _close(a, b):
    return math.isclose(a, b, rel_tol=1e-9)


def test_价目表关键模型():
    cases = {
        "claude-opus-5-5": (4.0, 20.0, 0.20),
        "claude-opus-5": (5.0, 25.0, 0.50),
        "claude-opus-4-8": (5.0, 25.0, 0.50),
        "claude-sonnet-5-5": (2.0, 10.0, 0.20),
        "claude-sonnet-5": (2.0, 10.0, 0.20),
        "claude-sonnet-4-6": (3.0, 15.0, 0.30),
        "claude-fable-5-1": (10.0, 50.0, 0.25),
        "claude-fable-5": (10.0, 50.0, 1.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10),
        "claude-opus-4-1-20250805": (15.0, 75.0, 1.50),
    }
    for model, (inp, out, read) in cases.items():
        p = builder._lookup_pricing(model)
        assert (p["input"], p["output"], p["cache_read"]) == (inp, out, read), model


def test_前缀不串台():
    # opus-5-5 不能落到 opus-5，fable-5-1 不能落到 fable-5
    assert builder._lookup_pricing("claude-opus-5-5")["input"] == 4.0
    assert builder._lookup_pricing("claude-fable-5-1")["cache_read"] == 0.25
    assert builder._lookup_pricing("deepseek-v3") is None


def test_cache_read不按普通input计():
    # 1M cache_read 在 Opus 5.5 上是 $0.20，不是 $4
    cost = builder._estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=M)
    assert _close(cost, 0.20)
    assert not _close(cost, builder._estimate_cost("claude-opus-5-5", M, 0))
    assert _close(builder._estimate_cost("claude-fable-5-1", 0, 0, cache_read_tokens=M), 0.25)
    assert _close(builder._estimate_cost("claude-opus-5", 0, 0, cache_read_tokens=M), 0.50)


def test_cache_write按1点25倍和1h档2倍():
    assert _close(builder._estimate_cost("claude-opus-5", 0, 0, cache_creation_tokens=M), 6.25)
    # 1M 写入里 40 万是 1h 档：0.6M×6.25 + 0.4M×10
    cost = builder._estimate_cost("claude-opus-5", 0, 0, cache_creation_tokens=M,
                                  cache_creation_1h_tokens=400_000)
    assert _close(cost, 0.6 * 6.25 + 0.4 * 10)


def test_组合用量():
    cost = builder._estimate_cost("claude-sonnet-5", M, M, M, M)
    assert _close(cost, 2 + 10 + 0.2 + 2.5)


def test_长上下文只对老模型加价():
    base = builder._estimate_cost("claude-opus-4-8", M, 0)
    assert _close(builder._estimate_cost("claude-opus-4-8[1m]", M, 0), base)
    assert _close(builder._estimate_cost("claude-opus-5-5[1m]", M, 0), 4.0)
    assert _close(builder._estimate_cost("claude-sonnet-4-5-20250929[1m]", M, 0), 6.0)


def test_build读取1h缓存档():
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": M,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": M}}
    pair = SimpleNamespace(
        timestamp="t0",
        request_body={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
        response_body={"model": "claude-opus-5", "content": [{"type": "text", "text": "ok"}],
                       "stop_reason": "end_turn", "usage": usage},
        new_messages=[], is_partial=False, stop_reason="end_turn",
    )
    meta = builder.SessionMetadata(session_id="s", model="claude-opus-5")
    builder.build_trajectory("s", [pair], meta)
    # 1h 档 2×：1M × $5 × 2 = $10（旧实现是 1M × $15 × 0.25 = $3.75）
    assert _close(meta.total_cost_usd, 10.0)
