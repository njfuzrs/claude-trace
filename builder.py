#!/usr/bin/env python3
"""
builder.py — 轨迹构建器

从 DataCollector 采集的请求/响应对中构建 SWE-agent 兼容的 .traj 格式。

Anthropic API → TAO 映射：
  content[type=thinking]           → Thought
  content[type=text]               → Thought（补充）
  content[type=tool_use]           → Action
  content[type=server_tool_use]    → Action（服务端工具，如 web_search）
  tool_result（下一请求）           → Observation
  *_tool_result（同一响应内）       → Observation（服务端工具结果）
  stop_reason=end_turn             → final_answer
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("builder")


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class SessionMetadata:
    session_id: str
    start_time: str = ""
    end_time: str = ""
    model: str = ""
    total_api_calls: int = 0
    total_tokens_sent: int = 0
    total_tokens_received: int = 0
    total_cost_usd: float = 0.0     # P2 fix: 估算成本（基于模型定价）
    exit_status: str = ""           # end_turn / tool_use / interrupted / user_interrupt / unknown
    tools_used: List[str] = field(default_factory=list)
    files_edited: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    step_count: int = 0
    has_thinking: bool = False
    has_sub_agent: bool = False
    working_directory: str = ""
    claude_md_hash: str = ""        # P2 fix: CLAUDE.md 内容 hash（用于关联项目）
    # Hook 事件丰富字段
    start_source: str = ""          # startup / resume / clear
    end_source: str = ""
    user_prompts: List[str] = field(default_factory=list)
    compactions: List[Dict] = field(default_factory=list)
    subagent_spans: List[Dict] = field(default_factory=list)
    # git 状态快照（P0：base_commit 锚定的唯一可靠来源）
    # 会话开始时的 HEAD 与工作区脏状态是采集时点独有的信息，事后无法重建：
    # 未合并分支 + squash 合并 + 多 worktree 会让「按时间反查 commit」落到
    # 主线上不存在的中间状态；脏工作区更意味着 HEAD 根本不代表真实起点。
    git_head: str = ""              # 会话开始时的 HEAD commit sha
    git_branch: str = ""            # 会话开始时的分支名
    git_dirty: Optional[bool] = None  # 工作区是否有未提交改动（None = 未采集到）
    git_state: Dict = field(default_factory=dict)      # 会话开始时的完整快照
    git_state_end: Dict = field(default_factory=dict)  # 会话结束时的完整快照


# ─────────────────────────────────────────────
# 模型定价
# ─────────────────────────────────────────────

# 模型定价（USD per million tokens）
# 来源：https://docs.anthropic.com/en/docs/about-claude/pricing
#
# Fix: 原实现只有 claude-opus-4 / sonnet-4 / haiku-4 三条前缀，
# 导致 claude-opus-5 / claude-sonnet-5 / claude-opus-4-8 等新模型
# 全部匹配不到，total_cost_usd 恒为 0。
# 现在按「精确名 → 最长前缀」两级匹配，并覆盖 4.x / 5 全系列。
_MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Opus 系列
    "claude-opus-5": {"input": 15.0, "output": 75.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-opus-3": {"input": 15.0, "output": 75.0},
    # Sonnet 系列
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-3-7-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
    # Haiku 系列
    "claude-haiku-4": {"input": 0.80, "output": 4.0},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.0},
    # Fable
    "claude-fable-5": {"input": 3.0, "output": 15.0},
}

# 长上下文（1M）变体的价格倍率。Anthropic 对超长上下文按溢价计费，
# 这里用保守倍率估算，避免 [1m] 模型成本被低估。
_LONG_CONTEXT_MULTIPLIER = 2.0


def _normalize_model_name(model: str) -> tuple:
    """归一化模型名，返回 (基础名, 是否长上下文变体)

    'claude-opus-4-8[1m]'          → ('claude-opus-4-8', True)
    'claude-haiku-4-5-20251001'    → ('claude-haiku-4-5', False)
    """
    if not model:
        return "", False
    name = model.strip()
    is_long = False
    if name.endswith("[1m]"):
        name = name[: -len("[1m]")]
        is_long = True
    # 去掉日期后缀（-20251001）
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        name = parts[0]
    return name, is_long


def _lookup_pricing(model: str) -> Optional[Dict[str, float]]:
    """按「精确名 → 最长前缀」匹配定价表，未知模型返回 None"""
    name, is_long = _normalize_model_name(model)
    if not name:
        return None

    pricing = _MODEL_PRICING.get(name)
    if pricing is None:
        # 最长前缀匹配，避免 'claude-sonnet-4-6' 被 'claude-opus-4' 之类误匹配
        best_len = 0
        for prefix, p in _MODEL_PRICING.items():
            if name.startswith(prefix) and len(prefix) > best_len:
                pricing, best_len = p, len(prefix)
    if pricing is None:
        return None
    if is_long:
        return {k: v * _LONG_CONTEXT_MULTIPLIER for k, v in pricing.items()}
    return pricing


def _estimate_cost(
    model: str, input_tokens: int, output_tokens: int,
    cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
) -> float:
    """根据模型和 token 用量估算成本（USD）

    Anthropic cache_read 是 input 价格的 10%，cache_creation 是 input 价格的 25%。
    非 Anthropic 模型（deepseek / qwen 等）无定价表，返回 0。
    """
    pricing = _lookup_pricing(model)
    if not pricing:
        if model:
            logger.debug("模型无定价数据，成本按 0 计: %s", model)
        return 0.0
    base_cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    cache_read_cost = cache_read_tokens * pricing["input"] * 0.1 / 1_000_000
    cache_creation_cost = cache_creation_tokens * pricing["input"] * 0.25 / 1_000_000
    return base_cost + cache_read_cost + cache_creation_cost


# ─────────────────────────────────────────────
# 工具名归类
# ─────────────────────────────────────────────

# Fix: 原实现写成 ("write", "edit", "read") 小写，而 Claude Code 实际
# 工具名是 Write / Edit / Read（首字母大写），导致 files_edited 恒为空。
# 现在统一小写比较，并补全各类编辑/读取工具的别名。
_EDIT_TOOLS: Set[str] = {
    "write", "edit", "multiedit", "notebookedit", "notebook_edit",
    "str_replace", "str_replace_editor", "str_replace_based_edit_tool",
    "create", "applypatch", "apply_patch", "update_file", "write_file",
}
_READ_TOOLS: Set[str] = {
    "read", "view", "readfile", "read_file", "notebookread", "notebook_read",
}

# tool_input 中可能承载文件路径的字段名
_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath", "filename", "file")


def _extract_file_path(tool_input) -> str:
    """从 tool_input 中提取文件路径（容忍非 dict 输入）"""
    if not isinstance(tool_input, dict):
        return ""
    for key in _PATH_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


# ─────────────────────────────────────────────
# 内容提取辅助
# ─────────────────────────────────────────────

def _extract_system_text(request_body: Dict) -> str:
    """从 request_body 提取 system prompt 文本（支持字符串和 content block 列表）"""
    system = request_body.get("system")
    if not system:
        return ""
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system
            if isinstance(b, dict) and b.get("text")
        )
    return str(system)


def _extract_claude_md_hash(system_text: str) -> str:
    """从 system prompt 文本计算 hash（用于关联项目）"""
    if not system_text:
        return ""
    return hashlib.md5(system_text.encode()).hexdigest()


def _stringify_result_content(content) -> str:
    """将 tool_result / *_tool_result 的 content 序列化为文本

    Fix: 原实现对所有 block 都取 b.get("text", "")，导致 image / 结构化
    结果（web_search_result 等）被静默丢成空字符串。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            btype = b.get("type")
            if btype == "text":
                parts.append(b.get("text", ""))
            elif btype == "image":
                src = b.get("source", {}) or {}
                parts.append(f"[image: {src.get('media_type', 'unknown')}]")
            else:
                parts.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


def _msg_fingerprint(msg: Dict) -> str:
    """消息内容指纹，用于识别 compaction 导致的历史重放"""
    try:
        payload = json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(msg)
    return hashlib.md5(payload.encode()).hexdigest()


def _strip_tool_results(msg: Dict) -> Optional[Dict]:
    """剔除 user message 中的 tool_result 块

    tool_result 由 observation 通道按 tool_use_id 单独记录，
    如果同时保留在 user message 里会造成 history 中每个 tool_result 出现两次。
    剔除后内容为空则返回 None（整条消息不记录）。
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return msg
    kept = [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") == "tool_result")
    ]
    if not kept:
        return None
    if len(kept) == len(content):
        return msg
    return {**msg, "content": kept}


# ─────────────────────────────────────────────
# Hook 事件 → metadata
# ─────────────────────────────────────────────

# git_state 模块提供规范化能力。软导入：builder 被 merger / rebuild_trajs 等
# 多处 import，缺失该模块时应降级而非整体 import 失败。
try:
    from git_state import coerce_git_state as _coerce_git_state_impl
except Exception:  # pragma: no cover
    _coerce_git_state_impl = None


def _coerce_git_state(payload: Dict) -> Dict:
    """从 hook 事件中提取 git 状态快照，兼容规范结构与扁平结构

    hook 事件里的形态有两种：
      - 新版 collector：event["git_state"] 为规范结构（键为 head/branch/dirty）
      - 旧版 collector：git_* 扁平字段直接铺在 event 顶层
    """
    if not isinstance(payload, dict):
        return {}
    candidate = payload.get("git_state")
    if not isinstance(candidate, dict) or not candidate:
        # 退回顶层扁平字段
        candidate = {
            k: v for k, v in payload.items()
            if k.startswith("git_") and k != "git_state"
        }
    if not candidate:
        return {}
    if _coerce_git_state_impl is not None:
        return _coerce_git_state_impl(candidate)
    # 无 git_state 模块时的极简兜底：只保留能直接用的字段
    head = candidate.get("head") or candidate.get("git_head") or ""
    if not head:
        return {}
    return {
        "head": head,
        "head_short": head[:12],
        "branch": candidate.get("branch") or candidate.get("git_branch") or "",
        "dirty": candidate.get("dirty", candidate.get("git_dirty")),
    }


# ─────────────────────────────────────────────
# 工具退出码推导（P0）
# ─────────────────────────────────────────────

# Bash 失败时，Claude Code 会在 tool_result 文本首行写 "Exit code N"。
# 实测（4528 个 Bash tool_result）：成功 = 无前缀 + is_error=False（4445 例）；
# 失败 = 有前缀 + is_error=True（72 例）；另有 11 例 is_error 但无前缀
# （用户拒绝执行 / 超时 / 被中断）。"Exit code 0" 从不出现。
_EXIT_CODE_RE = re.compile(r"^\s*Exit code[:\s]+(\d+)", re.IGNORECASE)

# 权限拒绝 / 中断 / 超时的判定文本。这些不是命令的退出码，
# 而是命令根本没跑完，必须与「真的跑了并返回非零」区分开。
_REJECTED_MARKERS = (
    "the user doesn't want to proceed",
    "the user doesn't want to take this action",
    "tool use was rejected",
    "operation was aborted",
    "request was aborted",
)
_INTERRUPT_MARKERS = (
    "interrupted by user",
    "user interrupted",
    "canceled by user",
    "cancelled by user",
)
_TIMEOUT_MARKERS = (
    "timed out after",
    "command timed out",
)
# InputValidationError 等：工具调用本身不合法，命令未执行
_INVALID_MARKERS = ("<tool_use_error>",)

# 会真正产生 shell 退出码的工具
_SHELL_TOOLS = {"bash", "bashoutput"}


def derive_tool_outcome(tool_name: str, result_text: str, is_error: bool) -> Dict:
    """从 tool_result 推导结构化执行结果

    为什么必须在这里做推导，而不是从 hook 里读一个字段：
    Claude Code **没有**在任何位置暴露数值退出码 —— PostToolUse 的
    tool_response 只有 stdout/stderr/interrupted/isImage/noOutputExpected，
    transcript 的 toolUseResult 同样没有 returnCode 字段（已核实）。唯一可靠
    信号是 API 侧 tool_result 文本首行的 "Exit code N" 前缀 + is_error 标记。

    Returns:
        {
          "exit_code": int | None,   # 数值退出码；无法确定为 None
          "status": str,             # success / failure / rejected / interrupted
                                     # / timeout / invalid_input
          "exit_code_source": str,   # 该结论的依据，便于下游评估可信度
        }
    """
    text = result_text or ""
    head = text[:400]          # 判定标记都在开头，避免扫描超长输出
    lower = head.lower()
    lname = (tool_name or "").lower()

    # 1) 显式退出码前缀 —— 最强信号
    m = _EXIT_CODE_RE.match(head)
    if m:
        code = int(m.group(1))
        return {
            "exit_code": code,
            "status": "success" if code == 0 else "failure",
            "exit_code_source": "exit_code_prefix",
        }

    # 2) <tool_use_error> 是 Claude Code 包裹的工具层错误标记，
    #    不会出现在正常命令输出里，可独立判定。
    if any(k in lower for k in _INVALID_MARKERS):
        return {"exit_code": None, "status": "invalid_input", "exit_code_source": "tool_use_error"}

    # 3) 命令未真正执行的几类情形（拒绝 / 超时 / 中断）。
    #    关键：这些文本标记只在 is_error=True 时才作数。
    #    实测发现的误判类：命令成功执行，但 stdout 里本身含有 "timed out" /
    #    "interrupted by user" 等字样（脚本自己打印的日志、grep 到的文本），
    #    若不加 is_error 前置条件，会把成功的命令误判成超时/中断。
    if is_error:
        if any(k in lower for k in _REJECTED_MARKERS):
            return {"exit_code": None, "status": "rejected", "exit_code_source": "rejection_text"}
        if any(k in lower for k in _TIMEOUT_MARKERS):
            return {"exit_code": None, "status": "timeout", "exit_code_source": "timeout_text"}
        if any(k in lower for k in _INTERRUPT_MARKERS):
            return {"exit_code": None, "status": "interrupted", "exit_code_source": "interrupt_text"}
        # is_error=True 但没有任何可辨识标记：确定失败，退出码未知
        return {"exit_code": None, "status": "failure", "exit_code_source": "is_error_flag"}

    # 4) shell 类工具：无 "Exit code" 前缀 + 非 error 即成功退出 0。
    #    实测 4445/4445 成立 —— Claude Code 只在非零时写前缀。
    if lname in _SHELL_TOOLS:
        return {"exit_code": 0, "status": "success", "exit_code_source": "no_error_shell"}

    # 5) 非 shell 工具（Read/Edit/Write 等）没有退出码概念，只报成功
    return {"exit_code": None, "status": "success", "exit_code_source": "no_error_nonshell"}


def hook_event_name(event: Dict) -> str:
    """读取 hook 事件名

    collector.py 写出的字段是 "event"，但 Claude Code 原始 payload 里叫
    "hook_event_name"。历史数据两种都存在过，这里统一兼容。
    """
    return event.get("event") or event.get("hook_event_name") or ""


def apply_hook_events_to_metadata(metadata: SessionMetadata, hook_events: List[Dict]) -> None:
    """从 hook events 提取语义信息，就地丰富 metadata

    Fix: proxy 和 merger 各有一份几乎相同的提取逻辑，且都只认旧事件名
    （PostCompact / SubagentStart / SubagentStop），漏掉了新版 Claude Code 的
    PreCompact、以及 SessionStart 带来的 cwd/model 补全。现在统一到这里。
    """
    if not hook_events:
        return

    by_name: Dict[str, List[Dict]] = {}
    for e in hook_events:
        by_name.setdefault(hook_event_name(e), []).append(e)

    prompts = [
        e["prompt"] for e in by_name.get("UserPromptSubmit", [])
        if e.get("prompt")
    ]
    if prompts:
        metadata.user_prompts = prompts

    # PreCompact 与 PostCompact 都算一次压缩事件
    compactions = by_name.get("PreCompact", []) + by_name.get("PostCompact", [])
    if compactions:
        metadata.compactions = compactions

    spans = by_name.get("SubagentStart", []) + by_name.get("SubagentStop", [])
    if spans:
        metadata.subagent_spans = spans
        metadata.has_sub_agent = True

    session_start = (by_name.get("SessionStart") or [{}])[0]
    session_end = (by_name.get("SessionEnd") or [{}])[0]
    if not metadata.start_source:
        metadata.start_source = session_start.get("source", "") or ""
    if not metadata.end_source:
        metadata.end_source = session_end.get("source", "") or ""
    # cwd / model 兜底补全：SessionStart 未必先于首个 API 请求到达
    if not metadata.working_directory:
        for e in hook_events:
            if e.get("cwd"):
                metadata.working_directory = e["cwd"]
                break
    if not metadata.model and session_start.get("model"):
        metadata.model = session_start["model"]

    # git 状态兜底：优先用代理侧已带入的（实时采集，最准），否则从 hook 事件补。
    # 这条路径对「离线重建」（rebuild_trajs / merger 读 events.jsonl）是唯一来源。
    if not metadata.git_state:
        start_state = _coerce_git_state(session_start)
        if start_state:
            metadata.git_state = start_state
    if not metadata.git_state_end:
        end_state = _coerce_git_state(session_end)
        if end_state:
            metadata.git_state_end = end_state
    # 扁平字段从起点快照回填，供直接筛选
    if metadata.git_state:
        if not metadata.git_head:
            metadata.git_head = metadata.git_state.get("head", "") or ""
        if not metadata.git_branch:
            metadata.git_branch = metadata.git_state.get("branch", "") or ""
        if metadata.git_dirty is None:
            metadata.git_dirty = metadata.git_state.get("dirty")


# ─────────────────────────────────────────────
# tool_result 索引
# ─────────────────────────────────────────────

def build_tool_result_index(pairs: List) -> Dict[str, Dict]:
    """预建 tool_use_id → tool_result 的全局索引

    Fix: 原实现 find_tool_result 用 max_lookahead=3 的滑动窗口向后查找。
    sub-agent 请求插入主会话序列、或长耗时工具（Bash / Agent）把 tool_result
    推到 3 个 pair 之外时就找不到，实测 orphan 率均值 30%、最坏 100%——
    数据其实都采到了，只是构建阶段没匹配上。
    改为一次性全量扫描建索引：O(n) 且无遗漏。

    同时扫描两处来源：
      - request_body["messages"]：代理实时路径 / raw.jsonl 首条基线记录
      - pair.new_messages：raw.jsonl 增量记录（无完整 messages 字段）
    """
    index: Dict[str, Dict] = {}

    def scan(messages) -> None:
        if not messages:
            return
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                if tid and tid not in index:
                    index[tid] = block

    def scan_blocks(blocks) -> None:
        """直接扫描 tool_result 块列表（_recovered_tool_results 兜底通道）"""
        for block in blocks or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if tid and tid not in index:
                index[tid] = block

    for pair in pairs:
        scan((pair.request_body or {}).get("messages"))
        scan(getattr(pair, "new_messages", None))
        # 代理落盘时补齐的、增量边界外的 tool_result
        scan_blocks(getattr(pair, "recovered_tool_results", None))

    return index


def build_server_result_index(pairs: List) -> Dict[str, Dict]:
    """预建服务端工具结果索引（web_search_tool_result 等）

    服务端工具（server_tool_use）的结果在同一次响应的 content 数组里就返回了，
    不会出现在下一个请求的 messages 中，因此需要单独扫描 response content。
    """
    index: Dict[str, Dict] = {}
    for pair in pairs:
        response = pair.response_body or {}
        for block in response.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type") or ""
            if btype.endswith("_tool_result") and btype != "tool_result":
                tid = block.get("tool_use_id")
                if tid and tid not in index:
                    index[tid] = block
    return index


def find_tool_result(pairs: List, tool_use_id: str, max_lookahead: Optional[int] = None) -> Optional[Dict]:
    """在后续请求的 messages 中查找对应的 tool_result

    保留此函数供外部调用者向后兼容。max_lookahead 参数已废弃并忽略——
    限制查找窗口是 orphan 率过高的根因，现在总是全量查找。
    内部构建流程请直接用 build_tool_result_index 避免重复扫描。
    """
    if max_lookahead is not None:
        logger.debug("find_tool_result: max_lookahead 参数已废弃，改为全量查找")
    return build_tool_result_index(pairs).get(tool_use_id)


# ─────────────────────────────────────────────
# 轨迹构建
# ─────────────────────────────────────────────

def build_trajectory(_session_id: str, pairs: List, metadata: SessionMetadata) -> Dict:
    """构建 SWE-agent 兼容的 .traj 格式

    输出结构：
      trajectory  — TAO 步骤列表（action/observation 对）
      history     — 完整 LLM 对话历史（用于 SFT 训练）
      info        — 会话统计信息 + 数据质量指标
      metadata    — 扩展元数据（双通道数据）
    """
    trajectory: List[Dict] = []
    history: List[Dict] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_creation_tokens = 0
    tools_used: Set[str] = set()
    files_edited: Set[str] = set()
    files_read: Set[str] = set()
    has_thinking = False

    # 数据质量计数
    n_tool_actions = 0
    n_orphan = 0
    n_partial_pairs = 0
    n_dup_user_dropped = 0
    n_dup_result_dropped = 0
    n_error_responses = 0
    n_failed_actions = 0       # 非 success 的工具执行数
    n_exit_codes_known = 0     # 拿到确定数值退出码的工具执行数

    # 全局索引：一次扫描，避免 O(n*m) 且不遗漏
    tool_result_index = build_tool_result_index(pairs)
    server_result_index = build_server_result_index(pairs)

    # system prompt 与 messages 基线
    system_text = ""
    baseline_recorded = False
    seen_user_fps: Set[str] = set()
    seen_result_ids: Set[str] = set()
    last_timestamp = ""

    for pair in pairs:
        response = pair.response_body or {}

        # Fix: system prompt 原来只在 pair_idx == 0 时提取，而 raw.jsonl 的首条
        # 记录经常不是 index=1（partial 或代理重启导致首个 pair 未落盘），
        # 实测 40% 的会话 history 里没有 system。现在从任意第一个带 system 的
        # pair 提取，并在最后插到 history 头部。
        if not system_text:
            system_text = _extract_system_text(pair.request_body or {})

        if pair.timestamp:
            last_timestamp = pair.timestamp
        if getattr(pair, "is_partial", False):
            n_partial_pairs += 1
        if isinstance(response.get("error"), dict) or "error" in response:
            n_error_responses += 1

        # ── history：记录 user 侧消息 ──────────────────────
        # 首个带完整 messages 的 pair 作为基线，其余用增量 new_messages。
        request_body = pair.request_body or {}
        if not baseline_recorded and request_body.get("messages"):
            source_msgs = request_body["messages"]
            baseline_recorded = True
        else:
            source_msgs = getattr(pair, "new_messages", None) or []

        user_msgs = [
            m for m in source_msgs
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        # Fix: 无法定位增量边界时 proxy 会返回完整历史，导致同一批 user 消息
        # 被反复写进 history（实测一个会话里同一个 tool_result 出现几十次，
        # 最坏 0 个 tool_use 对应 570 个 tool_result 块）。
        # 优先用 proxy 落盘的 is_full_replay 显式标记；历史数据没有该字段时
        # 退回内容指纹启发式（多条消息中出现已记录过的 → 判定为重放）。
        # 单条消息（正常的一轮用户输入）不做去重，避免误删重复的 "继续"。
        fingerprints = [_msg_fingerprint(m) for m in user_msgs]
        is_replay = bool(getattr(pair, "is_full_replay", False)) or (
            len(user_msgs) >= 2 and any(fp in seen_user_fps for fp in fingerprints)
        )

        # strict=True：fingerprints 由 user_msgs 逐条推导，长度必然相等；
        # 若将来改动破坏这个不变量，宁可报错也不要静默截断掉尾部消息。
        for msg, fp in zip(user_msgs, fingerprints, strict=True):
            if is_replay and fp in seen_user_fps:
                n_dup_user_dropped += 1
                continue
            seen_user_fps.add(fp)
            trimmed = _strip_tool_results(msg)
            if trimmed is None:
                continue
            history.append({
                "role": "user",
                "content": trimmed.get("content", ""),
                "agent": "primary",
            })

        if not response:
            continue

        content_blocks = [b for b in response.get("content", []) or [] if isinstance(b, dict)]
        stop_reason = response.get("stop_reason", "") or ""
        usage = response.get("usage", {}) or {}

        total_input_tokens += usage.get("input_tokens", 0)
        total_output_tokens += usage.get("output_tokens", 0)
        total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)

        # ── 提取 Thought ──────────────────────────────────
        thought_parts = []
        thinking_blocks = []
        for block in content_blocks:
            btype = block.get("type")
            if btype == "thinking":
                thought_parts.append(block.get("thinking", ""))
                thinking_blocks.append(block)
                has_thinking = True
            elif btype == "redacted_thinking":
                thinking_blocks.append(block)
                has_thinking = True
            elif btype == "text":
                thought_parts.append(block.get("text", ""))
        thought = "\n".join(p for p in thought_parts if p)

        # ── Action blocks：tool_use + server_tool_use ─────
        # Fix: 原实现只识别 tool_use，server_tool_use（web_search 等服务端工具）
        # 及其结果被静默丢弃。
        action_blocks = [
            b for b in content_blocks
            if b.get("type") in ("tool_use", "server_tool_use")
        ]
        has_tool_use = bool(action_blocks)

        # ── history：记录 assistant 消息 ──────────────────
        history.append({
            "role": "assistant",
            "content": content_blocks,
            "message_type": "action" if has_tool_use else "thought",
            "agent": "primary",
            "thought": thought,
            "thinking_blocks": thinking_blocks if thinking_blocks else None,
            "tool_calls": [
                {"function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}}
                for b in action_blocks
            ] or None,
            "usage": usage,
            "stop_reason": stop_reason,
            "timestamp": pair.timestamp,
        })

        # ── Action / Observation 步骤 ─────────────────────
        for tool_idx, block in enumerate(action_blocks):
            tool_name = block.get("name", "")
            tool_input = block.get("input", {}) or {}
            tool_use_id = block.get("id", "")
            is_server_side = block.get("type") == "server_tool_use"
            tools_used.add(tool_name)
            n_tool_actions += 1

            # 提取涉及的文件路径
            lname = tool_name.lower()
            fp_path = _extract_file_path(tool_input)
            if fp_path:
                if lname in _EDIT_TOOLS:
                    files_edited.add(fp_path)
                elif lname in _READ_TOOLS:
                    files_read.add(fp_path)

            action_str = f"{tool_name}({json.dumps(tool_input, ensure_ascii=False)})"

            # 只在第一个 tool_use 中关联 thought，避免多工具调用时重复
            step_thought = thought if tool_idx == 0 else ""
            content_str = (
                step_thought
                + f"\n\nTool: {tool_name}\nInput: {json.dumps(tool_input, ensure_ascii=False)}"
            ).strip()

            action_step = {
                "message_type": "action",
                "role": "assistant",
                "content": content_str,
                "thought": step_thought,
                "action": action_str,
                "agent": "primary",
                "timestamp": pair.timestamp,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            if is_server_side:
                action_step["_server_side"] = True
            trajectory.append(action_step)

            # ── Observation ──────────────────────────────
            tool_result = tool_result_index.get(tool_use_id)
            server_result = server_result_index.get(tool_use_id) if is_server_side else None

            if tool_result is not None:
                obs_content = _stringify_result_content(tool_result.get("content"))
                obs_is_error = bool(tool_result.get("is_error", False))
                # P0：结构化执行结果。「改前失败 / 改后通过」的天然证据，
                # F2P 判定可直接从轨迹取，不必靠解析文本重新构造。
                outcome = derive_tool_outcome(tool_name, obs_content, obs_is_error)
                if outcome["status"] != "success":
                    n_failed_actions += 1
                if outcome["exit_code"] is not None:
                    n_exit_codes_known += 1
                trajectory.append({
                    "message_type": "observation",
                    "role": "user",
                    "content": obs_content,
                    "agent": "primary",
                    "is_error": obs_is_error,
                    "tool_use_id": tool_use_id,
                    "exit_code": outcome["exit_code"],
                    "status": outcome["status"],
                    "exit_code_source": outcome["exit_code_source"],
                })
                # history：tool_result 只记录一次（user message 侧已剔除）
                if tool_use_id in seen_result_ids:
                    n_dup_result_dropped += 1
                else:
                    seen_result_ids.add(tool_use_id)
                    history.append({
                        "role": "user",
                        "content": [tool_result],
                        "message_type": "observation",
                        "agent": "primary",
                        "tool_call_ids": [tool_use_id],
                    })
            elif server_result is not None:
                obs_content = _stringify_result_content(server_result.get("content"))
                # 服务端工具（web_search 等）无 shell 退出码，只标注状态
                trajectory.append({
                    "message_type": "observation",
                    "role": "user",
                    "content": obs_content,
                    "agent": "primary",
                    "is_error": False,
                    "tool_use_id": tool_use_id,
                    "_server_side": True,
                    "exit_code": None,
                    "status": "success",
                    "exit_code_source": "server_tool",
                })
                if tool_use_id in seen_result_ids:
                    n_dup_result_dropped += 1
                else:
                    seen_result_ids.add(tool_use_id)
                    history.append({
                        "role": "user",
                        "content": [server_result],
                        "message_type": "observation",
                        "agent": "primary",
                        "tool_call_ids": [tool_use_id],
                    })
            else:
                # 全量索引后仍找不到 = 真正的孤儿（会话中断在工具执行途中）
                n_orphan += 1
                trajectory.append({
                    "message_type": "observation",
                    "role": "user",
                    "content": "[tool_result not found - session may have ended mid-execution]",
                    "agent": "primary",
                    "is_error": False,
                    "tool_use_id": tool_use_id,
                    "_orphan": True,
                    # 结果本身缺失：既不能说成功也不能说失败，状态为 unknown。
                    # 下游做 F2P 判定时必须排除这类步骤，不能当成功计。
                    "exit_code": None,
                    "status": "unknown",
                    "exit_code_source": "orphan",
                })

        # ── final_answer（纯文本回复，无工具调用） ──────────
        # Fix: 原条件要求 stop_reason == "end_turn"，但 SSE 中断的 pair
        # stop_reason 为空，导致最后一轮纯文本回复不生成 final_answer。
        # 现在放宽到所有非 tool_use 的终止原因。
        if thought and not has_tool_use and stop_reason != "tool_use":
            trajectory.append({
                "message_type": "action",
                "role": "assistant",
                "content": thought,
                "thought": thought,
                "action": "final_answer",
                "agent": "primary",
                "timestamp": pair.timestamp,
                "stop_reason": stop_reason,
            })

    # ── system prompt 插到 history 头部 ────────────────────
    if system_text:
        history.insert(0, {
            "role": "system",
            "content": system_text,
            "agent": "primary",
        })

    # ── 统计 ──────────────────────────────────────────────
    metadata.total_api_calls = len(pairs)
    metadata.total_tokens_sent = total_input_tokens
    metadata.total_tokens_received = total_output_tokens
    metadata.total_cost_usd = _estimate_cost(
        metadata.model, total_input_tokens, total_output_tokens,
        total_cache_read_tokens, total_cache_creation_tokens,
    )
    metadata.tools_used = sorted(tools_used)
    metadata.files_edited = sorted(files_edited)
    metadata.files_read = sorted(files_read)
    metadata.step_count = len(trajectory)
    metadata.has_thinking = has_thinking

    # Fix: 原来 end_time 取 datetime.now()，重建历史数据时会被写成重建时间。
    # 改为取最后一个 pair 的时间戳，只在完全没有时间戳时回退到当前时间。
    if not metadata.end_time:
        metadata.end_time = last_timestamp or datetime.now().isoformat()
    if not metadata.start_time and pairs:
        metadata.start_time = pairs[0].timestamp or ""

    if not metadata.claude_md_hash:
        metadata.claude_md_hash = _extract_claude_md_hash(system_text)

    # exit_status：取最后一个有 stop_reason 的 pair
    if pairs:
        last_stop = ""
        for p in reversed(pairs):
            if p.stop_reason:
                last_stop = p.stop_reason
                break
        if last_stop:
            metadata.exit_status = last_stop
        elif n_partial_pairs:
            # 全程没拿到 stop_reason 且存在 partial 响应 = 流被打断
            metadata.exit_status = "interrupted"
        elif n_error_responses:
            metadata.exit_status = "error"
        else:
            metadata.exit_status = "unknown"

    data_quality = {
        "tool_actions": n_tool_actions,
        "orphan_observations": n_orphan,
        "orphan_rate": round(n_orphan / n_tool_actions, 4) if n_tool_actions else 0.0,
        "partial_pairs": n_partial_pairs,
        "error_responses": n_error_responses,
        "duplicate_user_messages_dropped": n_dup_user_dropped,
        "duplicate_tool_results_dropped": n_dup_result_dropped,
        "has_system_prompt": bool(system_text),
        "has_pricing_data": _lookup_pricing(metadata.model) is not None,
        # 工具执行结果统计（P0）：failed_actions 是「改前失败 / 改后通过」的
        # 直接依据；exit_codes_known 反映有多少步骤拿到了确定的数值退出码。
        "failed_actions": n_failed_actions,
        "exit_codes_known": n_exit_codes_known,
        "failure_rate": round(n_failed_actions / n_tool_actions, 4) if n_tool_actions else 0.0,
        "has_git_state": bool(metadata.git_state),
    }

    return {
        "trajectory": trajectory,
        "history": history,
        "info": {
            "model_stats": {
                "tokens_sent": total_input_tokens,
                "tokens_received": total_output_tokens,
                "cache_read_tokens": total_cache_read_tokens,
                "cache_creation_tokens": total_cache_creation_tokens,
                "api_calls": len(pairs),
                "total_cost_usd": metadata.total_cost_usd,
            },
            "exit_status": metadata.exit_status,
            "has_thinking": has_thinking,
            "data_quality": data_quality,
        },
        "metadata": {
            "session_id": metadata.session_id,
            "model": metadata.model,
            "start_time": metadata.start_time,
            "end_time": metadata.end_time,
            "total_steps": len(trajectory),
            "total_api_calls": metadata.total_api_calls,
            "total_tokens_sent": total_input_tokens,
            "total_tokens_received": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "total_tokens": total_input_tokens + total_output_tokens + total_cache_read_tokens + total_cache_creation_tokens,
            "total_cost_usd": metadata.total_cost_usd,
            "exit_status": metadata.exit_status,
            "tools_used": metadata.tools_used,
            "files_edited": metadata.files_edited,
            "files_read": metadata.files_read,
            "has_thinking": has_thinking,
            "has_sub_agent": metadata.has_sub_agent,
            "working_directory": metadata.working_directory,
            "claude_md_hash": metadata.claude_md_hash,
            "start_source": metadata.start_source,
            "end_source": metadata.end_source,
            "user_prompts": metadata.user_prompts,
            "compactions": metadata.compactions,
            "subagent_spans": metadata.subagent_spans,
            # git 状态（P0）：扁平字段便于直接筛选，完整快照供精细分析。
            # git_head + git_dirty 决定 base_commit 是否可信：
            # 脏工作区意味着 HEAD 不代表会话的真实起点。
            "git_head": metadata.git_head,
            "git_branch": metadata.git_branch,
            "git_dirty": metadata.git_dirty,
            "git_state": metadata.git_state,
            "git_state_end": metadata.git_state_end,
            "data_quality": data_quality,
        },
    }


# ─────────────────────────────────────────────
# 增量保存
# ─────────────────────────────────────────────

def save_trajectory(traj_path: Path, traj: Dict):
    """覆盖写入 .traj 文件（每次记录后调用，保持最新状态）"""
    traj_path.parent.mkdir(parents=True, exist_ok=True)
    traj_path.write_text(json.dumps(traj, ensure_ascii=False, indent=2))
