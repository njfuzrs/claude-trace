"""退出预算测试：在途 SSE 不能把采集器拖过守护进程的 15 秒收尾预算

v0.3.1 事故链：aiohttp AppRunner 默认 shutdown_timeout=60 秒，退出时只要有一条
流在跑，runner.cleanup() 就等满 60 秒 → proxy-daemon.sh 15 秒后强杀 →
launchd bootout 迟迟不完成 → 紧接着的 bootstrap 报 5: Input/output error →
服务停在未加载状态，KeepAlive 管不到，代理直到手动 bootstrap 才回来。
"""

import asyncio
import re
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

import proxy

ROOT = Path(__file__).resolve().parent.parent

# proxy-daemon.sh 等采集器优雅退出的上限（秒）
DAEMON_BUDGET_SEC = 15


# aiohttp RequestHandler.shutdown 把 timeout 用两次（等待 + 取消后等待）
WORST_CASE_SEC = 2 * proxy.SHUTDOWN_TIMEOUT_SEC


def test_退出上限明显小于守护进程预算():
    # 最坏情况也要留出落盘、上传队列持久化、Codex watcher join 的余量
    assert WORST_CASE_SEC <= DAEMON_BUDGET_SEC / 2


@pytest.mark.parametrize("rel", ["proxy.py", "trace_agent.py"])
def test_两个入口都显式设置shutdown_timeout(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    runners = re.findall(r"web\.AppRunner\(([^)]*)\)", text)
    assert runners, rel
    for args in runners:
        assert "shutdown_timeout=SHUTDOWN_TIMEOUT_SEC" in args, (rel, args)


@pytest.mark.asyncio
async def test_在途流式响应不拖住退出():
    """真起一个慢流服务，客户端连着时 cleanup，耗时必须受 SHUTDOWN_TIMEOUT_SEC 约束"""
    started = asyncio.Event()

    async def slow_stream(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        started.set()
        # 模拟一条比 aiohttp 默认 60 秒还长的 SSE
        for _ in range(120):
            await resp.write(b"data: ping\n\n")
            await asyncio.sleep(1)
        return resp

    app = web.Application()
    app.router.add_get("/stream", slow_stream)
    runner = web.AppRunner(app, shutdown_timeout=proxy.SHUTDOWN_TIMEOUT_SEC)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def consume():
        async with ClientSession() as s:
            try:
                async with s.get(f"http://127.0.0.1:{port}/stream") as r:
                    async for _ in r.content:
                        pass
            except Exception:
                pass  # 服务端关连接是预期

    client = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)

    t0 = time.monotonic()
    await runner.cleanup()
    elapsed = time.monotonic() - t0

    client.cancel()
    await asyncio.gather(client, return_exceptions=True)
    assert elapsed < WORST_CASE_SEC + 2, f"cleanup 用了 {elapsed:.1f}s"
    assert elapsed < DAEMON_BUDGET_SEC, f"cleanup 用了 {elapsed:.1f}s，超出守护进程预算"
