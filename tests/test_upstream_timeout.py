"""上游连接的超时与连接池契约。

1. 读阶段不设超时：何时放弃由客户端决定。
   回归：曾用 ClientTimeout(total=300, sock_read=300)，把超过 5 分钟的流式响应
   掐成 504/502。aiohttp 默认 total=300，所以「不传 timeout」不等于不限时。
2. 建连阶段有上限，连接池无上限，socket 开 TCP keepalive。
   回归：0.4.7 换网后僵尸连接占满默认 limit=100 的池，新请求无限排队，
   Claude Code 等满 API_TIMEOUT_MS（20 分钟）才报错。
"""
import asyncio
import socket

import aiohttp
from aiohttp import web

import proxy


def test_upstream_timeout_read_phase_is_unbounded():
    t = proxy.UPSTREAM_TIMEOUT
    assert t.total is None
    assert t.sock_read is None


def test_upstream_timeout_connect_phase_is_bounded():
    # 契约变更：建连阶段必须有上限，否则握手卡住的新连接同样会让请求无限挂起
    t = proxy.UPSTREAM_TIMEOUT
    assert t.connect == proxy.UPSTREAM_CONNECT_TIMEOUT_SEC
    assert t.sock_connect == proxy.UPSTREAM_CONNECT_TIMEOUT_SEC
    assert 0 < proxy.UPSTREAM_CONNECT_TIMEOUT_SEC <= 60


def test_socket_factory_enables_keepalive():
    addr_info = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0))
    sock = proxy._upstream_socket_factory(addr_info)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle_opt is not None:
            assert sock.getsockopt(socket.IPPROTO_TCP, idle_opt) == proxy.UPSTREAM_KEEPALIVE_IDLE_SEC
        if hasattr(socket, "TCP_KEEPCNT"):
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == proxy.UPSTREAM_KEEPALIVE_CNT
    finally:
        sock.close()


def test_upstream_session_config(tmp_path):
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
            assert sess.timeout.sock_connect == proxy.UPSTREAM_CONNECT_TIMEOUT_SEC
            # 连接池不设上限：僵尸连接不能把新请求饿死
            assert sess.connector.limit == 0
            assert sess.connector.limit_per_host == 0
            assert proxy._upstream_connections_in_use(app) == 0
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_requests_not_starved_by_hung_connections(tmp_path):
    """端到端：上游挂住 N 条请求（模拟死连接）后，新请求仍能立刻拿到连接。

    N 取 105，超过 aiohttp 默认 limit=100：旧配置下第 101 条起就会在池里排队。
    """
    hung = 105

    async def run():
        release = asyncio.Event()

        async def upstream_handler(request):
            if request.path == "/hang":
                await release.wait()
            return web.Response(text="ok")

        up_app = web.Application()
        up_app.router.add_route("*", "/{tail:.*}", upstream_handler)
        up_runner = web.AppRunner(up_app)
        await up_runner.setup()
        site = web.TCPSite(up_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        app = await proxy.create_app(
            upstream_base=f"http://127.0.0.1:{port}",
            output_dir=tmp_path,
            session_timeout=1800,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        sess: aiohttp.ClientSession = app["upstream_session"]
        tasks = []
        try:
            async def hang():
                async with sess.get(f"http://127.0.0.1:{port}/hang") as r:
                    await r.read()

            tasks = [asyncio.create_task(hang()) for _ in range(hung)]
            # 等这些请求都真正占住连接
            for _ in range(100):
                if proxy._upstream_connections_in_use(app) >= hung:
                    break
                await asyncio.sleep(0.05)
            assert proxy._upstream_connections_in_use(app) >= hung

            async def fresh():
                async with sess.get(f"http://127.0.0.1:{port}/ok") as r:
                    return r.status

            assert await asyncio.wait_for(fresh(), timeout=5) == 200
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runner.cleanup()
            await up_runner.cleanup()

    asyncio.run(run())
