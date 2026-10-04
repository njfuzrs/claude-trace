#!/usr/bin/env python3
"""
transcript.py — Claude Code 本地 transcript 的解析与补洞（S4 / L2）

transcript 路径：`~/.claude/projects/<slug>/<session_id>.jsonl`，Stop hook 会带回
`transcript_path`。它是 Claude Code 的内部格式，版本间会变，所以这里只当
**补洞通道**，不当主源：

  - raw.jsonl（代理落的 wire 级请求/响应）永远优先；
  - transcript 只补 raw 没有的 assistant 轮，且只补「最后一个能对上的 raw 轮之后」
    的尾部缺口。中间缺口只计数不插入 —— 对不上位置的数据不许硬塞进轨迹中间；
  - SSE 被打断（is_partial）、而 transcript 里同一条消息是完整的，才用 transcript
    的内容补全那一轮。

解析器前向兼容：未知 `type` 跳过并计数，坏行跳过并计数，只读已知键。

已知限制（2026-09 实测）：
  - transcript 里 thinking 块的 signature 为空字符串，不能用于回放；
  - toolUseResult 没有数值退出码，退出码仍以 tool_result 文本为准；
  - 一条 API 响应会被拆成多条 assistant 记录（每个 content block 一条），
    共享 message.id，这里按 id 合并回一条；
  - tool_use.input 会被 Claude Code 改写（实测去掉了 `cd <cwd> &&` 前缀），
    发给上游的原样在 `wireToolInputs`（按 tool_use id 索引），合并时以它为准。
"""

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("claude-trace")

# 本模块消费的 type
_CONSUMED_TYPES = frozenset({"user", "assistant", "permission-mode"})
# 已知但不消费的 type（只计数，不算 unknown）
_IGNORED_TYPES = frozenset({
    "system", "attachment", "mode", "summary", "file-history-snapshot",
    "file-history-delta", "last-prompt", "ai-title", "atis-latch",
    "queue-operation", "pr-link", "cost-state", "custom-title",
    "relocated", "worktree-state", "agent-name",
})
# Claude Code 本地合成的 assistant 消息（API 报错占位等），不是模型输出
_SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class TranscriptMessage:
    """按 message.id 合并后的一条 assistant 消息，连同它之前的 user 消息"""
    message_id: str
    model: str
    timestamp: str
    content: List[Dict]
    stop_reason: str
    usage: Dict
    # 上一条 assistant 之后、本条之前的 user 消息（API 形状：{role, content}）
    preceding_users: List[Dict] = field(default_factory=list)

    def tool_use_ids(self) -> List[str]:
        return [
            b["id"] for b in self.content
            if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use")
            and isinstance(b.get("id"), str) and b["id"]
        ]


@dataclass
class Transcript:
    path: str
    session_id: str = ""
    assistants: List[TranscriptMessage] = field(default_factory=list)
    # 末尾 assistant 之后的 user 消息（本轮工具结果 / 用户最后一句）
    trailing_users: List[Dict] = field(default_factory=list)
    # 权限模式时间线：按 user 记录上的 permissionMode 做边沿检测
    permission_mode_timeline: List[Dict] = field(default_factory=list)
    type_counts: Dict[str, int] = field(default_factory=dict)
    unknown_types: Dict[str, int] = field(default_factory=dict)
    bad_lines: int = 0
    sidechain_skipped: int = 0
    synthetic_skipped: int = 0


def _wire_inputs(record: Dict) -> Dict[str, Any]:
    """读 wireToolInputs：实测既有 {id: input} 也有 [{id: input}, ...] 两种形状"""
    raw = record.get("wireToolInputs")
    items = raw if isinstance(raw, list) else [raw]
    out: Dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            for k, v in item.items():
                if isinstance(k, str) and isinstance(v, dict):
                    out[k] = v
    return out


def _restore_wire_inputs(blocks: List[Dict], wire: Dict[str, Any]) -> List[Dict]:
    if not wire:
        return blocks
    out = []
    for b in blocks:
        if b.get("type") == "tool_use" and b.get("id") in wire:
            b = {**b, "input": wire[b["id"]]}
        out.append(b)
    return out


def _user_message(record: Dict) -> Optional[Dict]:
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, (str, list)):
        return None
    return {"role": "user", "content": content}


def parse_transcript(path: Path) -> Transcript:
    """解析 transcript jsonl。未知 type / 坏行跳过并计数，绝不抛出解析异常。

    读文件本身失败（不存在、权限）仍会抛 OSError，由调用方决定怎么处理。
    """
    path = Path(path)
    result = Transcript(path=str(path))
    # message.id → 合并中的消息；顺序以首次出现为准
    merged: Dict[str, TranscriptMessage] = {}
    order: List[str] = []
    pending_users: List[Dict] = []
    last_mode: Optional[str] = None

    with path.open(errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                result.bad_lines += 1
                continue
            if not isinstance(record, dict):
                result.bad_lines += 1
                continue
            rtype = record.get("type")
            if not isinstance(rtype, str):
                rtype = ""
            result.type_counts[rtype] = result.type_counts.get(rtype, 0) + 1
            if rtype not in _CONSUMED_TYPES:
                if rtype not in _IGNORED_TYPES:
                    result.unknown_types[rtype] = result.unknown_types.get(rtype, 0) + 1
                continue
            if not result.session_id and isinstance(record.get("sessionId"), str):
                result.session_id = record["sessionId"]

            if rtype == "permission-mode":
                # 这类记录不带时间戳，时间线改用 user 记录上的 permissionMode
                continue

            # sub-agent 的侧链记录不属于主会话（代理那边是独立的子会话）
            if record.get("isSidechain"):
                result.sidechain_skipped += 1
                continue

            if rtype == "user":
                mode = record.get("permissionMode")
                if isinstance(mode, str) and mode:
                    if last_mode and mode != last_mode:
                        result.permission_mode_timeline.append({
                            "timestamp": record.get("timestamp") or "",
                            "from": last_mode,
                            "to": mode,
                            "source": "transcript",
                        })
                    last_mode = mode
                msg = _user_message(record)
                if msg is not None:
                    pending_users.append(msg)
                continue

            # assistant
            msg = record.get("message")
            if not isinstance(msg, dict):
                result.bad_lines += 1
                continue
            model = msg.get("model") or ""
            if model == _SYNTHETIC_MODEL:
                result.synthetic_skipped += 1
                continue
            mid = msg.get("id")
            if not isinstance(mid, str) or not mid:
                # 没有 message.id 的 assistant 对不上任何 raw 轮，按 unmatched 丢弃
                result.synthetic_skipped += 1
                continue
            blocks = _restore_wire_inputs(
                [b for b in (msg.get("content") or []) if isinstance(b, dict)],
                _wire_inputs(record),
            )
            cur = merged.get(mid)
            if cur is None:
                cur = TranscriptMessage(
                    message_id=mid,
                    model=model,
                    timestamp=record.get("timestamp") or "",
                    content=[],
                    stop_reason="",
                    usage={},
                    preceding_users=pending_users,
                )
                pending_users = []
                merged[mid] = cur
                order.append(mid)
            # 同一条消息的拆分记录之间若夹了 user（并行工具的结果先落盘），
            # 这些 user 留在 pending 里，归到下一条 assistant 之前，与 API 顺序一致
            cur.content.extend(blocks)
            if msg.get("stop_reason"):
                cur.stop_reason = msg["stop_reason"]
            if isinstance(msg.get("usage"), dict):
                cur.usage = msg["usage"]

    result.assistants = [merged[m] for m in order]
    result.trailing_users = pending_users
    return result


# ─────────────────────────────────────────────
# 定位 transcript
# ─────────────────────────────────────────────

def _claude_dir(claude_dir: Optional[Path] = None) -> Path:
    if claude_dir:
        return Path(claude_dir).expanduser()
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude"


def find_transcript(
    session_id: str,
    hook_events: Optional[Sequence[Dict]] = None,
    claude_dir: Optional[Path] = None,
) -> Optional[Path]:
    """找会话的 transcript：优先 Stop hook 带回的 transcript_path，其次按 sid 搜 projects/*"""
    for event in reversed(list(hook_events or [])):
        tp = event.get("transcript_path") if isinstance(event, dict) else None
        if isinstance(tp, str) and tp:
            p = Path(tp).expanduser()
            if p.is_file():
                return p
    if not session_id or "/" in session_id or session_id.startswith("."):
        return None
    projects = _claude_dir(claude_dir) / "projects"
    try:
        hits = sorted(projects.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return hits[0] if hits else None


# ─────────────────────────────────────────────
# 补洞
# ─────────────────────────────────────────────

@dataclass
class TranscriptPair:
    """由 transcript 合成的一轮 pair，接口与 merger._AdaptedPair 一致"""
    timestamp: str
    request_body: Dict
    response_body: Dict
    usage: Dict
    stop_reason: str
    is_partial: bool
    model: str
    index: int = 0
    new_messages: List[Dict] = field(default_factory=list)
    is_full_replay: bool = False
    recovered_tool_results: List[Dict] = field(default_factory=list)
    # builder 据此把它当主请求（合成 pair 没有 tools 声明，不能按旁路请求处理）
    from_transcript: bool = True


def _response_id(pair: Any) -> str:
    resp = getattr(pair, "response_body", None) or {}
    rid = resp.get("id") if isinstance(resp, dict) else None
    return rid if isinstance(rid, str) else ""


def _response_tool_ids(pair: Any) -> List[str]:
    resp = getattr(pair, "response_body", None) or {}
    if not isinstance(resp, dict):
        return []
    return [
        b["id"] for b in resp.get("content") or []
        if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use")
        and isinstance(b.get("id"), str) and b["id"]
    ]


def _to_response(msg: TranscriptMessage) -> Dict:
    return {
        "id": msg.message_id,
        "type": "message",
        "role": "assistant",
        "model": msg.model,
        "content": copy.deepcopy(msg.content),
        "stop_reason": msg.stop_reason or None,
        "usage": copy.deepcopy(msg.usage),
        "_complete": bool(msg.stop_reason),
        "_source": "transcript",
    }


def plan_fill(
    pairs: Sequence[Any],
    transcript: Transcript,
    extra_known_ids: Sequence[str] = (),
    allow_anchorless: bool = False,
) -> tuple:
    """计算补洞结果，返回 (new_pairs, report)。没有可补的内容时 report 为 None。

    对齐键（按可靠度）：assistant message.id ↔ raw 响应 id；其次 tool_use_id。
    只补「最后一个能对上的 raw 轮」之后的尾部缺口；没有任何锚点时默认不补
    （防止 resume 分叉的会话把旧历史整段灌进来），离线抢救可用 allow_anchorless。

    pairs 不会被修改；需要补全的 partial pair 会被浅拷贝后替换。
    """
    known_ids = {i for i in (_response_id(p) for p in pairs) if i} | set(extra_known_ids)
    known_tool_ids = {t for p in pairs for t in _response_tool_ids(p)}
    partial_by_id = {
        _response_id(p): i for i, p in enumerate(pairs)
        if getattr(p, "is_partial", False) and _response_id(p)
    }

    matched_flags = []
    for m in transcript.assistants:
        tids = m.tool_use_ids()
        matched_flags.append(
            m.message_id in known_ids or (bool(tids) and all(t in known_tool_ids for t in tids))
        )
    last_matched = max((i for i, ok in enumerate(matched_flags) if ok), default=-1)
    if last_matched < 0 and not allow_anchorless:
        tail: List[TranscriptMessage] = []
        anchorless_skipped = len(transcript.assistants)
    else:
        tail = transcript.assistants[last_matched + 1:]
        anchorless_skipped = 0
    missing_middle = sum(1 for ok in matched_flags[: max(last_matched, 0)] if not ok)

    # partial 补全：raw 被打断、transcript 里同一条消息有 stop_reason
    new_pairs = list(pairs)
    completed: List[str] = []
    for m in transcript.assistants:
        idx = partial_by_id.get(m.message_id)
        if idx is None or not m.stop_reason or not m.content:
            continue
        p = copy.copy(new_pairs[idx])
        p.response_body = _to_response(m)
        p.stop_reason = m.stop_reason
        p.usage = copy.deepcopy(m.usage)
        p.is_partial = False
        new_pairs[idx] = p
        completed.append(m.message_id)

    next_index = max((getattr(p, "index", 0) or 0 for p in pairs), default=0) + 1
    filled: List[str] = []
    for m in tail:
        new_pairs.append(TranscriptPair(
            timestamp=m.timestamp,
            request_body={},
            response_body=_to_response(m),
            usage=copy.deepcopy(m.usage),
            stop_reason=m.stop_reason,
            is_partial=False,
            model=m.model,
            index=next_index,
            new_messages=copy.deepcopy(m.preceding_users),
        ))
        next_index += 1
        filled.append(m.message_id)
    # 最后一轮的工具结果在末尾 user 里，没有下一轮请求承载，走 recovered 通道给 builder 建索引
    if tail and transcript.trailing_users:
        new_pairs[-1].recovered_tool_results = [
            copy.deepcopy(b) for u in transcript.trailing_users
            if isinstance(u.get("content"), list)
            for b in u["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]

    if not filled and not completed:
        return list(pairs), None

    thinking_blocks = sum(
        1 for m in tail for b in m.content if b.get("type") == "thinking"
    )
    report = {
        "transcript_path": transcript.path,
        "filled_pairs": len(filled),
        "filled_message_ids": filled,
        "completed_partial": len(completed),
        "completed_message_ids": completed,
        "missing_middle": missing_middle,
        "anchorless_skipped": anchorless_skipped,
        "transcript_assistants": len(transcript.assistants),
        "unknown_types": dict(transcript.unknown_types),
        "bad_lines": transcript.bad_lines,
        # transcript 里的 thinking 没有 signature，不能用于回放
        "thinking_without_signature": thinking_blocks,
    }
    return new_pairs, report


def fill_pairs_from_transcript(
    session_id: str,
    pairs: Sequence[Any],
    hook_events: Optional[Sequence[Dict]] = None,
    extra_known_ids: Sequence[str] = (),
    claude_dir: Optional[Path] = None,
    allow_anchorless: bool = False,
    transcript_path: Optional[Path] = None,
) -> tuple:
    """一站式补洞：定位 → 解析 → 计算。任何异常只打日志，返回原 pairs。

    返回 (pairs, report, timeline)：report 为 None 表示没补任何东西；
    timeline 是 transcript 的权限模式时间线（调用方在 hook 时间线为空时才用）。
    """
    try:
        path = transcript_path or find_transcript(session_id, hook_events, claude_dir)
        if path is None:
            return list(pairs), None, []
        transcript = parse_transcript(path)
        if transcript.unknown_types:
            logger.debug("transcript %s 含未知 type: %s", session_id[:8], transcript.unknown_types)
        new_pairs, report = plan_fill(
            pairs, transcript, extra_known_ids=extra_known_ids,
            allow_anchorless=allow_anchorless,
        )
        if report:
            logger.info(
                "transcript 补洞: %s | 补 %d 轮，补全 partial %d 轮，中间缺口 %d（未插入）",
                session_id[:8], report["filled_pairs"], report["completed_partial"],
                report["missing_middle"],
            )
        return new_pairs, report, transcript.permission_mode_timeline
    except Exception as e:
        logger.warning("transcript 补洞失败（不影响导出）%s: %s", session_id[:8], e)
        return list(pairs), None, []
