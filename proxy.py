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
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web

from builder import SessionMetadata, build_trajectory, save_trajectory
from uploader import UploadManager

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


async def _drain_pending_writes(session: "Session"):
    """等待会话中所有 pending 的写入任务完成，避免导出时读到半成品 pair。"""
    if not session._pending_write_tasks:
        return

    pending = [task for task in session._pending_write_tasks if not task.done()]
    session._pending_write_tasks = []
    if not pending:
        return

    results = await asyncio.gather(*pending, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.warning("等待写入任务完成时发生异常: %s", result)


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


def _normalize_model(model: str) -> str:
    """标准化模型名称，去掉 context window 后缀和日期后缀，用于模糊匹配。

    Hook 注册的 model 可能带 context window 后缀（如 'claude-opus-4-6[1m]'），
    API 请求的 model 可能带日期后缀（如 'claude-haiku-4-5-20251001'）。
    标准化为基础名称以支持匹配。

    示例：
      'claude-opus-4-6[1m]'       → 'claude-opus-4-6'
      'claude-opus-4-6'           → 'claude-opus-4-6'
      'claude-haiku-4-5-20251001' → 'claude-haiku-4-5'
      'claude-sonnet-4-6'         → 'claude-sonnet-4-6'
    """
    if not model:
        return ""
    # 去掉 [...] 后缀（context window 标记）
    base = model.split("[")[0]
    # 去掉日期后缀（-YYYYMMDD 格式）
    base = re.sub(r"-\d{8}$", "", base)
    return base


def _models_match(model_a: str, model_b: str) -> bool:
    """判断两个模型名是否指向同一个模型系列。

    支持 Hook 注册的 'claude-opus-4-6[1m]' 与 API 请求的 'claude-opus-4-6' 匹配，
    以及 'claude-haiku-4-5-20251001' 与 'claude-haiku-4-5' 匹配。
    """
    if not model_a or not model_b:
        return False
    return _normalize_model(model_a) == _normalize_model(model_b)


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
    # 子会话（sub-agent 产生的独立 API 请求流）
    child_sessions: List["Session"] = field(default_factory=list)
    parent_session_id: Optional[str] = None  # 如果是子会话，指向父会话 id
    is_subagent: bool = False
    is_title_generation: bool = False  # 标题生成请求（haiku 单条 message，不含有价值的轨迹数据）
    # 修复 exit_status 竞态：跟踪 pending 的 record_response_async tasks
    _pending_write_tasks: List[asyncio.Task] = field(default_factory=list)

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
    # pending session 超时时间：用户可能花很长时间阅读/思考才提交第一个 prompt，
    # 所以 pending 超时要远大于活跃会话超时。默认 30 分钟。
    PENDING_TIMEOUT = 1800  # 30 minutes

    def __init__(self, session_timeout: int = 300):
        self.active_sessions: Dict[str, Session] = {}
        # Hooks 通过 HTTP 回调注册的 pending 队列（等待首次 API 请求关联）
        self._pending_sessions: Dict[str, dict] = {}
        self._timeout = session_timeout
        # sub-agent session_id → parent session_id 映射
        self._subagent_parent_map: Dict[str, str] = {}
        # 正在导出中的 session_id 集合，防止 cleanup_expired 和 handle_session_event 竞态
        self._exporting_sessions: set = set()

    def register_session_from_hook(self, session_id: str, metadata: dict):
        """由 /_internal/session-register 路由调用，Hooks 主动通知

        支持 resume 场景：如果 session 已在 active_sessions 中（用户快速 resume），
        更新 source 字段但不重复注册 pending。
        """
        session_id = _sanitize_session_id(session_id)
        source = metadata.get("source", "")

        # 已有活跃 session（resume 场景）：更新 source，不注册 pending
        if session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            if source:
                session.source = source
            logger.info("Hook 更新已有会话: %s (source=%s)", session_id[:8], source)
            return

        # 已在 pending 中（重复注册）：更新 metadata
        if session_id in self._pending_sessions:
            self._pending_sessions[session_id].update(metadata)
            self._pending_sessions[session_id]["_registered_at"] = datetime.now().isoformat()
            logger.info("Hook 更新 pending 会话: %s (source=%s)", session_id[:8], source)
            return

        metadata["_registered_at"] = datetime.now().isoformat()
        self._pending_sessions[session_id] = metadata
        logger.info("Hook 注册会话: %s (model=%s, source=%s)", session_id[:8], metadata.get("model", ""), source)

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

    @staticmethod
    def _extract_cwd_from_request(request_body: Dict) -> str:
        """从请求的 system prompt 中尝试提取工作目录信息

        Claude Code 的 system prompt 通常包含 'working directory' 或 'cwd' 等信息，
        可用于多实例场景下辅助区分不同的 Claude Code 实例。
        """
        system = request_body.get("system")
        if not system:
            return ""
        if isinstance(system, list):
            text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        else:
            text = str(system)
        # 常见模式：Primary working directory: /path/to/dir
        import re as _re
        match = _re.search(r"(?:working directory|cwd)[:\s]+(/\S+)", text, _re.IGNORECASE)
        return match.group(1) if match else ""

    def _find_parent_session(self) -> Optional[Session]:
        """查找当前活跃的主会话（用于 sub-agent 请求关联）

        优先选择由 Hook 注册创建的会话（有 source 字段），因为这是真正的
        Claude Code 主会话。
        """
        # 优先：由 Hook 注册创建的会话（source 非空）
        hook_sessions = [
            s for s in self.active_sessions.values()
            if not s.is_subagent and s.source
        ]
        if len(hook_sessions) == 1:
            return hook_sessions[0]

        # 兜底：唯一的非 sub-agent 会话
        main_sessions = [
            s for s in self.active_sessions.values()
            if not s.is_subagent
        ]
        if len(main_sessions) == 1:
            return main_sessions[0]

        return None

    def match_session(self, request_body: Dict) -> Session:
        """双通道会话匹配

        策略优先级：
        1. Hooks 注册的 pending 队列（model 匹配 → 确定性关联）
        2. 已有活跃会话的对话内容连续性匹配（同 model 优先）
        3. Sub-agent 路由：model 不匹配时，关联到唯一的活跃主会话作为子会话
        4. 兜底：创建新会话
        """
        request_model = request_body.get("model", "")

        # 策略 1：Hooks 注册的 pending 队列（model 匹配关联）
        if self._pending_sessions:
            request_cwd = self._extract_cwd_from_request(request_body)

            model_matches = [
                (sid, meta) for sid, meta in self._pending_sessions.items()
                if _models_match(meta.get("model", ""), request_model)
            ]
            if len(model_matches) == 1:
                sid, metadata = model_matches[0]
                return self._create_session_from_pending(sid, metadata, "Hook model 匹配")
            elif len(model_matches) > 1:
                if request_cwd:
                    cwd_matches = [
                        (sid, meta) for sid, meta in model_matches
                        if meta.get("cwd") == request_cwd
                    ]
                    if len(cwd_matches) == 1:
                        sid, metadata = cwd_matches[0]
                        return self._create_session_from_pending(sid, metadata, "Hook model+cwd 匹配")

                logger.warning(
                    "多个 pending session 使用相同 model=%s，按 FIFO 关联",
                    request_model,
                )
                sid, metadata = model_matches[0]
                return self._create_session_from_pending(sid, metadata, "Hook FIFO 兜底")

            # model 不匹配任何 pending（可能是 sub-agent 或标题生成请求）
            logger.debug(
                "请求 model=%s 不匹配任何 pending session，跳过 pending 队列",
                request_model,
            )

        # 策略 2：已有活跃会话的对话内容连续性匹配
        messages = request_body.get("messages", [])
        # 优先匹配 model 一致的 session
        for session in self.active_sessions.values():
            if _models_match(session.model, request_model) and session.is_continuation(messages):
                session.update_activity()
                return session
        # model 不一致也尝试匹配（兼容 Hooks 未配置时的场景）
        for session in self.active_sessions.values():
            if session.is_continuation(messages):
                session.update_activity()
                return session

        # 策略 2.5：Compaction 兜底 — compaction 后 messages 完全重写，
        # is_continuation 会失败。如果请求带 system prompt（主会话特征）且
        # 有唯一的同 model、由 Hook 注册的活跃主会话，直接关联（重置 hash）。
        # 限制为 Hook 注册的会话（有 source），避免误匹配标题生成等兜底创建的会话。
        has_system = bool(request_body.get("system"))
        if has_system and messages:
            same_model_hook_mains = [
                s for s in self.active_sessions.values()
                if not s.is_subagent and s.source
                and _models_match(s.model, request_model)
            ]
            if len(same_model_hook_mains) == 1:
                session = same_model_hook_mains[0]
                session.update_activity()
                logger.info(
                    "Compaction 兜底匹配: %s (model=%s, msgs=%d→%d)",
                    session.id[:8], request_model,
                    session.prev_msg_count, len(messages),
                )
                return session

        # 策略 3：Sub-agent 路由 — 如果有唯一的活跃主会话，将不匹配的请求
        # 作为子会话关联到它。Claude Code 的 sub-agent 使用不同 model（如 haiku），
        # 标题生成也用 haiku，这些请求不应创建独立的 traj 文件。
        parent = self._find_parent_session()
        if parent is not None:
            # 同 model 的主会话请求（代理重启后 is_continuation 失败的恢复场景）：
            # 如果请求带 system prompt + 多条 messages + model 匹配 parent，
            # 说明这是同一个 Claude Code 会话的延续，直接关联而非创建子会话。
            if (has_system and len(messages) > 1
                    and _models_match(parent.model, request_model)):
                parent.update_activity()
                logger.info(
                    "主会话恢复关联: %s (model=%s, msgs=%d→%d)",
                    parent.id[:8], request_model,
                    parent.prev_msg_count, len(messages),
                )
                return parent

            # 识别标题生成请求：haiku model + 单条 message + 有 system prompt
            # 标题生成的 system prompt 通常很短（<500 字符），且 messages 只有 1 条
            is_title_gen = (
                len(messages) == 1
                and has_system
                and "haiku" in request_model.lower()
            )
            child_sid = str(uuid.uuid4())
            child = Session(
                id=child_sid,
                model=request_model,
                parent_session_id=parent.id,
                is_subagent=True,
                is_title_generation=is_title_gen,
            )
            self.active_sessions[child_sid] = child
            parent.child_sessions.append(child)
            self._subagent_parent_map[child_sid] = parent.id
            parent.update_activity()
            label = "标题生成" if is_title_gen else "Sub-agent"
            logger.info(
                "%s 会话: %s → parent %s (model=%s)",
                label, child_sid[:8], parent.id[:8], request_model,
            )
            return child

        # 策略 4：创建新会话（自动生成 session_id）
        # 识别标题生成请求：haiku + 单条 message，即使没有 parent 也不应产生独立 traj
        is_title_gen = (
            len(messages) == 1
            and has_system
            and "haiku" in request_model.lower()
        )
        if is_title_gen:
            # 标题生成请求没有找到 parent — 可能主会话还没创建或已清理。
            # 尝试关联到 pending 中的会话（Hook 已注册但首次 API 请求还没到）。
            pending_parents = [
                (sid, meta) for sid, meta in self._pending_sessions.items()
            ]
            if pending_parents:
                parent_sid, parent_meta = pending_parents[0]
                parent = self._create_session_from_pending(parent_sid, parent_meta, "标题生成提前关联")
                child_sid = str(uuid.uuid4())
                child = Session(
                    id=child_sid,
                    model=request_model,
                    parent_session_id=parent.id,
                    is_subagent=True,
                    is_title_generation=True,
                )
                self.active_sessions[child_sid] = child
                parent.child_sessions.append(child)
                self._subagent_parent_map[child_sid] = parent.id
                logger.info("标题生成提前关联: %s → parent %s", child_sid[:8], parent.id[:8])
                return child
            # 没有 pending 也没有 parent — 标记为标题生成的独立会话，
            # 后续 DataCollector 会跳过导出
            sid = str(uuid.uuid4())
            session = Session(id=sid, model=request_body.get("model", ""),
                              is_title_generation=True)
            self.active_sessions[sid] = session
            logger.info("标题生成（孤立）: %s model=%s", sid[:8], request_model)
            return session

        sid = str(uuid.uuid4())
        session = Session(id=sid, model=request_body.get("model", ""))
        self.active_sessions[sid] = session
        logger.info("新建会话（兜底）: %s model=%s", sid[:8], request_model)
        return session

    def get_parent_session(self, session: Session) -> Optional[Session]:
        """获取子会话的父会话"""
        if session.parent_session_id:
            return self.active_sessions.get(session.parent_session_id)
        return None

    def _remove_session_and_children(self, session_id: str):
        """原子性地从 active_sessions 中移除 session 及其所有子会话"""
        session = self.active_sessions.pop(session_id, None)
        if session:
            for child in session.child_sessions:
                self.active_sessions.pop(child.id, None)
                self._subagent_parent_map.pop(child.id, None)
        self._exporting_sessions.discard(session_id)
        return session

    async def cleanup_expired(self, collector: Optional["DataCollector"] = None):
        """清理超时会话和过期 pending，导出轨迹后再删除"""
        now = datetime.now()

        # 清理超时的活跃会话（子会话跟随父会话一起清理）
        expired = []
        for sid, session in self.active_sessions.items():
            if session.is_subagent:
                continue
            # 跳过正在被 handle_session_event 导出的 session
            if sid in self._exporting_sessions:
                continue
            try:
                last = datetime.fromisoformat(session.last_activity)
                if (now - last).total_seconds() > self._timeout:
                    expired.append(sid)
            except (ValueError, TypeError) as e:
                logger.warning("会话 %s 时间解析失败，标记为过期: %s", sid[:8], e)
                expired.append(sid)
        for sid in expired:
            # 二次检查：在 await 之间可能已被 handle_session_event 移除
            if sid not in self.active_sessions:
                continue
            try:
                session = self.active_sessions[sid]
                self._exporting_sessions.add(sid)
                if collector and (session.pairs or session.child_sessions):
                    # 修复 exit_status 竞态：等待 pending 写入完成
                    await _drain_pending_writes(session)
                    await collector.export_session_async(session, is_final=True)
                logger.info("会话超时清理: %s (pairs=%d, children=%d)",
                            sid[:8], len(session.pairs), len(session.child_sessions))
            except Exception as e:
                logger.warning("会话 %s 清理时异常: %s", sid[:8], e)
            self._remove_session_and_children(sid)

        # 清理过期的 pending sessions
        # 使用 PENDING_TIMEOUT（30 分钟）而非 session_timeout（5 分钟）
        stale_pending = []
        for sid, meta in self._pending_sessions.items():
            try:
                registered_at = datetime.fromisoformat(meta.get("_registered_at", now.isoformat()))
                if (now - registered_at).total_seconds() > self.PENDING_TIMEOUT:
                    stale_pending.append(sid)
            except (ValueError, TypeError):
                stale_pending.append(sid)
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
    def __init__(self, output_dir: Path, save_raw: bool = False, events_dir: Optional[Path] = None):
        self.output_dir = output_dir
        self.sessions_dir = output_dir / "sessions"
        self.save_raw = save_raw  # P2 #18: 控制是否保存原始 JSON 文件到 raw/ 子目录
        self.events_dir = events_dir or (Path.home() / ".claude" / "trajectory_events")
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        # 可靠上传管理器（替代原有的 fire-and-forget 上传）
        upload_url = os.environ.get("TRAJ_PLATFORM_URL", "").strip()
        upload_token = os.environ.get("TRAJ_UPLOAD_TOKEN", "").strip()
        if upload_url and upload_token:
            cleanup_env = os.environ.get("TRAJ_CLEANUP_AFTER_UPLOAD", "true").strip().lower()
            self._uploader: Optional[UploadManager] = UploadManager(
                upload_url=upload_url,
                upload_token=upload_token,
                cleanup_after_upload=(cleanup_env != "false"),
            )
            logger.info("可靠上传已启用: %s", upload_url)
        else:
            self._uploader = None

    def _session_dir(self, session_id: str) -> Path:
        """获取会话目录，按需创建"""
        d = self.sessions_dir / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

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

        # Fix 2: Hook 注册时 model 可能为 None（Claude Code SessionStart 不传 model），
        # 从首次 API 请求的 request_body 中补全
        if not session.model and request_body.get("model"):
            session.model = request_body["model"]
            logger.info("从 API 请求补全 model: %s (session=%s)", session.model, session.id[:8])

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

    async def record_response_async(
        self,
        session: Session,
        pair: RequestResponsePair,
        response: Dict,
        pairs_snapshot: Optional[List[RequestResponsePair]] = None,
    ):
        """异步记录重组后的响应，写入 raw/ 目录

        P0 #3: 所有文件 IO 移到线程池，避免 JSON 序列化 + 写入阻塞事件循环。
        P0 #4 fix: pairs_snapshot 应由调用方在 create_task 之前创建并传入，
        确保快照时机在事件循环的同步上下文中，而非 task 被调度执行时。
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
        # 如果调用方未传入 snapshot，在此处兜底创建（直接 await 场景）
        if pairs_snapshot is None:
            pairs_snapshot = list(session.pairs)
        # 子会话快照也在事件循环线程中创建（线程安全）
        children_snapshot = self._snapshot_children(session) if not session.is_subagent else None
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._write_pair_files, session, pair, pairs_snapshot, children_snapshot
        )

    async def record_partial_response(self, session: Session, pair: RequestResponsePair, raw_chunks: bytes):
        """SSE 流中断时保存部分数据"""
        partial_response = reassemble_sse_response(raw_chunks)
        partial_response["_complete"] = False
        await self.record_response_async(session, pair, partial_response)

    def _build_traj_data(
        self,
        session: Session,
        pairs_snapshot: List[RequestResponsePair],
        children_snapshot: Optional[List[tuple]] = None,
    ) -> tuple:
        """构建 traj 数据，合并子会话数据，读取 hook events 丰富 metadata。

        线程安全：所有可变数据（pairs、child_sessions）必须在事件循环线程中
        创建快照后传入，不在此方法中直接访问 session 的可变字段。

        Args:
            session: 主会话（只读取不可变字段：id/model/cwd/source/start_time）
            pairs_snapshot: 主会话 pairs 的快照
            children_snapshot: 子会话快照列表，每项为 (child_id, child_model, child_start_time, child_pairs_snapshot)
                               如果为 None 则不合并子会话

        Returns:
            (traj_path, traj_dict) 或在异常时返回 (None, None)
        """
        try:
            metadata = SessionMetadata(
                session_id=session.id,
                start_time=session.start_time,
                model=session.model,
                working_directory=session.cwd,
                start_source=session.source,
            )

            # 从 hook events JSONL 读取语义事件，丰富 metadata
            events_file = self.events_dir / f"{session.id}.jsonl"
            if events_file.exists():
                try:
                    hook_events = []
                    for line in events_file.read_text().splitlines():
                        if line.strip():
                            try:
                                hook_events.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                    metadata.user_prompts = [
                        e["prompt"] for e in hook_events
                        if e.get("event") == "UserPromptSubmit" and "prompt" in e
                    ]
                    metadata.compactions = [
                        e for e in hook_events if e.get("event") == "PostCompact"
                    ]
                    metadata.subagent_spans = [
                        e for e in hook_events
                        if e.get("event") in ("SubagentStart", "SubagentStop")
                    ]
                    metadata.has_sub_agent = len(metadata.subagent_spans) > 0
                    session_end = next(
                        (e for e in hook_events if e.get("event") == "SessionEnd"), {}
                    )
                    metadata.end_source = session_end.get("source", "")
                except Exception as e:
                    logger.debug("读取 hook events 失败: %s", e)

            # 构建主会话轨迹
            traj = build_trajectory(session.id, pairs_snapshot, metadata)

            # 合并子会话（sub-agent）的轨迹数据 — 跳过标题生成
            if children_snapshot:
                for child_id, child_model, child_start_time, child_pairs, is_title_gen in children_snapshot:
                    if not child_pairs:
                        continue
                    # 标题生成请求不合并到主轨迹（无训练价值），只计入 token 统计
                    child_meta = SessionMetadata(
                        session_id=child_id,
                        start_time=child_start_time,
                        model=child_model,
                    )
                    child_traj = build_trajectory(child_id, child_pairs, child_meta)

                    child_stats = child_traj.get("info", {}).get("model_stats", {})
                    main_stats = traj["info"]["model_stats"]
                    main_stats["tokens_sent"] += child_stats.get("tokens_sent", 0)
                    main_stats["tokens_received"] += child_stats.get("tokens_received", 0)
                    main_stats["cache_read_tokens"] += child_stats.get("cache_read_tokens", 0)
                    main_stats["cache_creation_tokens"] += child_stats.get("cache_creation_tokens", 0)
                    main_stats["api_calls"] += child_stats.get("api_calls", 0)
                    main_stats["total_cost_usd"] += child_stats.get("total_cost_usd", 0)

                    if is_title_gen:
                        # 标题生成只计入统计，不合并轨迹步骤
                        continue

                    for step in child_traj.get("trajectory", []):
                        step["agent"] = f"subagent:{child_id[:8]}"
                        step["_subagent_model"] = child_model
                    traj["trajectory"].extend(child_traj.get("trajectory", []))

                    for entry in child_traj.get("history", []):
                        entry["agent"] = f"subagent:{child_id[:8]}"
                    traj["history"].extend(child_traj.get("history", []))

                traj["metadata"]["total_steps"] = len(traj["trajectory"])
                traj["metadata"]["total_api_calls"] = traj["info"]["model_stats"]["api_calls"]
                traj["metadata"]["total_tokens_sent"] = traj["info"]["model_stats"]["tokens_sent"]
                traj["metadata"]["total_tokens_received"] = traj["info"]["model_stats"]["tokens_received"]
                traj["metadata"]["total_cost_usd"] = traj["info"]["model_stats"]["total_cost_usd"]
                traj["metadata"]["has_sub_agent"] = True
                traj["metadata"]["child_sessions"] = [
                    {"id": cid, "model": cmodel, "pairs": len(cpairs), "is_title_gen": ctitle}
                    for cid, cmodel, _, cpairs, ctitle in children_snapshot if cpairs
                ]

            traj_path = self._session_dir(session.id) / "session.traj"
            return traj_path, traj
        except Exception as e:
            logger.warning("构建 .traj 失败: %s", e)
            return None, None

    @staticmethod
    def _snapshot_children(session: Session) -> Optional[List[tuple]]:
        """在事件循环线程中创建子会话的不可变快照（线程安全）

        Returns:
            [(child_id, child_model, child_start_time, child_pairs_snapshot, is_title_gen), ...] 或 None
        """
        if not session.child_sessions:
            return None
        return [
            (child.id, child.model, child.start_time, list(child.pairs), child.is_title_generation)
            for child in session.child_sessions
        ]

    def export_session(self, session: Session):
        """导出会话的 .traj 文件（同步版本，用于优雅退出等非 async 上下文）

        子会话不单独导出 traj，它们的数据会在父会话导出时合并。
        标题生成的孤立会话（没有 parent）也跳过导出。
        """
        if session.is_subagent:
            return
        # 跳过标题生成的孤立会话（没有 parent 的标题生成请求）
        if session.is_title_generation:
            return
        if not session.pairs and not any(c.pairs for c in session.child_sessions):
            return
        # 在事件循环线程中创建所有快照（线程安全）
        pairs_snapshot = list(session.pairs)
        children_snapshot = self._snapshot_children(session)
        traj_path, traj = self._build_traj_data(session, pairs_snapshot, children_snapshot)
        if traj_path and traj:
            try:
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(None, save_trajectory, traj_path, traj)
                future.add_done_callback(_log_future_exception)
            except RuntimeError:
                save_trajectory(traj_path, traj)
        total_pairs = len(session.pairs) + sum(len(c.pairs) for c in session.child_sessions)
        logger.info("轨迹已导出: %s | 步骤=%d (含 %d 子会话)",
                     session.id[:8], total_pairs, len(session.child_sessions))

    async def export_session_async(self, session: Session, is_final: bool = False):
        """导出会话的 .traj 文件（async 版本）

        子会话不单独导出 traj，它们的数据会在父会话导出时合并。
        标题生成的孤立会话（没有 parent）也跳过导出。

        Args:
            session: 要导出的会话
            is_final: 是否为最终导出（end 事件或超时清理），为 True 时触发自动上传
        """
        if session.is_subagent:
            return
        # 跳过标题生成的孤立会话（没有 parent 的标题生成请求）
        if session.is_title_generation:
            return
        if not session.pairs and not any(c.pairs for c in session.child_sessions):
            return
        # 在事件循环线程中创建所有快照（线程安全）
        pairs_snapshot = list(session.pairs)
        children_snapshot = self._snapshot_children(session)
        traj_path, traj = self._build_traj_data(session, pairs_snapshot, children_snapshot)
        if traj_path and traj:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, save_trajectory, traj_path, traj)
            except Exception as e:
                logger.warning("异步导出 .traj 失败: %s", e)
                traj_path = None  # 导出失败，不触发上传
        total_pairs = len(session.pairs) + sum(len(c.pairs) for c in session.child_sessions)
        logger.info("轨迹已导出: %s | 步骤=%d (含 %d 子会话)",
                     session.id[:8], total_pairs, len(session.child_sessions))

        # 最终导出成功后，复制 events 并触发可靠上传
        if is_final and traj_path:
            # 将 hook events 复制到会话目录（无论是否上传都保留）
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._copy_events_to_session, session.id)

            if self._uploader:
                session_dir = self._session_dir(session.id)
                task = asyncio.create_task(self._uploader.upload_session(session_dir, session.id))
                task.add_done_callback(_log_task_exception)

    def _copy_events_to_session(self, session_id: str):
        """将 hook events 复制到会话目录"""
        import shutil
        src = self.events_dir / f"{session_id}.jsonl"
        if src.exists():
            dst = self._session_dir(session_id) / "events.jsonl"
            shutil.copy2(src, dst)

    def _write_pair_files(self, session: Session, pair: RequestResponsePair,
                          pairs_snapshot: List[RequestResponsePair],
                          children_snapshot: Optional[List[tuple]] = None):
        """同步写入请求/响应文件 + JSONL + .traj（在线程池中执行）

        线程安全：所有可变数据通过 pairs_snapshot / children_snapshot 传入，
        不在此方法中直接访问 session 的可变字段（pairs / child_sessions / last_activity）。
        session.id / start_time / model / cwd / source / is_subagent 在创建后不可变，安全读取。
        """
        idx = pair.index

        # P2 #18: 仅在 save_raw=True 时写入单独的 JSON 文件到 raw/ 子目录
        if self.save_raw:
            raw_dir = self._session_dir(session.id) / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)

            req_file = raw_dir / f"{idx:03d}_request.json"
            resp_file = raw_dir / f"{idx:03d}_response.json"

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

        # 追加写入 raw JSONL（始终执行，不受 save_raw 控制）
        jsonl_path = self._session_dir(session.id) / "raw.jsonl"
        self._append_raw_jsonl(jsonl_path, pair)

        # 增量重建 .traj 文件 — 子会话跳过（它们在父会话导出时合并）
        if not session.is_subagent:
            traj_path, traj = self._build_traj_data(session, pairs_snapshot, children_snapshot)
            if traj_path and traj:
                save_trajectory(traj_path, traj)

    @staticmethod
    def _append_raw_jsonl(jsonl_path: Path, pair: RequestResponsePair):
        """追加写入原始 JSONL

        P0 fix: 只保存 new_messages 而非完整 request_body，避免 O(n^2) 存储膨胀。
        Claude Code 每次请求都重发完整对话历史，JSONL 中保存完整 request_body
        会导致存储量随对话轮数平方增长。首次请求（index=1）保存完整 request_body
        作为基线，后续只保存增量 new_messages。
        """
        if pair.index == 1:
            # 首次请求：保存完整 request_body（含 system prompt 等）
            request_data = pair.request_body
        else:
            # 后续请求：只保存增量 messages + 非 messages 的请求参数
            request_data = {
                k: v for k, v in pair.request_body.items()
                if k != "messages"
            }
            request_data["new_messages"] = pair.new_messages
            request_data["_messages_count"] = len(pair.request_body.get("messages", []))

        record = {
            "timestamp": pair.timestamp,
            "index": pair.index,
            "model": pair.model,
            "request": request_data,
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

        # Fix 5: 前缀不匹配时，再检查尾部是否包含上次的最后几条。
        # 新 turn 开始时 Claude Code 可能在 messages 前面插入 system reminder 等内容，
        # 导致前缀变化，但尾部仍然包含上次的历史。这不是 compaction。
        tail_check = min(3, prev_count)
        if tail_check > 0 and len(curr_messages) > prev_count:
            try:
                tail_match = all(
                    _msg_hash(curr_messages[len(curr_messages) - prev_count + prev_count - tail_check + i])
                    == prev_hashes[prev_count - tail_check + i]
                    for i in range(tail_check)
                )
                if tail_match:
                    # 尾部匹配，说明前面插入了新内容，取尾部新增部分
                    return curr_messages[prev_count:]
            except (IndexError, KeyError):
                pass

        # 真正的 compaction 或历史重写，记录完整历史
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
            # 修复 exit_status 竞态：跟踪 partial task
            session._pending_write_tasks.append(t)

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
    # P0 #4 fix: 在 create_task 之前创建 snapshot
    pairs_snapshot = list(session.pairs)
    t = asyncio.create_task(
        collector.record_response_async(session, pair, complete_response, pairs_snapshot)
    )
    t.add_done_callback(_log_task_exception)
    # 修复 exit_status 竞态：跟踪 task，export 前 drain
    session._pending_write_tasks.append(t)

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

    # Force thinking: 将 adaptive thinking 改写为 effort=max，
    # 提高 thinking blocks 的产生概率（但不保证 100%）。
    #
    # ⚠️ 不能改写为 type=enabled + budget_tokens，原因：
    #   1. Claude Code 按 adaptive 模式管理对话历史，不保证保留 thinking blocks，
    #      而 enabled 模式要求历史中的 thinking blocks 原样保留，否则 API 返回 400。
    #      参见 https://github.com/anthropics/claude-code/issues/14264
    #   2. type=enabled 在 Opus 4.6 上已 deprecated，随时可能被移除。
    #   3. 每次请求强制消耗 budget_tokens 个 thinking tokens，成本大幅增加。
    #
    # 安全方案：改写 thinking.effort 为 "max"（仅 Opus 4.6 支持），
    # 这是 adaptive 模式内部的参数，不改变 type，不破坏对话历史兼容性。
    force_thinking: int = request.app.get("force_thinking", 0)
    body_rewritten = False
    if force_thinking > 0 and request_body.get("thinking"):
        original_thinking = request_body["thinking"]
        if original_thinking.get("type") == "adaptive":
            request_body["thinking"] = {
                "type": "adaptive",
                "effort": "max",
            }
            body_rewritten = True
            logger.debug("thinking 改写: adaptive → adaptive/effort=max")

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
    # 如果 thinking 被改写，需要用修改后的 request_body 重新序列化
    upstream_body = json.dumps(request_body).encode() if body_rewritten else body
    client: aiohttp.ClientSession = request.app["upstream_session"]
    try:
        async with client.request(
            request.method,
            upstream_url,
            headers=headers,
            data=upstream_body,
        ) as upstream_resp:

            if is_streaming:
                return await handle_streaming(request, upstream_resp, session, pair, collector)
            else:
                resp_body = await upstream_resp.read()
                try:
                    response_json = json.loads(resp_body)
                except json.JSONDecodeError:
                    response_json = {"_raw": resp_body.decode("utf-8", errors="replace")}

                # P0 #4 fix: 在 create_task 之前创建 snapshot，确保快照时机正确
                pairs_snapshot = list(session.pairs)
                t = asyncio.create_task(
                    collector.record_response_async(session, pair, response_json, pairs_snapshot)
                )
                t.add_done_callback(_log_task_exception)
                # 修复 exit_status 竞态：跟踪 task，export 前 drain
                session._pending_write_tasks.append(t)

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
    """接收 Hooks 的会话事件（stop / end）

    stop: Claude Code 一轮对话结束（用户可能继续提问），触发增量导出但不删除 session
    end:  Claude Code 会话彻底结束，触发最终导出并清理 session

    竞态保护：先用 _exporting_sessions 标记，防止 cleanup_expired 同时操作同一 session。
    """
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    session_id = data.get("session_id")
    event = data.get("event")
    logger.info("Hook 事件: %s session=%s", event, (session_id or "")[:8])

    if not session_id:
        return web.Response(status=200, text="ok")

    session_manager: SessionManager = request.app["session_manager"]
    collector: DataCollector = request.app["collector"]

    if event == "stop" and session_id in session_manager.active_sessions:
        # stop 事件：增量导出 .traj（不删除 session，用户可能继续提问）
        session = session_manager.active_sessions[session_id]
        if not session.is_subagent:
            session_manager._exporting_sessions.add(session_id)
            try:
                # 修复 exit_status 竞态：等待所有 pending 的 record_response_async 完成
                await _drain_pending_writes(session)
                await collector.export_session_async(session)
                total_pairs = len(session.pairs) + sum(len(c.pairs) for c in session.child_sessions)
                logger.info("Turn 结束增量导出: %s | API 调用=%d", session_id[:8], total_pairs)
            finally:
                session_manager._exporting_sessions.discard(session_id)

    elif event == "end":
        # end 事件：最终导出 + 清理 session
        # 先原子性地从 active_sessions 中移除，防止 cleanup_expired 竞态
        session = session_manager._remove_session_and_children(session_id)
        if session and not session.is_subagent:
            try:
                # 修复 exit_status 竞态：等待所有 pending 的 record_response_async 完成
                await _drain_pending_writes(session)
                await collector.export_session_async(session, is_final=True)
                total_pairs = len(session.pairs) + sum(len(c.pairs) for c in session.child_sessions)
                logger.info(
                    "会话结束: %s | API 调用=%d 次 (含 %d 个子会话)",
                    session_id[:8], total_pairs, len(session.child_sessions),
                )
            except Exception as e:
                logger.warning("会话 %s 最终导出异常: %s", session_id[:8], e)

    return web.Response(status=200, text="ok")


async def handle_health(request: web.Request) -> web.Response:
    """健康检查端点，供 start.sh 等外部脚本探测代理是否就绪"""
    session_manager: SessionManager = request.app["session_manager"]
    return web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps({
            "status": "ok",
            "active_sessions": len(session_manager.active_sessions),
            "pending_sessions": len(session_manager._pending_sessions),
        }),
    )


# ─────────────────────────────────────────────
# 应用工厂
# ─────────────────────────────────────────────

async def create_app(
    upstream_base: str,
    output_dir: Path,
    session_timeout: int,
    save_raw: bool = False,
    events_dir: Optional[Path] = None,
    force_thinking: int = 0,
) -> web.Application:
    app = web.Application()

    # 共享状态
    app["upstream_base"] = upstream_base.rstrip("/")
    app["session_manager"] = SessionManager(session_timeout=session_timeout)
    app["collector"] = DataCollector(output_dir, save_raw=save_raw, events_dir=events_dir)
    app["force_thinking"] = force_thinking

    # 应用启动时创建全局 ClientSession（连接池复用）+ 启动上传管理器
    async def on_startup(app):
        app["upstream_session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, sock_read=300),
        )
        collector = app["collector"]
        if collector._uploader:
            await collector._uploader.start()
        logger.info("ClientSession 已创建（连接池复用）")

    # 应用关闭时停止上传管理器 + 销毁 ClientSession
    async def on_cleanup(app):
        collector = app["collector"]
        if collector._uploader:
            await collector._uploader.stop()
        await app["upstream_session"].close()
        logger.info("ClientSession 已关闭")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # 内部路由（优先注册，避免被通配符覆盖）
    app.router.add_post("/_internal/session-register", handle_session_register)
    app.router.add_post("/_internal/session-event", handle_session_event)
    app.router.add_get("/_internal/health", handle_health)

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
    parser.add_argument(
        "--events-dir",
        default=str(Path.home() / ".claude" / "trajectory_events"),
        help="Hooks 事件数据目录",
    )
    parser.add_argument("--save-raw", action="store_true", default=False, help="保存原始请求/响应 JSON 文件到 raw/ 子目录（默认不保存，raw.jsonl 已包含全部数据）")
    parser.add_argument("--no-save-raw", dest="save_raw", action="store_false", help="不保存原始请求/响应 JSON 文件（默认行为）")
    parser.add_argument(
        "--force-thinking", type=int, default=0, metavar="BUDGET",
        help="强制提高 thinking blocks 产生概率。设为非 0 值时，将 adaptive thinking 的 effort 改写为 max。"
             "effort=max 是 Opus 4.6 独有的最高档，比 high 更激进地触发 thinking。"
             "注意：这不保证 100%% 产生 thinking blocks，但显著提高概率。设为 0 表示不改写（默认）。",
    )
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
        events_dir=Path(args.events_dir),
        force_thinking=args.force_thinking,
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
    if args.force_thinking:
        logger.info("强制 thinking: effort=max (adaptive 模式内提升)")
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
            await session_manager.cleanup_expired(collector=collector)

    cleanup_task = asyncio.create_task(cleanup_loop())
    cleanup_task.add_done_callback(_log_task_exception)

    try:
        # 等待直到 Ctrl+C
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        cleanup_task.cancel()
        # 优雅退出：同步构建所有 traj 数据，然后等待写入完成
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
        # 等待所有写入完成后再 cleanup
        if pending_futures:
            await asyncio.gather(*pending_futures, return_exceptions=True)
        # 优雅退出：持久化上传队列
        if collector._uploader:
            await collector._uploader.stop()
        await runner.cleanup()
        logger.info("代理已停止，数据保存在: %s", output_dir.resolve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
