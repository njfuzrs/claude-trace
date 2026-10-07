"""代理不得对上游请求设超时：何时放弃由客户端决定。

回归：曾用 ClientTimeout(total=300, sock_read=300)，把超过 5 分钟的流式响应
掐成 504/502。aiohttp 默认 total=300，所以「不传 timeout」不等于不限时。
"""
import asyncio

import aiohttp
from aiohttp import web

import proxy


def test_upstream_timeout_constant_is_unbounded():
    t = proxy.UPSTREAM_TIMEOUT
    assert t.total is None
    assert t.connect is None
    assert t.sock_connect is None
    assert t.sock_read is None


def test_upstream_session_uses_unbounded_timeout(tmp_path):
    async def run():
        app = await proxy.create_app(
            upstream_base="http://127.0.0.1:9",
            output_dir=tmp_path,
            session_timeout=1800,
        )
        runner = web.AppRunner(app)
        await runner.setup()  # 触发 on_startup
        try:
            sess: aiohttp.ClientSession = app["upstream_session"]
            assert sess.timeout.total is None
            assert sess.timeout.sock_read is None
            assert sess.timeout.sock_connect is None
        finally:
            await runner.cleanup()

    asyncio.run(run())
