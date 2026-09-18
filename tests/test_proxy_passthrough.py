#!/usr/bin/env python3
"""代理必须按原始字节转发 messages 请求，不得改写/重序列化请求体。

曾经 --force-thinking 会把 thinking.type=adaptive 改写成带 effort=max 的对象，
再 json.dumps 整份 body。后果：
  1. effort 不属于 thinking（应在 output_config），新模型直接 400
  2. json.dumps 默认 ensure_ascii=True，tool_use 里的中文被改成 \\uXXXX
  3. thinking.display 等 Claude Code 新字段被丢掉
Claude Code 2.1.275+ 因此报 Invalid tool use format。
这条测试锁死「原始 bytes 原样到达上游」，包括 force_thinking=1 的旧配置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import proxy as P


def _raw_body() -> bytes:
    """紧凑 JSON + 中文 + 字段顺序与 json.dumps 默认不同。

    一旦代理重序列化，字节就会对不上。中文用 UTF-8 原样放入，
    对照 json.dumps 默认 ensure_ascii=True 会变成 \\uXXXX。
    """
    return (
        '{"model":"claude-opus-5","max_tokens":16000,"stream":false,'
        '"thinking":{"type":"adaptive","display":"omitted"},'
        '"messages":[{"role":"user","content":"修复"},'
        '{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01",'
        '"name":"Edit","input":{"file_path":"/tmp/中文.py",'
        '"old_string":"foo","new_string":"bar"}}]}]}'
    ).encode("utf-8")


def _messages_request(body: bytes) -> dict:
    parsed = json.loads(body)
    assert parsed["thinking"]["type"] == "adaptive"
    assert "effort" not in parsed["thinking"]
    assert parsed["thinking"]["display"] == "omitted"
    return parsed


@pytest.fixture
def output_dir(tmp_path):
    return tmp_path / "trajectories"


async def _run_proxy_against_upstream(
    output_dir: Path,
    force_thinking: int,
    raw_body: bytes,
) -> tuple[bytes, dict, int]:
    """起一个假上游 + 代理，发 raw_body，返回上游收到的 bytes、头、状态码。"""
    captured: dict = {}

    async def fake_upstream(request: web.Request) -> web.Response:
        captured["body"] = await request.read()
        captured["path"] = request.path
        captured["content_type"] = request.headers.get("Content-Type", "")
        return web.Response(
            status=200,
            content_type="application/json",
            body=(
                b'{"id":"msg_1","type":"message","role":"assistant",'
                b'"content":[{"type":"text","text":"ok"}],'
                b'"stop_reason":"end_turn",'
                b'"usage":{"input_tokens":1,"output_tokens":1}}'
            ),
        )

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{path:.*}", fake_upstream)

    proxy_app = await P.create_app(
        upstream_base="http://placeholder",
        output_dir=output_dir,
        session_timeout=1800,
        force_thinking=force_thinking,
    )

    async with TestServer(upstream_app) as upstream_server:
        proxy_app["upstream_base"] = str(upstream_server.make_url("/")).rstrip("/")
        async with TestServer(proxy_app) as proxy_server:
            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/messages",
                    data=raw_body,
                    headers={"Content-Type": "application/json"},
                )
                status = resp.status
                await resp.read()

    return captured["body"], captured, status


@pytest.mark.asyncio
async def test_messages请求按原始字节转发(output_dir):
    """★核心：上游收到的 body 必须与客户端发出的 bytes 完全一致。"""
    raw = _raw_body()
    got, meta, status = await _run_proxy_against_upstream(
        output_dir, force_thinking=0, raw_body=raw,
    )
    assert status == 200
    assert got == raw, (
        f"请求体被改写了\n发出: {raw!r}\n收到: {got!r}"
    )
    assert meta["path"] == "/v1/messages"


@pytest.mark.asyncio
async def test_force_thinking开启也不改写请求体(output_dir):
    """旧 launchd / channels.json 仍可能传 force_thinking=1，必须是 no-op。"""
    raw = _raw_body()
    got, _, status = await _run_proxy_against_upstream(
        output_dir, force_thinking=1, raw_body=raw,
    )
    assert status == 200
    assert got == raw
    parsed = _messages_request(got)
    assert "effort" not in parsed["thinking"]
    # 中文不得被转成 \\uXXXX
    assert "中文.py".encode("utf-8") in got
    assert b"\\u" not in got


@pytest.mark.asyncio
async def test_json_dumps默认序列化会破坏这份请求体():
    """对照：说明为什么不能 json.dumps 后再发。

    这份断言锁的是「默认 dumps 确实会改字节」，防止以后有人觉得 dumps 一下没事。
    """
    raw = _raw_body()
    parsed = json.loads(raw)
    dumped = json.dumps(parsed).encode()
    assert dumped != raw
    # 默认 ensure_ascii=True 会把中文变成 \\uXXXX
    assert "中文".encode("utf-8") not in dumped
    # 即便 ensure_ascii=False，字段顺序/分隔也不保证与原始 bytes 一致
    dumped_utf8 = json.dumps(parsed, ensure_ascii=False).encode()
    assert dumped_utf8 != raw


@pytest.mark.asyncio
async def test_count_tokens走透传且不采集(output_dir):
    """count_tokens 曾被当成 messages 采集，污染会话目录。路径必须原样透传。"""
    captured: dict = {}

    async def fake_upstream(request: web.Request) -> web.Response:
        captured["body"] = await request.read()
        captured["path"] = request.path
        return web.Response(status=200, body=b'{"input_tokens":12}')

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{path:.*}", fake_upstream)
    proxy_app = await P.create_app(
        upstream_base="http://placeholder",
        output_dir=output_dir,
        session_timeout=1800,
    )

    body = b'{"model":"claude-opus-5","messages":[{"role":"user","content":"hi"}]}'
    async with TestServer(upstream_app) as upstream_server:
        proxy_app["upstream_base"] = str(upstream_server.make_url("/")).rstrip("/")
        async with TestServer(proxy_app) as proxy_server:
            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/messages/count_tokens",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 200
                await resp.read()

    assert captured["path"] == "/v1/messages/count_tokens"
    assert captured["body"] == body
    sessions_root = output_dir / "sessions"
    sessions = list(sessions_root.glob("*")) if sessions_root.exists() else []
    # 透传路径不得建会话目录
    assert sessions == []


def test_废弃的force_thinking参数仍可解析():
    """旧 launchd / 脚本还在传 --force-thinking，不能 unrecognized arguments。"""
    import sys
    argv = sys.argv
    try:
        sys.argv = ["proxy.py", "--force-thinking", "1", "--upload-status"]
        args = P.parse_args()
        assert args.force_thinking == 1
        assert args.upload_status is True
    finally:
        sys.argv = argv
