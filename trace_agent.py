#!/usr/bin/env python3
"""
trace_agent.py — 统一采集入口

默认同时启动：
1. Claude Code HTTP 代理采集
2. Codex rollout watcher 增量采集

目标：
- 对用户只暴露一个主执行文件
- 保持 Claude 代理参数和行为兼容
- Codex 默认开启，但不可用时只记录日志，不影响 Claude 代理
"""

import argparse
import asyncio
import logging
import os
import shutil
import signal
import threading
from pathlib import Path

from aiohttp import web

from builder import save_trajectory
from import_codex import CodexImporter, CodexRolloutWatcher
from proxy import DataCollector, SessionManager, _log_task_exception, create_app
from uploader import UploadManager

logger = logging.getLogger("claude-trace")


def parse_args():
    parser = argparse.ArgumentParser(
        description="claude-trace — Claude Code + Codex 统一轨迹采集",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=4000, help="Claude 代理监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="Claude 代理监听地址")
    parser.add_argument("--output", default="./trajectories", help="轨迹数据输出目录")
    parser.add_argument("--upstream", default="https://api.anthropic.com", help="Claude 上游 API 地址")
    parser.add_argument("--session-timeout", type=int, default=300, help="Claude 会话超时时间（秒）")
    parser.add_argument(
        "--events-dir",
        default=str(Path.home() / ".claude" / "trajectory_events"),
        help="Claude Hooks 事件数据目录",
    )
    parser.add_argument(
        "--save-raw",
        action="store_true",
        default=False,
        help="保存 Claude 原始请求/响应 JSON 文件到 raw/ 子目录",
    )
    parser.add_argument(
        "--no-save-raw",
        dest="save_raw",
        action="store_false",
        help="不保存 Claude 原始请求/响应 JSON 文件",
    )
    parser.add_argument(
        "--force-thinking",
        type=int,
        default=0,
        metavar="BUDGET",
        help="Claude adaptive thinking 改写预算。非 0 时提高 thinking blocks 产生概率。",
    )
    parser.add_argument(
        "--codex-watch",
        dest="codex_watch",
        action="store_true",
        default=True,
        help="默认开启 Codex watcher，监听 ~/.codex/sessions 变化",
    )
    parser.add_argument(
        "--no-codex-watch",
        dest="codex_watch",
        action="store_false",
        help="关闭 Codex watcher，仅保留 Claude 代理采集",
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"), help="Codex 数据目录")
    parser.add_argument("--codex-cmd", default="codex", help="Codex CLI 命令")
    parser.add_argument("--codex-state-file", default="", help="Codex watcher 状态文件路径")
    parser.add_argument("--codex-poll-interval", type=float, default=2.0, help="Codex watcher 扫描间隔（秒）")
    parser.add_argument("--codex-debounce-sec", type=float, default=2.0, help="Codex rollout 变化后的等待时间（秒）")
    parser.add_argument("--codex-finalize-sec", type=float, default=8.0, help="Codex 安静期后二次确认时间（秒）")
    parser.add_argument("--codex-retry-sec", type=float, default=10.0, help="Codex 导出失败后的重试间隔（秒）")
    parser.add_argument("--verbose", action="store_true", help="详细日志输出")
    return parser.parse_args()


def _codex_available(args) -> tuple[bool, str]:
    codex_home = Path(args.codex_home).expanduser()
    sessions_dir = codex_home / "sessions"
    if not codex_home.exists():
        return False, f"未发现 Codex 数据目录: {codex_home}"
    if not sessions_dir.exists():
        return False, f"未发现 Codex rollout 目录: {sessions_dir}"
    if shutil.which(args.codex_cmd) is None:
        return False, f"未找到 Codex 命令: {args.codex_cmd}"
    return True, ""


async def _start_codex_uploader(output_dir: Path) -> UploadManager | None:
    upload_url = os.environ.get("TRAJ_PLATFORM_URL", "").strip()
    upload_token = os.environ.get("TRAJ_UPLOAD_TOKEN", "").strip()
    if not upload_url or not upload_token:
        return None

    queue_dir = output_dir / ".codex_upload_queue"
    uploader = UploadManager(
        upload_url=upload_url,
        upload_token=upload_token,
        queue_dir=queue_dir,
        cleanup_after_upload=False,
    )
    await uploader.start()
    logger.info("Codex 上传器已启动: %s", upload_url)
    return uploader


async def _stop_codex_uploader(uploader: UploadManager | None):
    if uploader is not None:
        await uploader.stop()


async def _graceful_flush_proxy(app, runner, output_dir: Path):
    session_manager: SessionManager = app["session_manager"]
    collector: DataCollector = app["collector"]

    pending_futures = []
    loop = asyncio.get_running_loop()
    for _sid, session in list(session_manager.active_sessions.items()):
        if not session.is_subagent and (session.pairs or session.child_sessions):
            pairs_snapshot = list(session.pairs)
            children_snapshot = DataCollector._snapshot_children(session)
            traj_path, traj = collector._build_traj_data(session, pairs_snapshot, children_snapshot)
            if traj_path and traj:
                try:
                    future = loop.run_in_executor(None, save_trajectory, traj_path, traj)
                    pending_futures.append(future)
                except RuntimeError:
                    save_trajectory(traj_path, traj)
            total = len(session.pairs) + sum(len(c.pairs) for c in session.child_sessions)
            logger.info("优雅退出导出: %s | 步骤=%d", session.id[:8], total)

    if pending_futures:
        await asyncio.gather(*pending_futures, return_exceptions=True)

    if collector._uploader:
        await collector._uploader.stop()
    await runner.cleanup()
    logger.info("统一采集器已停止，数据保存在: %s", output_dir.resolve())


async def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output_dir = Path(args.output).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sessions").mkdir(parents=True, exist_ok=True)

    app = await create_app(
        upstream_base=args.upstream,
        output_dir=output_dir,
        session_timeout=args.session_timeout,
        save_raw=args.save_raw,
        events_dir=Path(args.events_dir).expanduser(),
        force_thinking=args.force_thinking,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    logger.info("=" * 50)
    logger.info("claude-trace 统一采集器已启动")
    logger.info("Claude 监听地址: http://%s:%d", args.host, args.port)
    logger.info("Claude 上游 API: %s", args.upstream)
    logger.info("输出目录: %s", output_dir.resolve())
    logger.info("Codex watcher 默认: %s", "开启" if args.codex_watch else "关闭")
    logger.info("=" * 50)
    logger.info("启动 Claude Code：")
    logger.info("  ANTHROPIC_BASE_URL=http://%s:%d claude", args.host, args.port)
    logger.info("=" * 50)

    session_manager: SessionManager = app["session_manager"]
    collector: DataCollector = app["collector"]

    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            await session_manager.cleanup_expired(collector=collector)

    cleanup_task = asyncio.create_task(cleanup_loop())
    cleanup_task.add_done_callback(_log_task_exception)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    watcher_stop_event = threading.Event()
    watcher_thread = None
    codex_uploader = await _start_codex_uploader(output_dir)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    if args.codex_watch:
        enabled, reason = _codex_available(args)
        if not enabled:
            logger.warning("Codex watcher 未启动: %s", reason)
        else:
            codex_home = Path(args.codex_home).expanduser()
            state_file = (
                Path(args.codex_state_file).expanduser()
                if args.codex_state_file
                else output_dir / "sessions" / ".codex_watch_state.json"
            )
            importer = CodexImporter(
                codex_home=codex_home,
                output_dir=output_dir / "sessions",
                codex_cmd=args.codex_cmd,
            )

            def on_codex_export(result: dict):
                if codex_uploader is None:
                    return

                future = asyncio.run_coroutine_threadsafe(
                    codex_uploader.upload_session(
                        result["session_dir"],
                        result["thread_id"],
                        tool_source="codex",
                    ),
                    loop,
                )

                def _log_upload_done(done_future):
                    try:
                        done_future.result()
                    except Exception as exc:
                        logger.warning("Codex 导出后自动上传失败: %s | %s", result["thread_id"][:12], exc)

                future.add_done_callback(_log_upload_done)

            watcher = CodexRolloutWatcher(
                importer=importer,
                codex_home=codex_home,
                state_file=state_file,
                poll_interval=args.codex_poll_interval,
                debounce_sec=args.codex_debounce_sec,
                finalize_sec=args.codex_finalize_sec,
                retry_sec=args.codex_retry_sec,
                on_export=on_codex_export,
            )

            def watcher_target():
                try:
                    watcher.watch_forever(stop_event=watcher_stop_event)
                except Exception:
                    logger.exception("Codex watcher 线程异常退出")

            watcher_thread = threading.Thread(
                target=watcher_target,
                name="codex-rollout-watcher",
                daemon=True,
            )
            watcher_thread.start()
            logger.info("Codex watcher 已启动: %s", codex_home / "sessions")

    try:
        await stop_event.wait()
    finally:
        watcher_stop_event.set()
        if watcher_thread is not None:
            watcher_thread.join(timeout=max(args.codex_poll_interval, 1.0) + 5.0)
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await _stop_codex_uploader(codex_uploader)
        await _graceful_flush_proxy(app, runner, output_dir)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
