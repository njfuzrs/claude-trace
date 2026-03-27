#!/usr/bin/env python3
"""
uploader.py — 可靠上传管理器

负责将采集到的会话数据可靠上传到云端平台。
特性：gzip 压缩、SHA256 校验、指数退避重试、持久化重试队列、服务端健康检查。

设计原则：
- SDK 自身闭环可靠，不依赖外部 sync.py 兜底
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
import platform
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
    """获取用户标识：环境变量 > os.getlogin() > unknown"""
    uid = os.environ.get("TRAJ_USER_ID", "").strip()
    if uid:
        return uid
    try:
        return os.getlogin()
    except OSError:
        return "unknown"


def _get_device_id() -> str:
    """获取设备标识：环境变量 > hostname"""
    did = os.environ.get("TRAJ_DEVICE_ID", "").strip()
    if did:
        return did
    return platform.node() or "unknown"


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

    # 后台任务间隔
    QUEUE_SCAN_INTERVAL = 300   # 5 分钟
    HEALTH_CHECK_INTERVAL = 60  # 60 秒

    def __init__(
        self,
        upload_url: str,
        upload_token: str,
        queue_dir: Optional[Path] = None,
        cleanup_after_upload: bool = True,
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

    async def stop(self):
        """停止上传管理器：持久化队列、取消后台任务、关闭 HTTP 会话"""
        if self._health_task:
            self._health_task.cancel()
        if self._scan_task:
            self._scan_task.cancel()
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
        results = {}  # file_type → {"sha256": ..., "gz_size": ...}
        all_confirmed = True
        resolved_tool_source = tool_source or infer_tool_source(session_dir)

        for file_type, filename in self.UPLOAD_FILES:
            filepath = session_dir / filename
            if not filepath.exists():
                continue

            gz_path = None
            try:
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
                    all_confirmed = False
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
                else:
                    # 立即重试失败，入队等待后台扫描
                    await self._enqueue(item)
                    all_confirmed = False
                    logger.warning("上传失败，已入队: %s/%s (retries=%d)", session_id[:8], filename, item.retry_count)

            except Exception as e:
                all_confirmed = False
                logger.warning("上传处理异常: %s/%s — %s", session_id[:8], filename, e)
                # 清理可能残留的压缩文件
                if gz_path and gz_path.exists():
                    gz_path.unlink(missing_ok=True)

        # 所有文件都确认后才标记和清理
        if all_confirmed and results:
            self._write_uploaded_marker(session_dir, results)
            if self._cleanup_after_upload:
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

        data = aiohttp.FormData()
        data.add_field(
            "file",
            open(gz_path, "rb"),
            filename=gz_path.name,
            content_type="application/gzip",
        )
        data.add_field("session_id", item.session_id)
        data.add_field("file_type", item.file_type)
        data.add_field("tool_source", item.tool_source)
        data.add_field("compressed", "true")
        data.add_field("user_id", self._user_id)
        data.add_field("device_id", self._device_id)

        headers = {
            "X-Upload-Token": self._upload_token,
            "X-Content-SHA256": item.sha256,
        }

        async with self._http_session.post(
            self._upload_endpoint, data=data, headers=headers,
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
            elif resp.status == 409:
                # 已存在，视为成功（幂等）
                return True
            else:
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
            except Exception as e:
                item.error = str(e)
                logger.debug(
                    "重试上传异常 (%d/%d): %s/%s — %s",
                    item.retry_count, item.max_retries,
                    item.session_id[:8], item.file_type, e,
                )

        return False

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

    async def _process_queue(self):
        """处理队列中的 pending 项"""
        if not self._server_healthy:
            return

        async with self._queue_lock:
            if not self._queue:
                return

            remaining = []
            for item in self._queue:
                if item.status == "failed":
                    remaining.append(item)  # 保留 failed 项供排查
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
    def _write_uploaded_marker(session_dir: Path, results: dict):
        """写入 .uploaded 标记文件，记录上传确认信息"""
        marker = session_dir / ".uploaded"
        marker.write_text(json.dumps({
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": results,
        }, ensure_ascii=False, indent=2))

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
