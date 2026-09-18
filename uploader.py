#!/usr/bin/env python3
"""
uploader.py — 可靠上传管理器

负责将采集到的会话数据可靠上传到云端平台。
特性：gzip 压缩、SHA256 校验、指数退避重试、持久化重试队列、服务端健康检查。

设计原则：
- SDK 自身闭环可靠，不依赖外部 tools/sync.py 兜底
- 每个文件独立重试，不因一个文件失败放弃其他
- 所有文件确认后才清理本地（原子性）
- 进程重启后能恢复未完成的上传
"""

import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

import aiohttp

logger = logging.getLogger("claude-trace")


# ─────────────────────────────────────────────
# 队列条目数据结构
# ─────────────────────────────────────────────

@dataclass
class UploadQueueItem:
    """持久化重试队列中的单个上传任务"""
    session_id: str
    file_type: str          # traj / raw / events
    filepath: str           # 原始文件绝对路径
    tool_source: str = "claude-code"
    gz_path: str = ""       # 压缩后文件路径（首次压缩后填入）
    sha256: str = ""        # 压缩文件的 SHA256
    retry_count: int = 0
    max_retries: int = 50   # 累计最大重试次数（覆盖约 24 小时）
    created_at: float = field(default_factory=time.time)
    last_attempt: float = 0.0
    status: str = "pending" # pending / failed
    error: str = ""


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _get_user_id() -> str:
    """获取用户标识：只认 TRAJ_USER_ID，未配置即留空。

    刻意不回退到系统登录名：它常含真实姓名，
    而这个值会作为 form 字段随每条轨迹上传。不配置就不上报。
    """
    return os.environ.get("TRAJ_USER_ID", "").strip()


def _get_device_id() -> str:
    """获取设备标识：只认 TRAJ_DEVICE_ID，未配置即留空。

    刻意不回退到主机名：它常含真实姓名与资产编号，
    理由同 _get_user_id()。
    """
    return os.environ.get("TRAJ_DEVICE_ID", "").strip()


def compress_and_hash(filepath: Path) -> tuple:
    """gzip 压缩文件并计算压缩后的 SHA256

    流式处理，不一次性读入内存。
    返回 (gz_path, sha256_hex)。
    """
    gz_path = filepath.with_suffix(filepath.suffix + ".gz")
    with open(filepath, "rb") as f_in:
        with gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    # 计算压缩文件的 SHA256
    sha256 = hashlib.sha256()
    with open(gz_path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return gz_path, sha256.hexdigest()


def is_empty_trajectory(session_dir: Path) -> bool:
    """判断会话是否为空轨迹（没有任何 TAO 步骤）

    读不出 traj 时返回 False（保守放行，宁可多传也不丢数据）。
    """
    traj_path = session_dir / "session.traj"
    if not traj_path.exists():
        return False
    try:
        data = json.loads(traj_path.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return not (data.get("trajectory") or [])


def infer_tool_source(session_dir: Path) -> str:
    """根据会话目录内容推断工具来源。

    默认 claude-code；Codex 导出目录会包含 codex_thread.json，
    或在 session.traj metadata 中写入 codex 采集标记。
    """
    override = os.environ.get("TRAJ_TOOL_SOURCE", "").strip()
    if override:
        return override

    if (session_dir / "codex_thread.json").exists():
        return "codex"

    traj_path = session_dir / "session.traj"
    if not traj_path.exists():
        return "claude-code"

    try:
        data = json.loads(traj_path.read_text())
    except Exception:
        return "claude-code"

    metadata = data.get("metadata", {}) or {}
    capture_channel = str(metadata.get("capture_channel", "") or "")
    model = str(metadata.get("model", "") or "")
    if capture_channel.startswith("codex_"):
        return "codex"
    if "codex" in model.lower():
        return "codex"

    return "claude-code"


# ─────────────────────────────────────────────
# 上传管理器
# ─────────────────────────────────────────────

class UploadManager:
    """可靠上传管理器

    生命周期：create_app on_startup 调用 start()，on_cleanup 调用 stop()。
    """

    # 上传文件列表：(file_type, filename)
    UPLOAD_FILES = [
        ("traj", "session.traj"),
        ("raw", "raw.jsonl"),
        ("events", "events.jsonl"),
    ]

    # 指数退避参数
    BACKOFF_BASE = 2
    BACKOFF_MAX = 32
    IMMEDIATE_RETRIES = 5       # 首次上传时的立即重试次数

    # 单文件上传体积上限（压缩后）。
    # 实测网关：50MB → 200，100MB → 413，上限落在两者之间，取 64MB 保守值。
    # 超过此值本地直接判死（status=oversize），不做 50 次注定 413 的无效重试 ——
    # 那会把 1.1GB 的 .gz 永久留在盘上，且每次扫描都重压一遍。
    MAX_UPLOAD_BYTES = 64 * 1024 * 1024

    # 后台任务间隔
    QUEUE_SCAN_INTERVAL = 300   # 5 分钟
    HEALTH_CHECK_INTERVAL = 60  # 60 秒

    # 启动补传节流：代理启动 30s 后开始，每个会话之间隔 2s，
    # 避免历史积压很多时补传把上行带宽占满、影响正常采集。
    BACKFILL_START_DELAY = 30
    BACKFILL_INTERVAL = 2

    def __init__(
        self,
        upload_url: str,
        upload_token: str,
        queue_dir: Optional[Path] = None,
        cleanup_after_upload: bool = True,
        sessions_dir: Optional[Path] = None,
        backfill_enabled: bool = True,
    ):
        base = upload_url.rstrip("/")
        self._upload_endpoint = f"{base}/api/v1/upload/session-file"
        self._health_endpoint = f"{base}/api/v1/health"
        self._upload_token = upload_token

        self._queue_dir = queue_dir or (Path.home() / ".claude-trace")
        self._queue_file = self._queue_dir / ".upload_queue.jsonl"
        self._cleanup_after_upload = cleanup_after_upload

        self._user_id = _get_user_id()
        self._device_id = _get_device_id()

        self._server_healthy = True
        self._queue: List[UploadQueueItem] = []
        self._queue_lock = asyncio.Lock()

        self._http_session: Optional[aiohttp.ClientSession] = None
        self._health_task: Optional[asyncio.Task] = None
        self._scan_task: Optional[asyncio.Task] = None

        # 启动补传：sessions 目录用于扫描未上传的历史会话
        self._sessions_dir = sessions_dir
        self._backfill_enabled = backfill_enabled and sessions_dir is not None
        self._backfill_task: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────

    async def start(self):
        """启动上传管理器：创建 HTTP 会话、加载队列、启动后台任务"""
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
        )
        self._load_queue()
        self._health_task = asyncio.create_task(self._health_check_loop())
        self._health_task.add_done_callback(self._log_task_error)
        self._scan_task = asyncio.create_task(self._queue_scan_loop())
        self._scan_task.add_done_callback(self._log_task_error)
        logger.info(
            "上传管理器已启动 (user=%s, device=%s, 队列=%d 项)",
            self._user_id, self._device_id, len(self._queue),
        )

        # 启动补传：扫描盘上所有「有轨迹但没 .uploaded 标记」的会话。
        #
        # 这是整个上传链路的兜底，也是最重要的一环 —— 有了它，上传就不再依赖
        # 任何单一触发点成功。sid-code 的教训正是「只挂 SessionEnd」：触发器一
        # 不灵，52 个会话一次都没传上去，而且没人发现。判据只看磁盘现状
        # （目录在、traj 非空、.uploaded 缺），刻意不依赖重试队列 ——
        # 队列文件本身可能丢失或损坏。
        if self._backfill_enabled:
            self._backfill_task = asyncio.create_task(self._backfill_loop())
            self._backfill_task.add_done_callback(self._log_task_error)

    async def stop(self):
        """停止上传管理器：持久化队列、取消后台任务、关闭 HTTP 会话"""
        if self._health_task:
            self._health_task.cancel()
        if self._scan_task:
            self._scan_task.cancel()
        if self._backfill_task:
            self._backfill_task.cancel()
        self._save_queue()
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        logger.info("上传管理器已停止 (队列=%d 项)", len(self._queue))

    @staticmethod
    def _log_task_error(task: asyncio.Task):
        if not task.cancelled() and task.exception():
            logger.error("上传管理器后台任务异常: %s", task.exception())

    # ─────────────────────────────────────────
    # 队列持久化
    # ─────────────────────────────────────────

    def _load_queue(self):
        """从磁盘加载重试队列，过滤掉源文件已不存在的条目"""
        self._queue = []
        if not self._queue_file.exists():
            return
        try:
            for line in self._queue_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    item = UploadQueueItem(**data)
                    # 过滤：源文件或压缩文件至少有一个存在
                    if Path(item.filepath).exists() or (item.gz_path and Path(item.gz_path).exists()):
                        self._queue.append(item)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug("队列条目解析失败，跳过: %s", e)
        except Exception as e:
            logger.warning("加载上传队列失败: %s", e)

    def _save_queue(self):
        """原子写入队列到磁盘（先写 .tmp 再 rename）"""
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._queue_file.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                for item in self._queue:
                    f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
            os.replace(str(tmp_path), str(self._queue_file))
        except Exception as e:
            logger.warning("保存上传队列失败: %s", e)
            tmp_path.unlink(missing_ok=True)

    # ─────────────────────────────────────────
    # 主入口：上传会话
    # ─────────────────────────────────────────

    async def upload_session(self, session_dir: Path, session_id: str, tool_source: Optional[str] = None):
        """上传会话的所有文件（fire-and-forget，失败不影响主流程）

        每个文件独立处理：压缩 → 上传（带重试）→ 失败则入队。
        全部成功后写 .uploaded 标记，可选清理本地文件。
        """
        # 空轨迹闸门：没有任何 TAO 步骤的会话没有训练价值，不上传。
        # 主要防的是 count_tokens 之类的探测请求 —— proxy 的 _is_messages_request
        # 已在采集侧拦掉，这里作为第二道防线，避免未来新增的探测端点再次污染
        # 存储（历史上这类垃圾占了拉取数据的 21%）。
        if is_empty_trajectory(session_dir):
            logger.info("空轨迹会话，跳过上传: %s", session_id[:8])
            return

        # results 记录传成功的，oversize 记录因过大放弃的。
        # 「哪些还没传成」由末尾按盘上文件重新推导（retriable），
        # 不再用一个 all_confirmed 布尔量 —— 它分不清「可重试的失败」
        # 和「重试也没用的过大」，而这两者的处置完全不同。
        results = {}  # file_type → {"sha256": ..., "gz_size": ...}
        oversize = {}  # file_type → 原因（超过服务端上限，本地保留）
        resolved_tool_source = tool_source or infer_tool_source(session_dir)

        for file_type, filename in self.UPLOAD_FILES:
            filepath = session_dir / filename
            if not filepath.exists():
                continue

            gz_path = None
            try:
                # 压缩前先按原始体积粗筛：gzip 对 JSONL 通常压到 1/3 左右，
                # 原始超过上限 4 倍的基本不可能压进去。省掉几百 MB 的无效压缩
                # （实测有 766MB 的 raw.jsonl，压一次要几十秒还是 413）。
                raw_size = filepath.stat().st_size
                if raw_size > self.MAX_UPLOAD_BYTES * 4:
                    oversize[file_type] = f"{raw_size / 1e6:.0f}MB(原始)"
                    logger.warning(
                        "文件过大跳过压缩: %s/%s (%.0fMB) — 本地保留",
                        session_id[:8], filename, raw_size / 1e6,
                    )
                    continue

                # 在线程池中压缩 + 计算 hash（避免阻塞事件循环）
                loop = asyncio.get_running_loop()
                gz_path, sha256 = await loop.run_in_executor(
                    None, compress_and_hash, filepath,
                )

                item = UploadQueueItem(
                    session_id=session_id,
                    file_type=file_type,
                    tool_source=resolved_tool_source,
                    filepath=str(filepath),
                    gz_path=str(gz_path),
                    sha256=sha256,
                )

                # 服务端不可达时直接入队
                if not self._server_healthy:
                    logger.info("服务端不可达，入队: %s/%s", session_id[:8], filename)
                    await self._enqueue(item)
                    continue

                # 尝试上传（带立即重试）
                success = await self._retry_with_backoff(item)
                if success:
                    results[file_type] = {
                        "sha256": sha256,
                        "gz_size": gz_path.stat().st_size if gz_path.exists() else 0,
                    }
                    # 上传成功，清理临时压缩文件
                    gz_path.unlink(missing_ok=True)
                    logger.info("上传成功: %s/%s", session_id[:8], filename)
                elif item.status == "oversize":
                    # 压缩后仍超上限：本地判死，不入队（入队只会每 5 分钟重压一次）
                    oversize[file_type] = item.error or "过大"
                    gz_path.unlink(missing_ok=True)
                else:
                    # 立即重试失败，入队等待后台扫描
                    await self._enqueue(item)
                    logger.warning("上传失败，已入队: %s/%s (retries=%d)", session_id[:8], filename, item.retry_count)

            except Exception as e:
                logger.warning("上传处理异常: %s/%s — %s", session_id[:8], filename, e)
                # 清理可能残留的压缩文件
                if gz_path and gz_path.exists():
                    gz_path.unlink(missing_ok=True)

        # ── 是否写 .uploaded 标记 ──
        #
        # 两条硬性条件：
        #   1) traj 必须真的上云。traj 是训练用的那份，它没上去这个会话就不算落地，
        #      哪怕 events.jsonl 传成功了也不能标记（否则积压数字会假性归零）。
        #   2) 没有「可重试」的缺口。oversize 是终态、重试永远不会成功，
        #      所以它不阻止标记 —— 否则每次启动补传都要重压几百 MB 再吃一个 413，
        #      积压永远降不下去。可重试的失败则必须留着，等下次补传。
        traj_exists = (session_dir / "session.traj").exists()
        traj_done = ("traj" in results) or (not traj_exists)
        retriable = [
            ft for ft, fname in self.UPLOAD_FILES
            if (session_dir / fname).exists()
            and ft not in results
            and ft not in oversize
        ]

        if not results or not traj_done or retriable:
            if oversize and not traj_done and not retriable:
                # traj 自身过大：这个会话传不上去，本地是唯一副本。
                # 说后果不说现象 —— 这条日志要能直接读出「它没上云」。
                logger.warning(
                    "会话轨迹因过大无法上云，本地保留为唯一副本: %s (%s)",
                    session_id[:8], oversize.get("traj", "traj 过大"),
                )
                # 落盘判据：否则每次启动补传都要重压一遍几百 MB 再吃一个 413
                self._record_oversize(session_dir, oversize)
            return

        self._write_uploaded_marker(session_dir, results, oversize=oversize or None)
        if oversize:
            logger.warning(
                "会话已标记上传，但 %s 因过大未上云（本地保留）: %s",
                ",".join(oversize), session_id[:8],
            )
        # 有文件因过大没上传时绝不删本地 —— 那是它唯一的副本
        elif self._cleanup_after_upload:
            self._cleanup_session_files(session_dir)
            logger.info("本地文件已清理: %s", session_id[:8])

    # ─────────────────────────────────────────
    # 单文件上传
    # ─────────────────────────────────────────

    async def _upload_single_file(self, item: UploadQueueItem) -> bool:
        """上传单个压缩文件，带 SHA256 校验

        返回 True 表示上传成功（服务端确认 SHA256 一致或文件已存在）。
        """
        gz_path = Path(item.gz_path)
        if not gz_path.exists():
            item.error = "压缩文件不存在"
            return False

        if not self._http_session:
            item.error = "HTTP 会话未初始化"
            return False

        gz_size = gz_path.stat().st_size
        # 体积预检：超过服务端上限的文件本地直接判死，不做 50 次无效重试。
        # 实测网关上限在 50MB(200) 与 100MB(413) 之间，取 64MB 作为保守阈值。
        if gz_size > self.MAX_UPLOAD_BYTES:
            item.error = (
                f"压缩后 {gz_size / 1e6:.1f}MB 超过上限 "
                f"{self.MAX_UPLOAD_BYTES / 1e6:.0f}MB，跳过（本地数据保留）"
            )
            item.status = "oversize"
            logger.warning(
                "文件过大跳过上传: %s/%s (%.1fMB) — 本地数据保留，不再重试",
                item.session_id[:8], item.file_type, gz_size / 1e6,
            )
            return False

        headers = {
            "X-Upload-Token": self._upload_token,
            "X-Content-SHA256": item.sha256,
        }

        # force=true：允许覆盖服务端已有文件。
        # 不加这个参数时服务端对 traj 返回 409，而老实现把 409 当幂等成功 ——
        # 于是「超时上传了残缺 traj → 会话继续 → 最终上传完整 traj」这条路径上，
        # 云端永远停留在第一次那份残缺版本（实测 126 个会话被这样锁死）。
        params = {"force": "true"}

        # FormData 每次尝试都要重建：body 是一次性的流，重试时不能复用。
        # 同时用 with 管住文件句柄 —— 老实现的裸 open() 在异常路径上会泄漏 fd。
        with open(gz_path, "rb") as fh:
            data = aiohttp.FormData()
            data.add_field(
                "file", fh, filename=gz_path.name, content_type="application/gzip",
            )
            data.add_field("session_id", item.session_id)
            data.add_field("file_type", item.file_type)
            data.add_field("tool_source", item.tool_source)
            data.add_field("compressed", "true")
            data.add_field("user_id", self._user_id)
            data.add_field("device_id", self._device_id)

            async with self._http_session.post(
                self._upload_endpoint, data=data, headers=headers, params=params,
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    # 二次校验：服务端返回的 hash 必须一致
                    server_sha = body.get("sha256", "")
                    if server_sha and server_sha != item.sha256:
                        item.error = f"SHA256 不匹配: local={item.sha256[:16]} server={server_sha[:16]}"
                        logger.warning("SHA256 不匹配: %s/%s", item.session_id[:8], item.file_type)
                        return False
                    return True
                if resp.status == 409:
                    # 带了 force=true 仍返回 409：服务端不支持覆盖（老版本）。
                    # 视为成功以免无限重试，但明确告警 —— 云端可能是旧版本数据。
                    logger.warning(
                        "服务端拒绝覆盖 (409)：%s/%s 云端可能仍是旧版本",
                        item.session_id[:8], item.file_type,
                    )
                    return True
                if resp.status == 413:
                    # 服务端明确说太大：本地判死，不再重试
                    item.error = f"HTTP 413: 服务端拒绝（{gz_size / 1e6:.1f}MB 过大）"
                    item.status = "oversize"
                    logger.warning(
                        "服务端返回 413: %s/%s (%.1fMB) — 停止重试，本地数据保留",
                        item.session_id[:8], item.file_type, gz_size / 1e6,
                    )
                    return False
                text = await resp.text()
                item.error = f"HTTP {resp.status}: {text[:200]}"
                return False

    # ─────────────────────────────────────────
    # 指数退避重试
    # ─────────────────────────────────────────

    async def _retry_with_backoff(self, item: UploadQueueItem) -> bool:
        """指数退避重试上传（单次调用最多 IMMEDIATE_RETRIES 次尝试）

        延迟序列：2s, 4s, 8s, 16s, 32s
        """
        for attempt in range(self.IMMEDIATE_RETRIES):
            if item.retry_count >= item.max_retries:
                item.status = "failed"
                return False

            # 首次尝试不等待，后续指数退避
            if attempt > 0:
                delay = min(self.BACKOFF_BASE * (2 ** attempt), self.BACKOFF_MAX)
                await asyncio.sleep(delay)

            item.retry_count += 1
            item.last_attempt = time.time()

            try:
                if await self._upload_single_file(item):
                    return True
                # oversize 是终态：重试多少次都还是过大，立即放弃剩余尝试
                if item.status == "oversize":
                    return False
            except Exception as e:
                item.error = str(e)
                logger.debug(
                    "重试上传异常 (%d/%d): %s/%s — %s",
                    item.retry_count, item.max_retries,
                    item.session_id[:8], item.file_type, e,
                )

        return False

    # ─────────────────────────────────────────
    # 可观测性
    # ─────────────────────────────────────────

    @property
    def server_healthy(self) -> bool:
        """服务端当前是否可达（由后台健康检查维护）"""
        return self._server_healthy

    def queue_stats(self) -> dict:
        """重试队列的分状态计数

        老实现的 _process_queue 返回 void，队列里攒了多少 failed 项
        外部完全看不见 —— 静默丢数据。这里把计数暴露出去，
        供 /health 和 --upload-status 使用。
        """
        stats = {"total": len(self._queue), "pending": 0, "failed": 0, "oversize": 0}
        for item in self._queue:
            key = item.status if item.status in ("failed", "oversize") else "pending"
            stats[key] = stats.get(key, 0) + 1
        return stats

    # ─────────────────────────────────────────
    # 队列管理
    # ─────────────────────────────────────────

    async def _enqueue(self, item: UploadQueueItem):
        """将上传任务加入重试队列并持久化"""
        async with self._queue_lock:
            self._queue.append(item)
            self._save_queue()

    async def _queue_scan_loop(self):
        """后台队列扫描循环：每 5 分钟处理 pending 项"""
        while True:
            await asyncio.sleep(self.QUEUE_SCAN_INTERVAL)
            await self._process_queue()

    # ─────────────────────────────────────────
    # 启动补传（上传链路的兜底）
    # ─────────────────────────────────────────

    OVERSIZE_MARKER = ".upload_oversize"

    def _record_oversize(self, session_dir: Path, reasons: dict) -> None:
        """落盘「过大放弃」的判据，避免每次启动都重压一遍几百 MB

        记录 traj 当时的体积：文件变小（比如用新 builder 重建后）就说明
        判据过期，应重新尝试上传。
        """
        traj = session_dir / "session.traj"
        try:
            payload = {
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "limit_bytes": self.MAX_UPLOAD_BYTES,
                "traj_bytes": traj.stat().st_size if traj.exists() else 0,
                "reasons": reasons,
            }
            (session_dir / self.OVERSIZE_MARKER).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except OSError as e:
            logger.debug("写 oversize 标记失败 %s: %s", session_dir.name[:8], e)

    def _is_recorded_oversize(self, session_dir: Path, traj: Path) -> bool:
        """该会话是否已被判定过大且判据仍然成立

        判据过期（traj 变小了，或上限提高了）时返回 False 并删掉标记，
        让它重新进入补传队列 —— 否则一旦误判就永远没机会再传。
        """
        marker = session_dir / self.OVERSIZE_MARKER
        if not marker.exists():
            return False
        try:
            data = json.loads(marker.read_text())
            recorded_bytes = int(data.get("traj_bytes") or 0)
            recorded_limit = int(data.get("limit_bytes") or 0)
            current_bytes = traj.stat().st_size
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if current_bytes < recorded_bytes or self.MAX_UPLOAD_BYTES > recorded_limit:
            marker.unlink(missing_ok=True)
            logger.info(
                "oversize 判据已过期，重新纳入补传: %s", session_dir.name[:8],
            )
            return False
        return True

    def scan_pending_sessions(self) -> List[Path]:
        """扫描盘上所有待上传的会话目录

        判据只看磁盘现状，不依赖任何内存状态或队列文件：
          1. 是目录
          2. 没有 .uploaded 标记
          3. session.traj 存在且含 TAO 步骤（空轨迹没有训练价值）

        返回按修改时间倒序排列的目录列表（新的先传）。
        """
        if not self._sessions_dir or not self._sessions_dir.exists():
            return []
        pending = []
        try:
            entries = list(self._sessions_dir.iterdir())
        except OSError as e:
            logger.warning("扫描 sessions 目录失败: %s", e)
            return []
        for d in entries:
            try:
                if not d.is_dir() or (d / ".uploaded").exists():
                    continue
                # 必须有非空 session.traj 才补传。
                # 注意 is_empty_trajectory 在 traj 缺失时返回 False（保守放行），
                # 这里要显式排除「根本没建出 traj」的目录 —— 那多半是 subagent
                # 或标题生成的子目录（实测 9355 个），补传它们既没价值也很吵。
                traj = d / "session.traj"
                if not traj.exists() or traj.stat().st_size == 0:
                    continue
                # 已判定过大的会话不再反复尝试：压一次 200~350MB 要好几秒，
                # 而结果注定是 413。判据落盘（.upload_oversize），
                # 文件变小或上限提高后删掉该标记即可重新纳入补传。
                if self._is_recorded_oversize(d, traj):
                    continue
                if is_empty_trajectory(d):
                    continue
                pending.append(d)
            except OSError:
                continue
        pending.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return pending

    async def _backfill_loop(self):
        """启动补传：把盘上未上传的会话逐个补传上去

        串行执行 + 每个之间小睡：补传是后台兜底，不能和正常采集抢带宽。
        单个会话失败不影响其余（upload_session 内部已捕获异常并入队）。
        """
        # 等一会儿再开始：让健康检查先跑一轮，同时避开代理刚启动时的请求高峰
        await asyncio.sleep(self.BACKFILL_START_DELAY)

        pending = await asyncio.get_running_loop().run_in_executor(
            None, self.scan_pending_sessions,
        )
        if not pending:
            logger.info("启动补传：没有待上传的会话")
            return

        # 说后果而不是说现象：sid-code 的告警写「51 个会话未正常收尾」，
        # 描述正确却省掉了「所以它们没上云」，唯一的线索就这么被当成噪音了。
        logger.warning("启动补传：%d 个会话的轨迹仍未上云，开始补传", len(pending))

        ok = failed = 0
        for session_dir in pending:
            if not self._server_healthy:
                logger.info("服务端不可达，暂停补传（剩余 %d 个，下次启动继续）",
                            len(pending) - ok - failed)
                break
            try:
                await self.upload_session(session_dir, session_dir.name)
                if (session_dir / ".uploaded").exists():
                    ok += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.warning("补传异常 %s: %s", session_dir.name[:8], e)
            await asyncio.sleep(self.BACKFILL_INTERVAL)

        logger.info("启动补传完成：成功 %d，未完成 %d（未完成的已入队或留待下次启动）",
                    ok, failed)

    async def _process_queue(self):
        """处理队列中的 pending 项"""
        if not self._server_healthy:
            return

        async with self._queue_lock:
            if not self._queue:
                return

            remaining = []
            for item in self._queue:
                if item.status in ("failed", "oversize"):
                    # 终态项：保留条目供排查，但把 .gz 释放掉。
                    # 老实现把重试到死的 .gz 永久留在会话目录里（实测 1.1GB），
                    # 源文件仍在，真要重传随时能重新压缩。
                    if item.gz_path:
                        gz = Path(item.gz_path)
                        if gz.exists():
                            try:
                                freed = gz.stat().st_size
                                gz.unlink()
                                item.gz_path = ""
                                logger.info(
                                    "释放终态项压缩文件: %s/%s (%.1fMB)",
                                    item.session_id[:8], item.file_type, freed / 1e6,
                                )
                            except OSError as e:
                                logger.debug("释放 .gz 失败 %s: %s", gz, e)
                    remaining.append(item)
                    continue

                # 确保压缩文件存在
                if not item.gz_path or not Path(item.gz_path).exists():
                    # 尝试重新压缩
                    src = Path(item.filepath)
                    if src.exists():
                        try:
                            loop = asyncio.get_running_loop()
                            gz_path, sha256 = await loop.run_in_executor(
                                None, compress_and_hash, src,
                            )
                            item.gz_path = str(gz_path)
                            item.sha256 = sha256
                        except Exception as e:
                            item.error = f"重新压缩失败: {e}"
                            remaining.append(item)
                            continue
                    else:
                        # 源文件也不存在，丢弃
                        logger.debug("队列项源文件不存在，丢弃: %s/%s", item.session_id[:8], item.file_type)
                        continue

                success = await self._retry_with_backoff(item)
                if success:
                    # 清理压缩文件
                    Path(item.gz_path).unlink(missing_ok=True)
                    logger.info("队列补传成功: %s/%s", item.session_id[:8], item.file_type)
                else:
                    remaining.append(item)
                    if item.status == "failed":
                        logger.warning(
                            "队列项达到最大重试次数: %s/%s (retries=%d)",
                            item.session_id[:8], item.file_type, item.retry_count,
                        )

            self._queue = remaining
            self._save_queue()

    # ─────────────────────────────────────────
    # 健康检查
    # ─────────────────────────────────────────

    async def _health_check_loop(self):
        """后台健康检查循环：每 60 秒探测服务端"""
        while True:
            try:
                if self._http_session:
                    async with self._http_session.get(
                        self._health_endpoint,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        was_healthy = self._server_healthy
                        self._server_healthy = resp.status == 200
                        # 状态变化时记录日志
                        if self._server_healthy and not was_healthy:
                            logger.info("服务端恢复可达")
                        elif not self._server_healthy and was_healthy:
                            logger.warning("服务端不可达 (HTTP %d)", resp.status)
                else:
                    self._server_healthy = False
            except Exception:
                if self._server_healthy:
                    logger.warning("服务端不可达 (连接异常)")
                self._server_healthy = False
            await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)

    # ─────────────────────────────────────────
    # 上传确认后处理
    # ─────────────────────────────────────────

    @staticmethod
    def _write_uploaded_marker(
        session_dir: Path, results: dict, oversize: Optional[dict] = None,
    ):
        """写入 .uploaded 标记文件，记录上传确认信息

        oversize 记录「因超过服务端上限而没上云」的文件。写进标记里是为了
        日后能查清云端为什么缺这个文件 —— 否则只能看到 files 里少一项，
        分不清是过大跳过还是上传漏了。
        """
        marker = session_dir / ".uploaded"
        payload = {
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": results,
        }
        if oversize:
            payload["oversize_skipped"] = oversize
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _cleanup_session_files(session_dir: Path):
        """清理本地数据文件，保留 .uploaded 标记

        受 TRAJ_CLEANUP_AFTER_UPLOAD 环境变量控制。
        """
        # 删除数据文件
        for name in ("session.traj", "raw.jsonl", "events.jsonl"):
            (session_dir / name).unlink(missing_ok=True)
        # 删除 raw/ 子目录（如果存在）
        raw_dir = session_dir / "raw"
        if raw_dir.is_dir():
            shutil.rmtree(raw_dir, ignore_errors=True)
        # 删除残留的 .gz 临时文件
        for gz in session_dir.glob("*.gz"):
            gz.unlink(missing_ok=True)
