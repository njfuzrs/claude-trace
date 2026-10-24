#!/usr/bin/env python3
"""上游错误响应（429 / 403 / 502）的采集口径

修复前：流式请求遇到 429 时，代理把 JSON 错误体当 SSE 重组，找不到 message_stop
→ 记成 content=[] 的 partial，错误类型丢失。实测 1014 条 partial 里 1004 条是 429，
traj 的 partial_pairs 虚高、error_responses 恒为 0。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import builder as B
import proxy as P

_ERR_429 = (
    b'{"type":"error","error":{"type":"rate_limit_error",'
    b'"message":"Number of request tokens has exceeded your per-minute rate limit"}}'
)


async def _post_once(output_dir: Path, status: int, body: bytes, stream: bool,
                     content_type: str = "application/json", headers=None):
    """假上游固定返回 (status, body)，经代理发一次 messages 请求"""

    async def fake_upstream(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(status=status, body=body, content_type=content_type,
                            headers=headers or {})

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{path:.*}", fake_upstream)
    proxy_app = await P.create_app(
        upstream_base="http://placeholder", output_dir=output_dir, session_timeout=1800,
    )
    req = {
        "model": "claude-opus-5-5", "stream": stream, "max_tokens": 10,
        "metadata": {"user_id": json.dumps({"session_id": "sess-err"})},
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read"}],
    }
    async with TestServer(upstream_app) as up:
        proxy_app["upstream_base"] = str(up.make_url("/")).rstrip("/")
        async with TestServer(proxy_app) as ps:
            async with TestClient(ps) as client:
                resp = await client.post("/v1/messages", json=req)
                got_body = await resp.read()
                got = (resp.status, got_body, {k.lower(): v for k, v in resp.headers.items()})
            # 等后台落盘任务
            for s in proxy_app["session_manager"].active_sessions.values():
                await P._drain_pending_writes(s)
    return got


def _raw_records(output_dir: Path):
    files = list((output_dir / "sessions").glob("*/raw.jsonl"))
    assert files, "没有落盘 raw.jsonl"
    return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_流式429原样转发且不记成partial(tmp_path):
    status, body, headers = await _post_once(
        tmp_path, 429, _ERR_429, stream=True, headers={"retry-after": "7"},
    )
    # Claude Code 依赖状态码、错误体和 retry-after 做退避，必须原样到达
    assert status == 429
    assert body == _ERR_429
    assert headers.get("retry-after") == "7"
    assert "text/event-stream" not in headers.get("content-type", "")

    (rec,) = _raw_records(tmp_path)
    assert rec["is_partial"] is False
    assert rec["response"]["error"]["type"] == "rate_limit_error"
    assert rec["response"]["_http_status"] == 429


@pytest.mark.asyncio
async def test_网关返回非JSON的502也保留错误信息(tmp_path):
    html = b"<html><body>502 Bad Gateway</body></html>"
    status, body, _ = await _post_once(tmp_path, 502, html, stream=True,
                                       content_type="text/html")
    assert status == 502 and body == html
    (rec,) = _raw_records(tmp_path)
    assert rec["is_partial"] is False
    assert rec["response"]["error"]["type"] == "http_502"
    assert "Bad Gateway" in rec["response"]["error"]["message"]


@pytest.mark.asyncio
async def test_非流式成功响应不再被误判为partial(tmp_path):
    ok = (b'{"id":"msg_1","type":"message","role":"assistant",'
          b'"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn",'
          b'"usage":{"input_tokens":1,"output_tokens":1}}')
    status, _, _ = await _post_once(tmp_path, 200, ok, stream=False)
    assert status == 200
    (rec,) = _raw_records(tmp_path)
    assert rec["is_partial"] is False
    assert rec["stop_reason"] == "end_turn"


def test_SSE中途error事件被记录():
    sse = (
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","content":[]}}\n\n'
        b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    msg = P.reassemble_sse_response(sse)
    assert msg["error"]["type"] == "overloaded_error"
    assert msg["_complete"] is False


def _pair(resp, *, partial=False, thinking=True, ts="2026-10-24T10:00:00"):
    body = {"model": "claude-opus-5-5", "tools": [{"name": "Read"}],
            "messages": [{"role": "user", "content": "hi"}]}
    if thinking:
        body["thinking"] = {"type": "adaptive", "display": "updates"}
    return P.RequestResponsePair(
        timestamp=ts, request_body=body, request_headers={}, index=1,
        response_body=resp, new_messages=[], is_partial=partial,
        stop_reason=resp.get("stop_reason", ""), usage=resp.get("usage", {}),
        model="claude-opus-5-5",
    )


def _build(pairs):
    meta = B.SessionMetadata(session_id="s", model="claude-opus-5-5")
    return B.build_trajectory("s", pairs, meta)


def test_builder把错误响应计入error_responses而不是partial():
    err = P._parse_error_body(429, _ERR_429)
    ok = {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
          "usage": {"input_tokens": 1, "output_tokens": 1}, "_complete": True}
    traj = _build([_pair(err), _pair(ok)])
    dq = traj["info"]["data_quality"]
    assert dq["error_responses"] == 1
    assert dq["partial_pairs"] == 0
    # 错误响应不产生空的 assistant 消息
    assistants = [h for h in traj["history"] if h.get("role") == "assistant"]
    assert len(assistants) == 1


def test_builder标出上游剥掉thinking():
    no_think = {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
                "usage": {"output_tokens": 1}, "_complete": True}
    with_think = {"content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                              {"type": "text", "text": "done"}],
                  "stop_reason": "end_turn", "usage": {"output_tokens": 1}, "_complete": True}

    dq = _build([_pair(no_think), _pair(no_think)])["info"]["data_quality"]
    assert dq["thinking_requested"] == 2 and dq["thinking_returned"] == 0

    dq = _build([_pair(with_think), _pair(no_think, thinking=False)])["info"]["data_quality"]
    assert dq["thinking_requested"] == 1 and dq["thinking_returned"] == 1
