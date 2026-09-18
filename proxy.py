#!/usr/bin/env python3
"""
claude-trace proxy.py — HTTP 代理服务器
通道 A：透明转发 Claude Code 的 API 请求，SSE Tee 模式采集完整轨迹数据

用法（日常入口是 trace_agent.py；本文件的 main 留给单测和 --upload-status）：
    python3 trace_agent.py --port 4000 --output ./trajectories
    ANTHROPIC_BASE_URL=http://localhost:4000 claude
"""

import argparse
import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import signal
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import aiohttp
from aiohttp import web

from builder import (
    SessionMetadata,
    _traj_step_count,
    apply_hook_events_to_metadata,
    build_trajectory,
    save_trajectory,
)
from git_state import coerce_git_state, collect_git_state, flatten_git_state
from uploader import UploadManager
from version_info import build_fingerprint, resolve_version, version_string

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

# 标准 UUID 形式的 session_id（用于校验从 request metadata 提取的值）
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Claude Code 的 metadata.user_id 拼接格式：..._session_<uuid>
_SESSION_SUFFIX_RE = re.compile(
    r"_session_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


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
    # 无法定位增量边界时（真正的历史重写），new_messages 是完整历史。
    # builder 依据此标记做内容指纹去重，避免 history 中消息重复。
    is_full_replay: bool = False
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
    # 已落盘到 raw.jsonl 的 tool_result id，用于补齐增量遗漏（见 _append_raw_jsonl）
    persisted_tool_result_ids: set = field(default_factory=set)
    # git 状态快照（P0）：会话起点的 HEAD 与工作区脏状态。
    # 事后无法重建 —— 时间反查 commit 会落到主线上不存在的中间状态，
    # 脏工作区更无从得知，而脏工作区意味着 HEAD 不代表真实起点。
    git_state: Dict = field(default_factory=dict)
    git_state_end: Dict = field(default_factory=dict)
    # 会话复活标记：该 session_id 的目录里已有上一轮 incarnation 落盘的 raw.jsonl。
    # 触发场景是超时清理后用户又提问 —— 代理新建了一个 pairs 为空的 Session，
    # 但盘上的历史仍在。prior_pair_count 让 index 接着往下排而不是从 1 重来，
    # revived 则让最终导出走「从 raw.jsonl 重建」以拿回完整轨迹。
    revived: bool = False
    prior_pair_count: int = 0

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
        # hook 在 SessionStart 时点、真实 cwd 下采到的 git 状态优先：
        # 代理侧要等首个 API 请求才知道 cwd，时点偏晚，期间 HEAD 可能已变。
        session.git_state = coerce_git_state(metadata.get("git_state") or metadata)
        if not session.git_state and session.cwd:
            session.git_state = collect_git_state(session.cwd)
        self.active_sessions[session_id] = session
        logger.info("会话关联（%s）: %s", match_type, session_id[:8])
        return session

    @staticmethod
    def _extract_session_id_from_request(request_body: Dict) -> str:
        """从请求体的 metadata.user_id 中提取 Claude Code 的真实 session_id

        Fix: Claude Code 在 request_body.metadata.user_id 里携带了真实 session_id，
        代理此前完全没用它，只靠「Hook 注册 + model 匹配」这类启发式关联。
        Hook 未配置 / SessionStart 未触发 / 多实例并发时启发式就失效，代理兜底
        生成随机 uuid 作为目录名，于是 events.jsonl（按真实 session_id 命名）
        永远对不上号 —— 实测真实轨迹里 61% 缺 events，本机 2974 个 events 文件
        与 4885 个会话目录只有 1251 个交集。

        user_id 有两种已知格式：
          1) JSON 串：{"device_id":"...","account_uuid":"...","session_id":"<uuid>"}
          2) 拼接串：user_<hash>_account__session_<uuid>
        """
        metadata = request_body.get("metadata")
        if not isinstance(metadata, dict):
            return ""
        user_id = metadata.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return ""

        # 格式 1：JSON 串
        if user_id.lstrip().startswith("{"):
            try:
                parsed = json.loads(user_id)
                sid = parsed.get("session_id")
                if isinstance(sid, str) and _SESSION_ID_RE.match(sid):
                    return sid
            except (json.JSONDecodeError, AttributeError):
                pass

        # 格式 2：拼接串，取 _session_ 之后的 uuid
        match = _SESSION_SUFFIX_RE.search(user_id)
        if match:
            return match.group(1)
        return ""

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

    def _attach_child_session(
        self, parent: Session, request_body: Dict, request_model: str,
    ) -> Session:
        """把一个 sub-agent / 标题生成请求挂到已知父会话下

        从策略 3 内联逻辑抽出，供策略 0（真实 session_id 已知但 model 不匹配）复用。
        Claude Code 的 sub-agent 与标题生成使用不同 model（通常是 haiku），
        且携带与主会话相同的 session_id，不应产生独立的 traj 文件。
        """
        messages = request_body.get("messages", []) or []
        has_system = bool(request_body.get("system"))
        # 识别标题生成请求：haiku model + 单条 message + 有 system prompt
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
        0. 请求体 metadata.user_id 中的真实 session_id（确定性，最可靠）
        1. Hooks 注册的 pending 队列（model 匹配 → 确定性关联）
        2. 已有活跃会话的对话内容连续性匹配（同 model 优先）
        3. Sub-agent 路由：model 不匹配时，关联到唯一的活跃主会话作为子会话
        4. 兜底：创建新会话
        """
        request_model = request_body.get("model", "")

        # 策略 0：从请求体直接读取 Claude Code 的真实 session_id。
        # 这是唯一确定性的关联信号，优先于所有启发式匹配 ——
        # 实测 250 个抽样里有 122 个（76%）目录名与请求携带的真实 session_id
        # 不一致，正是 events.jsonl 对不上号的根因。
        #
        # 注意：sub-agent / 标题生成请求携带的是同一个主会话 session_id
        # （已验证：同一会话里 haiku 的 subagent 请求 session_id 与主会话相同），
        # 所以这里只做「主会话归属」判定，不同 model 的请求仍走 sub-agent 路由，
        # 由下方策略 3 挂到父会话下面。
        real_sid = self._extract_session_id_from_request(request_body)
        if real_sid:
            existing = self.active_sessions.get(real_sid)
            if existing is not None and not existing.is_subagent:
                if _models_match(existing.model, request_model):
                    existing.update_activity()
                    return existing
                # model 不同 → sub-agent / 标题生成，挂到这个已知父会话下
                existing.update_activity()
                return self._attach_child_session(existing, request_body, request_model)

            # pending 中已有该 id（Hook 注册过）→ 用 Hook 带来的 metadata 创建
            if real_sid in self._pending_sessions:
                parent = self._create_session_from_pending(
                    real_sid, self._pending_sessions[real_sid], "请求 session_id + Hook metadata",
                )
                if _models_match(parent.model, request_model) or not parent.model:
                    if not parent.model:
                        parent.model = request_model
                    return parent
                return self._attach_child_session(parent, request_body, request_model)

            # Hook 未注册（未配置 hooks 或 SessionStart 未触发）：
            # 直接用真实 id 建会话，events 后续仍能按同名文件关联上。
            session = Session(
                id=real_sid,
                model=request_model,
                cwd=self._extract_cwd_from_request(request_body),
            )
            # hook 未配置 / SessionStart 未触发：退回代理侧自采。
            # cwd 来自 system prompt 正则反解，可能为空，此时拿不到 git 状态。
            if session.cwd:
                session.git_state = collect_git_state(session.cwd)
            self.active_sessions[real_sid] = session
            logger.info("会话关联（请求 session_id）: %s model=%s", real_sid[:8], request_model)
            return session

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

            return self._attach_child_session(parent, request_body, request_model)

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
        session = Session(
            id=sid,
            model=request_body.get("model", ""),
            cwd=self._extract_cwd_from_request(request_body),
        )
        if session.cwd:
            session.git_state = collect_git_state(session.cwd)
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
            # 默认 "false"：不配置就保留本地数据（见下方 cleanup_after_upload）
            cleanup_env = os.environ.get("TRAJ_CLEANUP_AFTER_UPLOAD", "false").strip().lower()
            backfill_env = os.environ.get("TRAJ_BACKFILL_ON_START", "true").strip().lower()
            # 默认保留本地数据。
            #
            # 老默认值是 "true"（上传成功即删本地），后果是「上传成功」这个
            # 判断一旦有偏差，数据就没了第二份 —— 而 409 幂等 bug 恰恰会把
            # 「服务端拒绝覆盖」也算成成功。实测 7247 个会话目录只剩一个
            # .uploaded 标记，其中 1786 个是真实主会话，本地已无法重建。
            # 删数据必须是显式选择，不能是默认行为。
            cleanup_after_upload = cleanup_env == "true"
            if cleanup_after_upload:
                logger.warning(
                    "TRAJ_CLEANUP_AFTER_UPLOAD=true：上传成功后将删除本地数据，"
                    "云端将是唯一副本",
                )
            self._uploader: Optional[UploadManager] = UploadManager(
                upload_url=upload_url,
                upload_token=upload_token,
                cleanup_after_upload=cleanup_after_upload,
                # 启动补传需要知道去哪儿扫：上传链路的兜底，见 _backfill_loop
                sessions_dir=self.sessions_dir,
                backfill_enabled=(backfill_env != "false"),
            )
            logger.info("可靠上传已启用: %s", upload_url)
        else:
            self._uploader = None
            # 上传默认关闭：TRAJ_PLATFORM_URL 与 TRAJ_UPLOAD_TOKEN 两者皆非空才启用
            logger.info("上传未配置，数据仅保存在本地: %s", self.sessions_dir)

    def _session_dir(self, session_id: str) -> Path:
        """获取会话目录，按需创建"""
        d = self.sessions_dir / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _count_raw_records(self, session_id: str) -> int:
        """数 raw.jsonl 已落盘的记录数（会话复活检测用）

        只数行不解析 JSON：这是每个 Session 首个请求路径上的同步调用，
        必须够快。raw.jsonl 每行一条记录，行数即历史轮数。
        """
        raw_path = self.sessions_dir / session_id / "raw.jsonl"
        if not raw_path.exists():
            return 0
        try:
            with raw_path.open("rb") as f:
                return sum(1 for line in f if line.strip())
        except OSError as e:
            logger.debug("统计 raw.jsonl 行数失败 %s: %s", session_id[:8], e)
            return 0

    def record_request(
        self,
        session: Session,
        request_body: Dict,
        raw_headers: Dict[str, str],
    ) -> RequestResponsePair:
        """记录请求，提取增量 messages"""
        curr_messages = request_body.get("messages", [])
        new_messages, is_full_replay = self._extract_incremental_messages(
            session.prev_msg_hashes, session.prev_msg_count, curr_messages,
        )

        # Fix 2: Hook 注册时 model 可能为 None（Claude Code SessionStart 不传 model），
        # 从首次 API 请求的 request_body 中补全
        if not session.model and request_body.get("model"):
            session.model = request_body["model"]
            logger.info("从 API 请求补全 model: %s (session=%s)", session.model, session.id[:8])

        # 会话复活检测：仅在本 Session 的首个请求时做一次盘上探测。
        # 超时清理会销毁 Session 对象但保留目录，用户隔一会儿再提问就会新建
        # 一个 pairs 为空的 Session。不认这段历史的话 index 会从 1 重来，
        # traj 被短轨迹覆盖（见 save_trajectory 的 allow_shrink）。
        if not session.pairs and not session.is_subagent and not session.revived:
            prior = self._count_raw_records(session.id)
            if prior > 0:
                session.revived = True
                session.prior_pair_count = prior
                # 复活后的首个请求带着完整历史，但内存里没有 prev_hashes 做基线，
                # 所以 new_messages 就是整段历史。不打 replay 标记的话 builder 会
                # 把复活前已记录过的 user 消息又写一遍进 history。
                is_full_replay = True
                logger.info(
                    "会话复活: %s 盘上已有 %d 轮，index 从 %d 续排",
                    session.id[:8], prior, prior + 1,
                )

        # P0 #1: 在 append 之前分配序号。
        # 安全假设：record_request 是同步方法，aiohttp 单线程事件循环中
        # 两个 await 点之间不会被打断，因此无需加锁。
        # 如果未来改为 async，需要引入 asyncio.Lock 保护。
        # 复活会话从 prior_pair_count 之后续排，避免 raw.jsonl 里出现重复 index。
        idx = session.prior_pair_count + len(session.pairs) + 1

        pair = RequestResponsePair(
            timestamp=datetime.now().isoformat(),
            request_body=request_body,
            request_headers=sanitize_headers_for_storage(raw_headers),
            index=idx,
            new_messages=new_messages,
            is_full_replay=is_full_replay,
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
        # Sequence 而非 List：复活重建走 merger 的 _AdaptedPair，
        # 与 RequestResponsePair 结构兼容但不同名（builder 只按属性取值）。
        pairs_snapshot: Sequence[Any],
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
                git_state=session.git_state or {},
                git_state_end=session.git_state_end or {},
                **flatten_git_state(session.git_state),
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
                    apply_hook_events_to_metadata(metadata, hook_events)
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

    def _rebuild_traj_from_raw(
        self, session: Session, children_snapshot: Optional[List[tuple]] = None,
    ) -> tuple:
        """从 raw.jsonl 重建完整轨迹（复活会话的最终导出用）

        在线程池中执行：读整个 raw.jsonl + 重建可能较慢（实测最大 766MB）。
        复用 merger 的加载器，保证与 tools/rebuild_trajs.py 的重建口径一致。

        子会话的 pair 只存在于内存、从未单独落盘到 raw.jsonl，所以重建后
        仍要把 children_snapshot 合并进来。

        失败返回 (None, None)，调用方退回内存快照。
        """
        raw_path = self.sessions_dir / session.id / "raw.jsonl"
        if not raw_path.exists():
            return None, None
        try:
            from merger import _adapt_raw_pair, load_raw_pairs_from_jsonl

            raw_pairs = load_raw_pairs_from_jsonl(raw_path)
            if not raw_pairs:
                return None, None
            adapted = [_adapt_raw_pair(p, i + 1) for i, p in enumerate(raw_pairs)]
        except Exception as e:
            logger.warning("重建 %s 的 raw.jsonl 失败: %s", session.id[:8], e)
            return None, None

        # 走与常规导出相同的构建路径，metadata / 子会话合并逻辑完全一致
        return self._build_traj_data(session, adapted, children_snapshot)

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
        loop = asyncio.get_running_loop()

        # 复活会话的最终导出：内存里只有复活后那一段 pairs，直接建出来的 traj
        # 会丢掉复活前的全部历史。raw.jsonl 是 append 写入、历史完整，
        # 所以最终导出改为从盘上重建，拿回整条轨迹。
        traj = None
        traj_path = None
        if is_final and session.revived:
            traj_path, traj = await loop.run_in_executor(
                None, self._rebuild_traj_from_raw, session, children_snapshot,
            )
            if traj is None:
                logger.warning(
                    "复活会话从 raw.jsonl 重建失败，退回内存快照: %s", session.id[:8],
                )

        if traj is None:
            traj_path, traj = self._build_traj_data(session, pairs_snapshot, children_snapshot)

        if traj_path and traj:
            try:
                # allow_shrink=False：拒绝用更短的轨迹覆盖盘上已有的版本，
                # 兜住复活重建失败退回内存快照的场景。
                await loop.run_in_executor(
                    None, functools.partial(save_trajectory, allow_shrink=False),
                    traj_path, traj,
                )
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
                task = asyncio.create_task(
                    self._uploader.upload_session(session_dir, session.id, tool_source="claude-code")
                )
                task.add_done_callback(_log_task_exception)

    def _copy_events_to_session(self, session_id: str):
        """将 hook events 复制到会话目录

        session_id 现在优先来自请求体 metadata（见 _extract_session_id_from_request），
        与 hooks 写出的 events 文件名一致，因此正常路径直接命中。
        """
        import shutil
        src = self.events_dir / f"{session_id}.jsonl"
        if not src.exists():
            logger.debug("hook events 不存在，跳过复制: %s", session_id[:8])
            return
        dst = self._session_dir(session_id) / "events.jsonl"
        dst.parent.mkdir(parents=True, exist_ok=True)
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
                # 与 raw.jsonl 通道保持一致，供 merger 重建时去重
                "is_full_replay": pair.is_full_replay,
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
        self._append_raw_jsonl(jsonl_path, pair, session.persisted_tool_result_ids)

        # 增量重建 .traj 文件 — 子会话跳过（它们在父会话导出时合并）
        if not session.is_subagent:
            traj_path, traj = self._build_traj_data(session, pairs_snapshot, children_snapshot)
            if traj_path and traj:
                # allow_shrink=False：复活会话的内存快照只含复活后那一段，
                # 每轮增量写都会试图用短轨迹覆盖盘上的完整版本。
                # 闸门挡住之后，完整轨迹在最终导出时由 raw.jsonl 重建。
                save_trajectory(traj_path, traj, allow_shrink=not session.revived)

    @staticmethod
    def _collect_missing_tool_results(
        pair: RequestResponsePair, persisted_ids: set,
    ) -> List[Dict]:
        """找出本轮请求里尚未落盘过的 tool_result 块

        Fix: raw.jsonl 只在 index==1 落完整 messages，之后全靠 new_messages 增量。
        一旦增量边界算错（compaction、消息被插入历史中部等），落在边界外的
        tool_result 就永久丢失 —— 重建时表现为 orphan observation，而在线路径
        因为读的是内存里的完整 request_body 反而看不出问题，属于静默数据丢失。
        这里做一层完整性兜底：扫描完整 messages，把没落盘过的 tool_result 补上。
        """
        missing: List[Dict] = []
        for msg in (pair.request_body or {}).get("messages", []) or []:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                if tid and tid not in persisted_ids:
                    persisted_ids.add(tid)
                    missing.append(block)
        return missing

    @staticmethod
    def _register_persisted_tool_results(messages, persisted_ids: set) -> None:
        """把一批 messages 中的 tool_result id 登记为已落盘"""
        for msg in messages or []:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        persisted_ids.add(tid)

    def _append_raw_jsonl(self, jsonl_path: Path, pair: RequestResponsePair,
                          persisted_tool_result_ids: Optional[set] = None):
        """追加写入原始 JSONL

        P0 fix: 只保存 new_messages 而非完整 request_body，避免 O(n^2) 存储膨胀。
        Claude Code 每次请求都重发完整对话历史，JSONL 中保存完整 request_body
        会导致存储量随对话轮数平方增长。首次请求（index=1）保存完整 request_body
        作为基线，后续只保存增量 new_messages。
        """
        ids = persisted_tool_result_ids if persisted_tool_result_ids is not None else set()

        if pair.index == 1:
            # 首次请求：保存完整 request_body（含 system prompt 等）
            request_data = pair.request_body
            self._register_persisted_tool_results(
                (pair.request_body or {}).get("messages"), ids,
            )
        else:
            # 后续请求：只保存增量 messages + 非 messages 的请求参数
            request_data = {
                k: v for k, v in pair.request_body.items()
                if k != "messages"
            }
            request_data["new_messages"] = pair.new_messages
            request_data["_messages_count"] = len(pair.request_body.get("messages", []))
            self._register_persisted_tool_results(pair.new_messages, ids)

            # 完整性兜底：补上增量漏掉的 tool_result，避免重建时变成 orphan
            recovered = self._collect_missing_tool_results(pair, ids)
            if recovered:
                request_data["_recovered_tool_results"] = recovered
                logger.debug(
                    "补齐增量遗漏的 tool_result: %d 个 (index=%d)",
                    len(recovered), pair.index,
                )

        record = {
            "timestamp": pair.timestamp,
            "index": pair.index,
            "model": pair.model,
            "request": request_data,
            "response": pair.response_body,
            "usage": pair.usage,
            "stop_reason": pair.stop_reason,
            "is_partial": pair.is_partial,
            # 落盘重放标记，重建轨迹（merger / tools/rebuild_trajs）时同样需要去重
            "is_full_replay": pair.is_full_replay,
        }
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _extract_incremental_messages(
        prev_hashes: List[str], prev_count: int, curr_messages: List,
    ) -> tuple:
        """提取增量 messages — Claude Code 每次请求都重发完整历史

        返回 (new_messages, is_full_replay)。
        is_full_replay=True 表示无法定位增量边界，new_messages 是完整历史，
        下游（builder）需要按内容指纹去重，否则同一条消息会反复进 history。

        Fix 1: 原实现的尾部校验分支索引算错了 ——
            curr[len(curr) - prev_count + prev_count - tail_check + i]
          化简就是 curr[len(curr) - tail_check + i]，即拿 curr 的「最后几条」
          去比 prev 的「最后几条」。但 curr 尾部恰恰是本轮新追加的消息，
          所以该分支几乎永远不成立，每次前缀不匹配都退化成返回完整历史。
          实测后果：一个会话里同一个 tool_result 在 history 中出现几十次
          （最坏 0 个 tool_use 对应 570 个 tool_result 块）。
          现在改为「以上次最后一条消息为锚点反向定位」，这是正确的对齐方式。

        Fix 2: 原实现返回完整历史时不给任何标记，下游无法区分
          「真的有这么多新消息」和「定位失败的重放」。现在显式返回标记。
        """
        if not prev_hashes:
            return curr_messages, False

        curr_hashes = [_msg_hash(m) for m in curr_messages]

        # 1) 快路径：前缀完全一致且有新增 → 直接取尾部新增
        if len(curr_messages) > prev_count:
            check_count = min(5, prev_count)
            if all(curr_hashes[i] == prev_hashes[i] for i in range(check_count)):
                return curr_messages[prev_count:], False

        # 2) 锚点定位：从后往前找上次最后一条消息在本次历史中的位置。
        #    覆盖「消息被插到历史前部导致前缀变化」的场景（system reminder 注入等）。
        anchor = prev_hashes[-1]
        for i in range(len(curr_hashes) - 1, -1, -1):
            if curr_hashes[i] == anchor:
                return curr_messages[i + 1:], False

        # 3) 退一步：找上次历史里任意一条最靠后的消息作为锚点。
        #    compaction 会截断前部历史，但尾部通常仍被保留。
        prev_pos = {h: idx for idx, h in enumerate(prev_hashes)}
        best_curr_idx = -1
        best_prev_idx = -1
        for i, h in enumerate(curr_hashes):
            p = prev_pos.get(h)
            if p is not None and p >= best_prev_idx:
                best_prev_idx, best_curr_idx = p, i
        if best_curr_idx >= 0:
            return curr_messages[best_curr_idx + 1:], False

        # 4) 完全无法对齐（真正的历史重写）：返回完整历史并打上重放标记
        return curr_messages, True


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
    """判断是否为需要采集的 messages API 请求

    Fix: 原实现用 `"/v1/messages" in path`，对 `/v1/messages/count_tokens`
    同样成立。Claude Code 会频繁发 count_tokens 预估 token 用量，每一个都被
    当成新会话注册、建目录、写空 traj——实测污染了 1020 个会话目录（21%），
    这些目录 trajectory 为空、token 全 0、response 是上游的
    "Invalid URL (POST /v1/messages/count_tokens)" 错误。

    现在要求路径以 /v1/messages 结尾（允许尾部斜杠），
    子路径端点（count_tokens、batches 等）全部走透传不采集。
    """
    if method != "POST" or "messages" not in request_body:
        return False
    normalized = path.rstrip("/")
    return normalized.endswith("/v1/messages")


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

    # 采集用解析后的 dict；转发给上游必须用原始 bytes。
    # 曾经 --force-thinking 会改 thinking 再 json.dumps 整份 body：
    #   1. effort 不属于 thinking（应在 output_config），新模型直接 400
    #   2. json.dumps 默认 ensure_ascii=True，tool_use 里的 UTF-8 被改成 \uXXXX
    #   3. 丢掉 thinking.display 等 Claude Code 新字段
    # Claude Code 2.1.275+ 因此报 Invalid tool use format。
    # 所以这里永远不再改写、不再重序列化。见 tests/test_proxy_passthrough.py。

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
    # 必须用原始 bytes：任何 json.dumps 重序列化都会改变转义/字段，上游可能 400。
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
        # 先记录终点 git 状态，再移除 session。
        # 终点状态由 collector 随本次通知一起送来，不走 events.jsonl ——
        # collector 是「先 notify、后写文件」，代理这边导出 traj 时
        # events.jsonl 里的 SessionEnd 往往还没落盘，读不到。
        pre_removal = session_manager.active_sessions.get(session_id)
        if pre_removal is not None:
            end_state = coerce_git_state(
                data.get("git_state_end") or data.get("git_state") or {}
            )
            if end_state:
                pre_removal.git_state_end = end_state
            elif pre_removal.cwd:
                # hook 没送来（旧版 collector 的 end 通知不带 git 状态）：
                # 代理侧兜底自采。时点比 hook 稍晚但仍在会话结束瞬间，足够用。
                pre_removal.git_state_end = collect_git_state(pre_removal.cwd)
        # 原子性地从 active_sessions 中移除，防止 cleanup_expired 竞态
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
    """健康检查端点，供 start.sh 等外部脚本探测代理是否就绪

    同时暴露上传积压 —— 上传静默失效时，这里是唯一能看出来的地方。
    """
    session_manager: SessionManager = request.app["session_manager"]
    collector: DataCollector = request.app["collector"]

    payload = {
        "status": "ok",
        # 版本与构建指纹：回答「现在这个端口上跑的到底是哪一份」。
        # 只报版本号不够 —— 同一个 0.2.0 曾对应过行为不同的两份二进制，
        # 于是「以为在跑今天的修复，实际在跑 13 天前的包」无法被发现。
        "version": resolve_version(),
        "build": build_fingerprint() or ("source" if not getattr(sys, "frozen", False) else ""),
        "active_sessions": len(session_manager.active_sessions),
        "pending_sessions": len(session_manager._pending_sessions),
    }

    uploader = collector._uploader
    if uploader is None:
        payload["upload"] = {"enabled": False}
    else:
        loop = asyncio.get_running_loop()
        try:
            pending = await loop.run_in_executor(None, uploader.scan_pending_sessions)
            pending_count = len(pending)
        except Exception as e:
            logger.debug("健康检查扫描积压失败: %s", e)
            pending_count = -1
        queue = uploader.queue_stats()
        payload["upload"] = {
            "enabled": True,
            "server_healthy": uploader.server_healthy,
            # 说后果不说现象：这个数字的含义是「这么多会话的轨迹还没上云」
            "sessions_not_uploaded": pending_count,
            "queue": queue,
        }
        if pending_count > 20 or queue.get("oversize") or queue.get("failed"):
            payload["status"] = "degraded"

    return web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps(payload),
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
    # --force-thinking 已废弃：参数保留以免旧 launchd / 脚本 unrecognized arguments，
    # 但不再改写请求体。非 0 只打一次警告，方便发现还在传这个开关的配置。
    app["force_thinking"] = 0
    if force_thinking:
        logger.warning(
            "--force-thinking=%s 已废弃并被忽略：代理不再改写请求体，"
            "thinking / tool_use 按 Claude Code 原始字节转发。"
            "请从 launchd plist、channels.json 和启动脚本中去掉该参数。",
            force_thinking,
        )

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
    parser.add_argument(
        "--version", action="version", version=version_string(),
        help="打印版本与构建指纹后退出",
    )
    parser.add_argument("--port", type=int, default=4000, help="代理监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，防止局域网访问）")
    parser.add_argument("--output", default="./trajectories", help="轨迹数据输出目录")
    parser.add_argument(
        "--upstream",
        default="https://api.anthropic.com",
        help="上游 API 地址",
    )
    # 默认 1800s（30 分钟），不是 300s。
    #
    # 300s 太激进：用户开个会、看会儿文档、思考五分钟，会话就被判过期清理掉，
    # 再提问时新建 Session、index 从 1 重来，于是轨迹被截断（实测 140 个会话
    # 因此损坏，最严重的一个在 7 小时里被切成 28 段）。复活逻辑现在能兜住，
    # 但把窗口放宽到 30 分钟能从根上少踩这条路径。
    # 代价只是内存里多留会话对象一会儿，可忽略。
    parser.add_argument("--session-timeout", type=int, default=1800, help="会话超时时间（秒）")
    parser.add_argument(
        "--upload-status", action="store_true",
        help="打印上传积压状态后退出（不启动代理）。用于排查上传是否静默失效。",
    )
    parser.add_argument(
        "--events-dir",
        default=str(Path.home() / ".claude" / "trajectory_events"),
        help="Hooks 事件数据目录",
    )
    parser.add_argument("--save-raw", action="store_true", default=False, help="保存原始请求/响应 JSON 文件到 raw/ 子目录（默认不保存，raw.jsonl 已包含全部数据）")
    parser.add_argument("--no-save-raw", dest="save_raw", action="store_false", help="不保存原始请求/响应 JSON 文件（默认行为）")
    parser.add_argument(
        "--force-thinking", type=int, default=0, metavar="BUDGET",
        help="已废弃，忽略。曾把 adaptive thinking 改写为 effort=max 并重序列化请求体，"
             "会在 Claude Code 2.1.275+ 触发 Invalid tool use format 400。",
    )
    parser.add_argument("--verbose", action="store_true", help="详细日志输出")
    return parser.parse_args()


def print_upload_status(output_dir: Path) -> int:
    """打印上传积压状态（--upload-status），返回进程退出码

    存在的理由：上传静默失效时，唯一能看出来的地方就是「多少会话还没上云」。
    sid-code 的 52 个会话一次都没传上去而无人发现，正是因为没有任何地方
    能一眼看到这个数字。
    """
    sessions_dir = output_dir / "sessions"
    if not sessions_dir.is_dir():
        print(f"sessions 目录不存在: {sessions_dir}")
        return 1

    url = os.environ.get("TRAJ_PLATFORM_URL", "").strip()
    token = os.environ.get("TRAJ_UPLOAD_TOKEN", "").strip()
    print(f"数据目录: {sessions_dir}")
    if not url or not token:
        print("上传: 未配置（TRAJ_PLATFORM_URL / TRAJ_UPLOAD_TOKEN 为空）")
        print("      数据仅保存在本地。")
        return 0

    mgr = UploadManager(
        upload_url=url, upload_token=token,
        cleanup_after_upload=False,
        sessions_dir=sessions_dir, backfill_enabled=False,
    )
    pending = mgr.scan_pending_sessions()
    queue = mgr.queue_stats()

    total = sum(1 for d in sessions_dir.iterdir() if d.is_dir())
    uploaded = sum(1 for d in sessions_dir.iterdir() if (d / ".uploaded").exists())

    print(f"上传目标: {url}")
    print()
    print(f"会话目录总数        : {total}")
    print(f"已确认上云          : {uploaded}")
    print(f"轨迹仍未上云        : {len(pending)}")
    print()
    print(f"重试队列            : 共 {queue['total']} 项 "
          f"(待重试 {queue['pending']} / 重试到死 {queue['failed']} / 过大 {queue['oversize']})")

    if pending:
        print("\n未上云的会话（最近 15 个）:")
        for d in pending[:15]:
            steps = _traj_step_count(d / "session.traj")
            print(f"  {d.name[:8]}  {steps:>5} 步")
    if queue["failed"] or queue["oversize"]:
        print("\n⚠️  队列里有终态项，本地数据仍在，可用 tools/recover_truncated.py 排查")
    if not pending and not queue["total"]:
        print("\n✅ 没有积压")
    return 0


async def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output_dir = Path(args.output)

    if args.upload_status:
        sys.exit(print_upload_status(output_dir))

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
    logger.info("claude-trace 代理已启动 | %s", version_string())
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
            await session_manager.cleanup_expired(collector=collector)

    cleanup_task = asyncio.create_task(cleanup_loop())
    cleanup_task.add_done_callback(_log_task_exception)

    # 信号处理：SIGTERM / SIGHUP / SIGINT 都要走完 finally 里的优雅退出。
    #
    # 修复前只 catch KeyboardInterrupt —— 而 SIGTERM 的默认处置是直接终止进程，
    # finally 一行都不执行：活跃会话的最终导出、上传队列持久化全部跳过。
    # 偏偏老的 install-daemon.sh restart 用的是 `launchctl kickstart -k`，
    # 那正是 SIGTERM，也就是文档推荐的恢复动作本身在丢数据。
    # 现在 restart 已改成 bootout + bootstrap，但 SIGTERM 这条路径仍要接住：
    # launchd 停服务、用户 kill、二次信号之前的第一次，全都走它。
    # SIGHUP 同样要接（关终端 / SSH 断连），它的默认处置也是终止。
    stop_event = asyncio.Event()
    received_signal: List[str] = []

    def _on_signal(signame: str):
        if received_signal:
            # 第二次收到信号：用户在催，立即硬退出
            logger.warning("再次收到 %s，立即退出（跳过收尾）", signame)
            os._exit(1)
        received_signal.append(signame)
        logger.info("收到 %s，开始优雅退出…", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, signame, None)
        if sig is None:  # 平台不支持（如 Windows 没有 SIGHUP）
            continue
        try:
            loop.add_signal_handler(sig, functools.partial(_on_signal, signame))
        except (NotImplementedError, RuntimeError) as e:
            logger.debug("注册 %s 处理器失败: %s", signame, e)

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        cleanup_task.cancel()
        # 优雅退出：只做「快」的事 —— 把内存里的会话落盘。
        #
        # 刻意不在这里等上传（sid-code 的教训：上传挂在退出关键路径上，
        # 进程一退 fetch 就被杀在半路，于是一次都没成功过）。落盘实测约 30ms，
        # 上传要秒级；未上传的会话交给下次启动的补传扫描兜底。
        pending_futures = []
        for _sid, session in list(session_manager.active_sessions.items()):
            if not session.is_subagent and (session.pairs or session.child_sessions):
                pairs_snapshot = list(session.pairs)
                children_snapshot = DataCollector._snapshot_children(session)
                # 复活会话从 raw.jsonl 重建，避免落盘一份被截断的短轨迹
                traj_path, traj = (None, None)
                if session.revived:
                    traj_path, traj = collector._rebuild_traj_from_raw(session, children_snapshot)
                if traj is None:
                    traj_path, traj = collector._build_traj_data(
                        session, pairs_snapshot, children_snapshot,
                    )
                if traj_path and traj:
                    saver = functools.partial(save_trajectory, allow_shrink=False)
                    try:
                        future = loop.run_in_executor(None, saver, traj_path, traj)
                        pending_futures.append(future)
                    except RuntimeError:
                        saver(traj_path, traj)
                # events 也要落到会话目录，否则补传上去的会话缺 events.jsonl
                try:
                    collector._copy_events_to_session(session.id)
                except Exception as e:
                    logger.debug("优雅退出复制 events 失败 %s: %s", session.id[:8], e)
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
