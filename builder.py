#!/usr/bin/env python3
"""
builder.py — 轨迹构建器

从 DataCollector 采集的请求/响应对中构建 SWE-agent 兼容的 .traj 格式。

Anthropic API → TAO 映射：
  content[type=thinking]  → Thought
  content[type=text]      → Thought（补充）
  content[type=tool_use]  → Action
  tool_result（下一请求）  → Observation
  stop_reason=end_turn    → final_answer
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


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
    exit_status: str = ""           # end_turn / tool_use_loop / user_interrupt / error
    tools_used: List[str] = field(default_factory=list)
    files_edited: List[str] = field(default_factory=list)
    step_count: int = 0
    has_thinking: bool = False
    has_sub_agent: bool = False
    working_directory: str = ""
    # Hook 事件丰富字段
    start_source: str = ""          # startup / resume / clear
    end_source: str = ""
    user_prompts: List[str] = field(default_factory=list)
    compactions: List[Dict] = field(default_factory=list)
    subagent_spans: List[Dict] = field(default_factory=list)


# ─────────────────────────────────────────────
# tool_result 查找
# ─────────────────────────────────────────────

def find_tool_result(pairs: List, tool_use_id: str) -> Optional[Dict]:
    """在后续请求的 messages 中查找对应的 tool_result

    Claude Code 每次请求都带完整对话历史，tool_result 出现在
    tool_use 之后的某个请求的 messages 里。
    """
    for pair in pairs:
        messages = pair.request_body.get("messages", [])
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == tool_use_id
                    ):
                        return block
    return None


# ─────────────────────────────────────────────
# 轨迹构建
# ─────────────────────────────────────────────

def build_trajectory(_session_id: str, pairs: List, metadata: SessionMetadata) -> Dict:
    """构建 SWE-agent 兼容的 .traj 格式

    输出结构：
      trajectory  — TAO 步骤列表（action/observation 对）
      history     — 完整 LLM 对话历史（用于 SFT 训练）
      info        — 会话统计信息
      metadata    — 扩展元数据（双通道数据）
    """
    trajectory = []
    history = []
    total_input_tokens = 0
    total_output_tokens = 0
    tools_used = set()
    files_edited = set()
    has_thinking = False

    for pair_idx, pair in enumerate(pairs):
        response = pair.response_body
        if not response:
            continue

        content_blocks = response.get("content", [])
        stop_reason = response.get("stop_reason", "")
        usage = response.get("usage", {})

        total_input_tokens += usage.get("input_tokens", 0)
        total_output_tokens += usage.get("output_tokens", 0)

        # ── 提取 Thought ──────────────────────────────────
        thought_parts = []
        thinking_blocks = []
        for block in content_blocks:
            if block.get("type") == "thinking":
                thought_parts.append(block.get("thinking", ""))
                thinking_blocks.append(block)
                has_thinking = True
            elif block.get("type") == "text":
                thought_parts.append(block.get("text", ""))
        thought = "\n".join(p for p in thought_parts if p)

        # ── history：记录 assistant 消息 ──────────────────
        history.append({
            "role": "assistant",
            "content": content_blocks,
            "message_type": "action" if any(b.get("type") == "tool_use" for b in content_blocks) else "thought",
            "agent": "primary",
            "thought": thought,
            "thinking_blocks": thinking_blocks if thinking_blocks else None,
            "tool_calls": [
                {"function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                for b in content_blocks if b.get("type") == "tool_use"
            ] or None,
            "usage": usage,
            "stop_reason": stop_reason,
            "timestamp": pair.timestamp,
        })

        # ── Action 步骤（tool_use blocks） ────────────────
        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue

            tool_name = block.get("name", "")
            tool_input = block.get("input", {})
            tool_use_id = block.get("id", "")
            tools_used.add(tool_name)

            # 提取编辑的文件（write/edit/read 工具）
            if tool_name in ("write", "edit", "read"):
                fp = tool_input.get("file_path") or tool_input.get("path", "")
                if fp:
                    files_edited.add(fp)

            action_str = f"{tool_name}({json.dumps(tool_input, ensure_ascii=False)})"

            trajectory.append({
                "message_type": "action",
                "role": "assistant",
                "content": (thought + f"\n\nTool: {tool_name}\nInput: {json.dumps(tool_input, ensure_ascii=False)}").strip(),
                "thought": thought,
                "action": action_str,
                "agent": "primary",
                "timestamp": pair.timestamp,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            })

            # ── Observation（tool_result） ─────────────────
            tool_result = find_tool_result(pairs[pair_idx + 1:], tool_use_id)
            if tool_result:
                obs_content = tool_result.get("content", "")
                if isinstance(obs_content, list):
                    obs_content = "\n".join(
                        b.get("text", "") for b in obs_content if isinstance(b, dict)
                    )

                trajectory.append({
                    "message_type": "observation",
                    "role": "user",
                    "content": obs_content,
                    "agent": "primary",
                    "is_error": tool_result.get("is_error", False),
                    "tool_use_id": tool_use_id,
                })

                # history：记录 tool_result
                history.append({
                    "role": "user",
                    "content": [tool_result],
                    "message_type": "observation",
                    "agent": "primary",
                    "tool_call_ids": [tool_use_id],
                })

        # ── final_answer（end_turn + 有文本回复） ──────────
        if stop_reason == "end_turn" and thought:
            trajectory.append({
                "message_type": "action",
                "role": "assistant",
                "content": thought,
                "thought": thought,
                "action": "final_answer",
                "agent": "primary",
                "timestamp": pair.timestamp,
            })

    # ── 统计 ──────────────────────────────────────────────
    metadata.total_api_calls = len(pairs)
    metadata.total_tokens_sent = total_input_tokens
    metadata.total_tokens_received = total_output_tokens
    metadata.tools_used = sorted(tools_used)
    metadata.files_edited = sorted(files_edited)
    metadata.step_count = len(trajectory)
    metadata.has_thinking = has_thinking
    metadata.end_time = datetime.now().isoformat()

    # exit_status：取最后一个 pair 的 stop_reason
    if pairs:
        last_stop = pairs[-1].stop_reason
        metadata.exit_status = last_stop if last_stop else "unknown"

    return {
        "trajectory": trajectory,
        "history": history,
        "info": {
            "model_stats": {
                "tokens_sent": total_input_tokens,
                "tokens_received": total_output_tokens,
                "api_calls": len(pairs),
            },
            "exit_status": metadata.exit_status,
            "has_thinking": has_thinking,
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
            "exit_status": metadata.exit_status,
            "tools_used": metadata.tools_used,
            "files_edited": metadata.files_edited,
            "has_thinking": has_thinking,
            "has_sub_agent": metadata.has_sub_agent,
            "working_directory": metadata.working_directory,
            "start_source": metadata.start_source,
            "end_source": metadata.end_source,
            "user_prompts": metadata.user_prompts,
            "compactions": metadata.compactions,
            "subagent_spans": metadata.subagent_spans,
        },
    }


# ─────────────────────────────────────────────
# 增量保存
# ─────────────────────────────────────────────

def save_trajectory(traj_path: Path, traj: Dict):
    """覆盖写入 .traj 文件（每次记录后调用，保持最新状态）"""
    traj_path.parent.mkdir(parents=True, exist_ok=True)
    traj_path.write_text(json.dumps(traj, ensure_ascii=False, indent=2))


def append_raw_jsonl(raw_path: Path, pair) -> None:
    """追加写入原始 JSONL（一行一个请求/响应对）"""
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": pair.timestamp,
        "model": pair.model,
        "request": pair.request_body,
        "response": pair.response_body,
        "usage": pair.usage,
        "stop_reason": pair.stop_reason,
        "is_partial": pair.is_partial,
    }
    with open(raw_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
