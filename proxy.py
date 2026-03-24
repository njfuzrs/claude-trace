#!/usr/bin/env python3
"""
claude-trace proxy.py — HTTP 代理服务器
通道 A：透明转发 Claude Code 的 API 请求，SSE Tee 模式采集完整轨迹数据

用法：
    python proxy.py --port 4000 --output ./trajectories
    ANTHROPIC_BASE_URL=http://localhost:4000 claude
"""

import argparse
import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web

# ─────────────────────────────────────────────
# 日志配置
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("claude-trace")


def _log_task_exception(task: asyncio.Task):
    """asyncio.create_task 异常回调，防止异常被静默吞没"""
    if not task.cancelled() and task.exception():
        logger.error("后台任务异常: %s", task.exception(), exc_info=task.exception())


def _log_future_exception(future: asyncio.Future):
    """run_in_executor 返回的 Future 异常回调"""
    if not future.cancelled() and future.exception():
        logger.error("线程池任务异常: %s", future.exception(), exc_info=future.exception())


# ─────────────────────────────────────────────
# 安全：请求头脱敏 + 路径校验
# ─────────────────────────────────────────────

SENSITIVE_HEADERS = {"x-api-key", "authorization", "proxy-authorization"}

# P0 #5: session_id 只允许字母数字和连字符，防止路径遍历
_SAFE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_headers_for_storage(headers: Dict[str, str]) -> Dict[str, str]:
    """对请求头中的敏感信息脱敏后再存储

    安全原则：raw JSON 文件中不应存储明文 API Key，防止轨迹数据
    意外泄露（上传 HuggingFace、分享给他人等）导致 API Key 被盗用。
    """
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            sanitized[k] = v[:10] + "***" if len(v) > 10 else "***"
        else:
            sanitized[k] = v
    return sanitized


def _sanitize_session_id(session_id: str) -> str:
    """P0 #5: 校验 session_id 防止路径遍历攻击

    如果 session_id 包含非法字符（如 ../），替换为安全的 UUID。
    """
    if session_id and _SAFE_SESSION_ID_RE.match(session_id):
        return session_id
    safe_id = str(uuid.uuid4())
    logger.warning("session_id 包含非法字符，已替换: %r → %s", session_id[:20], safe_id[:8])
    return safe_id


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _msg_hash(msg: Dict) -> str:
    """计算单条 message 的内容指纹（用于前缀一致性校验）"""
    key = f"{msg.get('role', '')}:{str(msg.get('content', ''))[:200]}"
    return hashlib.md5(key.encode()).hexdigest()


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class RequestResponsePair:
    timestamp: str
    request_body: Dict
    request_headers: Dict       # 脱敏后的请求头
    index: int = 0              # P0 #1: 在 record_request 时分配，避免并发冲突
    response_body: Optional[Dict] = None
    new_messages: List[Dict] = field(default_factory=list)
    model: str = ""
    usage: Dict = field(default_factory=dict)
    stop_reason: str = ""
    is_partial: bool = False    # SSE 流中断时标记


@dataclass
class Session:
    id: str
    model: str = ""
    source: str = ""            # startup / resume / clear
    cwd: str = ""
    start_time: str = field(default_factory=lambda: datetime.now().isoformat())
    last_activity: str = field(default_factory=lambda: datetime.now().isoformat())
    pairs: List[RequestResponsePair] = field(default_factory=list)
    # P0 #2: 只保留 hash 列表和 count，不保留完整 messages 引用，避免内存泄漏
    prev_msg_hashes: List[str] = field(default_factory=list)
    prev_msg_count: int = 0

    def update_activity(self):
        self.last_activity = datetime.now().isoformat()

    def is_continuation(self, messages: List[Dict]) -> bool:
        """对话内容连续性匹配（Hooks 未配置时的兜底）
        P2 #11: 使用独立的 _msg_hash 函数，不再耦合 DataCollector
        P1 #7: 增加前缀校验数量到 5 条，降低 compaction 误判概率
        """
        if not self.prev_msg_hashes or not messages:
            return False
        check = min(5, self.prev_msg_count, len(messages))
        return all(
            _msg_hash(messages[i]) == self.prev_msg_hashes[i]
            for i in range(check)
        )


# ─────────────────────────────────────────────
# 会话管理
# ─────────────────────────────────────────────

class SessionManager:
    def __init__(self, session_timeout: int = 300):
        self.active_sessions: Dict[str, Session] = {}
        # Hooks 通过 HTTP 回调注册的 pending 队列（等待首次 API 请求关联）
        self._pending_sessions: Dict[str, dict] = {}
        self._timeout = session_timeout

    def register_session_from_hook(self, session_id: str, metadata: dict):
        """由 /_internal/session-register 路由调用，Hooks 主动通知"""
        session_id = _sanitize_session_id(session_id)
        if session_id not in self.active_sessions:
            metadata["_registered_at"] = datetime.now().isoformat()
            self._pending_sessions[session_id] = metadata
            logger.info("Hook 注册会话: %s (model=%s)", session_id[:8], metadata.get("model", ""))

    def _create_session_from_pending(self, session_id: str, metadata: dict, match_type: str) -> Session:
        """从 pending 队列创建 Session 的公共方法"""
        self._pending_sessions.pop(session_id, None)
        session = Session(
            id=session_id,
            model=metadata.get("model", ""),
            source=metadata.get("source", ""),
            cwd=metadata.get("cwd", ""),
        )
        self.active_sessions[session_id] = session
        logger.info("会话关联（%s）: %s", match_type, session_id[:8])
        return session

    def match_session(self, request_body: Dict) -> Session:
        """双通道会话匹配"""

        # 策略 1：Hooks 注册的 pending 队列（确定性关联）
        if len(self._pending_sessions) == 1:
            session_id, metadata = next(iter(self._pending_sessions.items()))
            return self._create_session_from_pending(session_id, metadata, "Hook 单实例")

        # P1 #6: 多个 pending 时，用 model + cwd 联合匹配，避免同 model 误关联
        if self._pending_sessions:
            request_model = request_body.get("model", "")
            # 先尝试 model 精确匹配
            model_matches = [
                (sid, meta) for sid, meta in self._pending_sessions.items()
                if meta.get("model") == request_model
            ]
            if len(model_matches) == 1:
                sid, metadata = model_matches[0]
                return self._create_session_from_pending(sid, metadata, "Hook model 匹配")
            elif len(model_matches) > 1:
                # 多个同 model 的 pending，warn 并取第一个（FIFO）
                logger.warning(
                    "多个 pending session 使用相同 model=%s，按 FIFO 关联（可能不准确）",
                    request_model,
                )
                sid, metadata = model_matches[0]
                return self._create_session_from_pending(sid, metadata, "Hook FIFO 兜底")

        # 策略 2：对话内容连续性匹配（Hooks 未配置时的兜底）
        messages = request_body.get("messages", [])
        for session in self.active_sessions.values():
            if session.is_continuation(messages):
                session.update_activity()
                return session

        # 策略 3：创建新会话（自动生成 session_id）
        sid = str(uuid.uuid4())
        session = Session(id=sid, model=request_body.get("model", ""))
        self.active_sessions[sid] = session
        logger.info("新建会话（兜底）: %s", sid[:8])
        return session

    def cleanup_expired(self, collector: Optional["DataCollector"] = None):
        """清理超时会话和过期 pending，导出轨迹后再删除"""
        now = datetime.now()

        # 清理超时的活跃会话
        expired = []
        for sid, session in self.active_sessions.items():
            last = datetime.fromisoformat(session.last_activity)
            if (now - last).total_seconds() > self._timeout:
                expired.append(sid)
        for sid in expired:
            session = self.active_sessions[sid]
            # P1 #5: 超时清理前先导出 .traj，防止数据丢失
            if collector and session.pairs:
                collector.export_session(session)
            logger.info("会话超时清理: %s (pairs=%d)", sid[:8], len(session.pairs))
            del self.active_sessions[sid]

        # P1 #10: 清理过期的 pending sessions（Hook 注册了但始终没有 API 请求到达）
        stale_pending = [
            sid for sid, meta in self._pending_sessions.items()
            if (now - datetime.fromisoformat(meta.get("_registered_at", now.isoformat()))).total_seconds() > self._timeout
        ]
        for sid in stale_pending:
            logger.info("Pending 会话超时清理: %s", sid[:8])
            del self._pending_sessions[sid]


# ─────────────────────────────────────────────
# SSE 解析与重组
# ─────────────────────────────────────────────

def parse_sse_events(raw_data: bytes) -> List[Dict]:
    """解析原始 SSE 字节流为事件列表

    按空行（\\n\\n）分割事件块，符合 SSE 规范，能正确处理多行 data。
    """
    events = []
    text = raw_data.decode("utf-8", errors="replace")

    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue

        data_lines = []
        for line in block.split("\n"):
            if line.startswith("data: "):
                data_lines.append(line[6:])
            elif line.startswith("data:"):
                data_lines.append(line[5:])

        if not data_lines:
            continue

        data_str = "\n".join(data_lines)
        if data_str and data_str != "[DONE]":
            try:
                events.append(json.loads(data_str))
            except json.JSONDecodeError:
                pass  # 跳过无法解析的事件

    return events


def reassemble_sse_response(raw_data: bytes) -> Dict:
    """从 SSE 事件流重组完整的 API 响应

    处理所有 Anthropic SSE 事件类型：
    message_start / content_block_start / content_block_delta /
    content_block_stop / message_delta / message_stop
    """
    events = parse_sse_events(raw_data)

    message: Dict = {}
    content_blocks: List[Dict] = []
    current_block: Optional[Dict] = None
    has_message_stop = False

    for event in events:
        event_type = event.get("type")

        if event_type == "message_start":
            message = event.get("message", {})

        elif event_type == "content_block_start":
            current_block = dict(event.get("content_block", {}))
            current_block["_deltas"] = []

        elif event_type == "content_block_delta":
            if current_block is None:
                continue
            delta: Dict = event.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                current_block["_deltas"].append(delta.get("text", ""))
            elif delta_type == "thinking_delta":
                current_block["_deltas"].append(delta.get("thinking", ""))
            elif delta_type == "input_json_delta":
                current_block["_deltas"].append(delta.get("partial_json", ""))

        elif event_type == "content_block_stop":
            if current_block is not None:
                block_type = current_block.get("type")
                merged = "".join(current_block.pop("_deltas", []))

                if block_type == "text":
                    current_block["text"] = merged
                elif block_type == "thinking":
                    current_block["thinking"] = merged
                elif block_type == "tool_use":
                    # 容错：SSE 流中断时 merged 可能是不完整的 JSON
                    try:
                        current_block["input"] = json.loads(merged) if merged else {}
                    except json.JSONDecodeError:
                        current_block["input"] = {
                            "_raw_partial": merged,
                            "_parse_error": True,
                        }

                content_blocks.append(current_block)
                current_block = None

        elif event_type == "message_delta":
            message.update(event.get("delta", {}))
            if "usage" in event:
                message.setdefault("usage", {}).update(event["usage"])

        elif event_type == "message_stop":
            has_message_stop = True

    message["content"] = content_blocks
    message["_complete"] = has_message_stop  # 标记 SSE 流是否完整
    return message


# ─────────────────────────────────────────────
# 数据采集器
# ─────────────────────────────────────────────

class DataCollector:
    def __init__(self, output_dir: Path, save_raw: bool = True):
        self.output_dir = output_dir
        self.raw_dir = output_dir / "raw"
        self.traj_dir = output_dir / "traj"
        self.save_raw = save_raw  # P2 #18: 控制是否保存原始 JSON 文件
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.traj_dir.mkdir(parents=True, exist_ok=True)

    def record_request(
        self,
        session: Session,
        request_body: Dict,
        raw_headers: Dict[str, str],
    ) -> RequestResponsePair:
        """记录请求，提取增量 messages"""
        curr_messages = request_body.get("messages", [])
        new_messages = self._extract_incremental_messages(
            session.prev_msg_hashes, session.prev_msg_count, curr_messages,
        )

        # P0 #1: 在 append 之前分配序号。
        # 安全假设：record_request 是同步方法，aiohttp 单线程事件循环中
        # 两个 await 点之间不会被打断，因此无需加锁。
        # 如果未来改为 async，需要引入 asyncio.Lock 保护。
        idx = len(session.pairs) + 1

        pair = RequestResponsePair(
            timestamp=datetime.now().isoformat(),
            request_body=request_body,
            request_headers=sanitize_headers_for_storage(raw_headers),
            index=idx,
            new_messages=new_messages,
            model=request_body.get("model", ""),
        )
        session.pairs.append(pair)

        # P0 #2: 只保留 hash 列表和 count，释放完整 messages 引用
        session.prev_msg_hashes = [_msg_hash(m) for m in curr_messages]
        session.prev_msg_count = len(curr_messages)
        return pair

    async def record_response_async(self, session: Session, pair: RequestResponsePair, response: Dict):
        """异步记录重组后的响应，写入 raw/ 目录

        P0 #3: 所有文件 IO 移到线程池，避免 JSON 序列化 + 写入阻塞事件循环。
        """
        pair.response_body = response
        pair.usage = response.get("usage", {})
        pair.stop_reason = response.get("stop_reason", "")
        pair.is_partial = not response.get("_complete", False)

        idx = pair.index
        status = "⚠️ partial" if pair.is_partial else "✅"
        logger.info(
            "%s 记录 #%d | model=%s | stop=%s | tokens=%s",
            status, idx, pair.model, pair.stop_reason,
            pair.usage.get("output_tokens", "?"),
        )

        # 将所有同步文件 IO 移到线程池
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_pair_files, session, pair)

    async def record_partial_response(self, session: Session, pair: RequestResponsePair, raw_chunks: bytes):
        """SSE 流中断时保存部分数据"""
        partial_response = reassemble_sse_response(raw_chunks)
        partial_response["_complete"] = False
        await self.record_response_async(session, pair, partial_response)

    def export_session(self, session: Session):
        """导出会话的 .traj 文件（超时清理 / SessionEnd 时调用）"""
        if not session.pairs:
            return
        self._rebuild_traj(session)
        logger.info("轨迹已导出: %s | 步骤=%d", session.id[:8], len(session.pairs))

    def _rebuild_traj(self, session: Session):
        """重建并覆盖 .traj 文件

        P0 #1: 兼容事件循环内外两种调用场景。
        P0 #2: run_in_executor 返回的 Future 添加异常回调。
        """
        try:
            from builder import SessionMetadata, build_trajectory, save_trajectory
            metadata = SessionMetadata(
                session_id=session.id,
                start_time=session.start_time,
                model=session.model,
                working_directory=session.cwd,
                start_source=session.source,
            )
            traj = build_trajectory(session.id, session.pairs, metadata)
            traj_path = self.traj_dir / f"{session.id}.traj"
            try:
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(None, save_trajectory, traj_path, traj)
                future.add_done_callback(_log_future_exception)
            except RuntimeError:
                # 非事件循环上下文（如优雅退出的 finally 块），直接同步写入
                save_trajectory(traj_path, traj)
        except Exception as e:
            logger.warning("重建 .traj 失败: %s", e)

    def _write_pair_files(self, session: Session, pair: RequestResponsePair):
        """同步写入请求/响应文件 + JSONL + .traj（在线程池中执行）

        P0 #3: 所有文件 IO 集中在此方法，由 run_in_executor 调用，不阻塞事件循环。
        P2 #18: save_raw 控制是否写入单独的 JSON 文件。
        """
        session_dir = self.raw_dir / session.id
        session_dir.mkdir(parents=True, exist_ok=True)

        idx = pair.index

        # P2 #18: 仅在 save_raw=True 时写入单独的 JSON 文件
        if self.save_raw:
            req_file = session_dir / f"{idx:03d}_request.json"
            resp_file = session_dir / f"{idx:03d}_response.json"

            req_data = {
                "timestamp": pair.timestamp,
                "model": pair.model,
                "headers": pair.request_headers,  # 已脱敏
                "body": pair.request_body,
                "new_messages": pair.new_messages,
            }
            resp_data = {
                "timestamp": datetime.now().isoformat(),
                "usage": pair.usage,
                "stop_reason": pair.stop_reason,
                "is_partial": pair.is_partial,
                "body": pair.response_body,
            }

            req_file.write_text(json.dumps(req_data, ensure_ascii=False, indent=2))
            resp_file.write_text(json.dumps(resp_data, ensure_ascii=False, indent=2))

        # 追加写入 raw JSONL（双格式并行输出）
        jsonl_path = self.raw_dir / f"{session.id}.jsonl"
        self._append_raw_jsonl(jsonl_path, pair)

        # 增量重建 .traj 文件（直接同步写入，因为已在线程池中）
        try:
            from builder import SessionMetadata, build_trajectory, save_trajectory
            metadata = SessionMetadata(
                session_id=session.id,
                start_time=session.start_time,
                model=session.model,
                working_directory=session.cwd,
                start_source=session.source,
            )
            traj = build_trajectory(session.id, session.pairs, metadata)
            traj_path = self.traj_dir / f"{session.id}.traj"
            save_trajectory(traj_path, traj)
        except Exception as e:
            logger.warning("重建 .traj 失败: %s", e)

    @staticmethod
    def _append_raw_jsonl(jsonl_path: Path, pair: RequestResponsePair):
        """追加写入原始 JSONL"""
        record = {
            "timestamp": pair.timestamp,
            "model": pair.model,
            "request": pair.request_body,
            "response": pair.response_body,
            "usage": pair.usage,
            "stop_reason": pair.stop_reason,
            "is_partial": pair.is_partial,
        }
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _extract_incremental_messages(
        prev_hashes: List[str], prev_count: int, curr_messages: List,
    ) -> List[Dict]:
        """提取增量 messages — Claude Code 每次请求都重发完整历史

        P0 #2: 使用 hash 列表而非完整 messages 引用进行比较。
        正确处理 context compaction 场景。
        """
        if not prev_hashes:
            return curr_messages

        # compaction 或重置：当前 messages 数量 <= 上次
        if len(curr_messages) <= prev_count:
            return curr_messages

        # P1 #7: 前缀指纹校验，检查前 5 条降低 compaction 误判概率
        check_count = min(5, prev_count)
        prefix_match = all(
            _msg_hash(curr_messages[i]) == prev_hashes[i]
            for i in range(check_count)
        )

        if prefix_match:
            return curr_messages[prev_count:]
        else:
            # 前缀不匹配（compaction 后重新填充），记录完整历史
            return curr_messages


# ─────────────────────────────────────────────
# 响应头过滤
# ─────────────────────────────────────────────

HOP_BY_HOP_REQUEST = {
    "host", "content-length", "connection",
    "keep-alive", "transfer-encoding", "upgrade",
}

HOP_BY_HOP_RESPONSE = {
    "connection", "keep-alive", "transfer-encoding",
    "upgrade", "content-encoding",  # aiohttp 自动解压，不透传
    "content-length",  # P2 #16: aiohttp 自动解压后 content-length 与实际 body 不匹配
}


def _filter_response_headers(headers) -> Dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_RESPONSE}


# ─────────────────────────────────────────────
# 代理处理器
# ─────────────────────────────────────────────

async def handle_streaming(
    request: web.Request,
    upstream_resp: aiohttp.ClientResponse,
    session: Session,
    pair: RequestResponsePair,
    collector: DataCollector,
) -> web.StreamResponse:
    """SSE Tee 模式：逐 chunk 立即转发 + 后台收集

    P0 #4: write_eof 只在正常路径调用一次，异常路径在 except 中处理，
    避免 finally 中重复调用导致的不确定行为。
    """

    response = web.StreamResponse(
        status=upstream_resp.status,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        },
    )
    await response.prepare(request)

    raw_chunks: List[bytes] = []
    stream_interrupted = False
    try:
        async for chunk in upstream_resp.content.iter_any():
            raw_chunks.append(chunk)
            await response.write(chunk)  # 立即转发，零延迟
    except (aiohttp.ClientPayloadError, ConnectionResetError) as e:
        stream_interrupted = True
        logger.warning("SSE 流中断: %s，保存部分数据", e)
        if raw_chunks:
            t = asyncio.create_task(
                collector.record_partial_response(session, pair, b"".join(raw_chunks))
            )
            t.add_done_callback(_log_task_exception)

    # write_eof 只调用一次（无论正常结束还是中断）
    try:
        await response.write_eof()
    except Exception:
        pass

    if stream_interrupted:
        return response

    # 流正常结束：重组 SSE 事件 → 异步记录
    full_data = b"".join(raw_chunks)
    complete_response = reassemble_sse_response(full_data)
    t = asyncio.create_task(
        collector.record_response_async(session, pair, complete_response)
    )
    t.add_done_callback(_log_task_exception)

    return response


def _is_messages_request(method: str, path: str, request_body: Dict) -> bool:
    """判断是否为需要采集的 messages API 请求"""
    return method == "POST" and "/v1/messages" in path and "messages" in request_body


async def _passthrough_upstream(
    request: web.Request, upstream_base: str, body: bytes,
) -> web.StreamResponse:
    """直接透传非 messages 请求（GET /v1/models 等），不做会话匹配和数据采集"""
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_REQUEST
    }
    upstream_url = f"{upstream_base}{request.path}"
    if request.query_string:
        upstream_url += f"?{request.query_string}"

    # P0 #4: GET 请求不带 body
    data = body if request.method in ("POST", "PUT", "PATCH") else None

    client: aiohttp.ClientSession = request.app["upstream_session"]
    try:
        async with client.request(
            request.method, upstream_url, headers=headers, data=data,
        ) as upstream_resp:
            resp_body = await upstream_resp.read()
            return web.Response(
                status=upstream_resp.status,
                headers=_filter_response_headers(upstream_resp.headers),
                body=resp_body,
            )
    except aiohttp.ClientError as e:
        return web.Response(status=502, text=f"Upstream connection error: {e}")
    except asyncio.TimeoutError:
        return web.Response(status=504, text="Upstream timeout")


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """代理主处理器 — 支持流式和非流式两种模式"""

    session_manager: SessionManager = request.app["session_manager"]
    collector: DataCollector = request.app["collector"]
    upstream_base: str = request.app["upstream_base"]

    # 1. 读取请求体
    body: bytes = b""
    try:
        body = await request.read()
        request_body = json.loads(body) if body else {}
    except json.JSONDecodeError:
        request_body = {}

    # P0 #3: 非 messages 请求直接透传，不做会话匹配和数据采集
    if not _is_messages_request(request.method, request.path, request_body):
        return await _passthrough_upstream(request, upstream_base, body)

    is_streaming = request_body.get("stream", False)

    # 2. 会话匹配 + 记录请求
    session = session_manager.match_session(request_body)
    raw_headers = dict(request.headers)
    pair = collector.record_request(session, request_body, raw_headers)

    # 3. 构建上游请求头（透传原始 headers，去掉 hop-by-hop）
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_REQUEST
    }

    # 4. 构建上游 URL
    upstream_url = f"{upstream_base}{request.path}"
    if request.query_string:
        upstream_url += f"?{request.query_string}"

    # 5. 转发到上游（使用全局复用的 ClientSession）
    client: aiohttp.ClientSession = request.app["upstream_session"]
    try:
        async with client.request(
            request.method,
            upstream_url,
            headers=headers,
            data=body,
        ) as upstream_resp:

            if is_streaming:
                return await handle_streaming(request, upstream_resp, session, pair, collector)
            else:
                resp_body = await upstream_resp.read()
                try:
                    response_json = json.loads(resp_body)
                except json.JSONDecodeError:
                    response_json = {"_raw": resp_body.decode("utf-8", errors="replace")}

                t = asyncio.create_task(
                    collector.record_response_async(session, pair, response_json)
                )
                t.add_done_callback(_log_task_exception)

                return web.Response(
                    status=upstream_resp.status,
                    headers=_filter_response_headers(upstream_resp.headers),
                    body=resp_body,
                )

    except aiohttp.ClientError as e:
        logger.error("上游连接失败: %s", e)
        return web.Response(status=502, text=f"Upstream connection error: {e}")
    except asyncio.TimeoutError:
        logger.error("上游请求超时")
        return web.Response(status=504, text="Upstream timeout")
    except Exception as e:
        logger.exception("代理内部错误")
        return web.Response(status=500, text=f"Proxy error: {e}")


# ─────────────────────────────────────────────
# 内部路由：Hooks 回调
# ─────────────────────────────────────────────

async def handle_session_register(request: web.Request) -> web.Response:
    """接收 Hooks 的 SessionStart 注册通知"""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    session_id = data.get("session_id")
    if not session_id:
        return web.Response(status=400, text="missing session_id")

    session_manager: SessionManager = request.app["session_manager"]
    session_manager.register_session_from_hook(session_id, data)
    return web.Response(status=200, text="ok")


async def handle_session_event(request: web.Request) -> web.Response:
    """接收 Hooks 的其他会话事件（SessionEnd 等）"""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    session_id = data.get("session_id")
    event = data.get("event")
    logger.info("Hook 事件: %s session=%s", event, (session_id or "")[:8])

    # P1 #7: SessionEnd 触发轨迹导出
    if event == "end" and session_id:
        session_manager: SessionManager = request.app["session_manager"]
        collector: DataCollector = request.app["collector"]
        if session_id in session_manager.active_sessions:
            session = session_manager.active_sessions[session_id]
            collector.export_session(session)
            logger.info(
                "会话结束: %s | API 调用=%d 次",
                session_id[:8], len(session.pairs),
            )
            del session_manager.active_sessions[session_id]

    return web.Response(status=200, text="ok")


# ─────────────────────────────────────────────
# 应用工厂
# ─────────────────────────────────────────────

async def create_app(
    upstream_base: str,
    output_dir: Path,
    session_timeout: int,
    save_raw: bool = True,
) -> web.Application:
    app = web.Application()

    # 共享状态
    app["upstream_base"] = upstream_base.rstrip("/")
    app["session_manager"] = SessionManager(session_timeout=session_timeout)
    app["collector"] = DataCollector(output_dir, save_raw=save_raw)

    # 应用启动时创建全局 ClientSession（连接池复用）
    async def on_startup(app):
        app["upstream_session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, sock_read=300),
        )
        logger.info("ClientSession 已创建（连接池复用）")

    # 应用关闭时销毁 ClientSession
    async def on_cleanup(app):
        await app["upstream_session"].close()
        logger.info("ClientSession 已关闭")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # 内部路由（优先注册，避免被通配符覆盖）
    app.router.add_post("/_internal/session-register", handle_session_register)
    app.router.add_post("/_internal/session-event", handle_session_event)

    # 通配符路由：透传所有请求
    app.router.add_route("*", "/{path:.*}", proxy_handler)

    return app


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="claude-trace — Claude Code HTTP 代理 + 轨迹采集",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=4000, help="代理监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，防止局域网访问）")
    parser.add_argument("--output", default="./trajectories", help="轨迹数据输出目录")
    parser.add_argument(
        "--upstream",
        default="https://api.anthropic.com",
        help="上游 API 地址",
    )
    parser.add_argument("--session-timeout", type=int, default=300, help="会话超时时间（秒）")
    parser.add_argument("--save-raw", action="store_true", default=True, help="保存原始请求/响应 JSON 文件")
    parser.add_argument("--no-save-raw", dest="save_raw", action="store_false", help="不保存原始请求/响应 JSON 文件（只保留 JSONL + .traj）")
    parser.add_argument("--verbose", action="store_true", help="详细日志输出")
    return parser.parse_args()


async def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    app = await create_app(
        upstream_base=args.upstream,
        output_dir=output_dir,
        session_timeout=args.session_timeout,
        save_raw=args.save_raw,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    logger.info("=" * 50)
    logger.info("claude-trace 代理已启动")
    logger.info("监听地址: http://%s:%d", args.host, args.port)
    logger.info("上游 API:  %s", args.upstream)
    logger.info("输出目录:  %s", output_dir.resolve())
    logger.info("=" * 50)
    logger.info("启动 Claude Code：")
    logger.info("  ANTHROPIC_BASE_URL=http://%s:%d claude", args.host, args.port)
    logger.info("=" * 50)

    # 定期清理超时会话
    session_manager: SessionManager = app["session_manager"]
    collector: DataCollector = app["collector"]

    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            session_manager.cleanup_expired(collector=collector)

    cleanup_task = asyncio.create_task(cleanup_loop())
    cleanup_task.add_done_callback(_log_task_exception)

    try:
        # 等待直到 Ctrl+C
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        cleanup_task.cancel()
        # 优雅退出：导出所有活跃会话的轨迹
        for _sid, session in list(session_manager.active_sessions.items()):
            if session.pairs:
                collector.export_session(session)
        await runner.cleanup()
        logger.info("代理已停止，数据保存在: %s", output_dir.resolve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
