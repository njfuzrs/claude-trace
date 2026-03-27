#!/usr/bin/env python3
"""
import_codex.py — 导入 Codex CLI 会话到统一轨迹格式

采集策略：
1. 主通道：通过 `codex app-server` 的 `thread/list` + `thread/read` 获取官方语义线程结构
2. 补通道：读取 `~/.codex/sessions/.../rollout-*.jsonl`，补全 system/developer 指令、token 用量、
   旧版本会话的工具调用轨迹，以及原始事件文件
3. 异常兜底：读取 `state_5.sqlite` / `logs_1.sqlite` 中按 thread_id 过滤的日志，保留异常和索引信息

输出目录：
  trajectories/sessions/{thread_id}/
    ├── session.traj
    ├── codex_thread.json
    ├── rollout.jsonl
    ├── state_logs.jsonl
    └── feedback_logs.jsonl
"""

import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from builder import save_trajectory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import-codex")


ALL_SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]

ACTION_ITEM_TYPES = {
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "collabAgentToolCall",
    "webSearch",
    "imageView",
    "imageGeneration",
}

ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<thread_id>.+)\.jsonl$"
)
WATCH_STATE_VERSION = 1
SESSION_ARTIFACTS = (
    "session.traj",
    "codex_thread.json",
    "rollout.jsonl",
    "state_logs.jsonl",
    "feedback_logs.jsonl",
)


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _iso_from_unix(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _join_text_blocks(blocks: List[Dict], text_key: str) -> str:
    parts = []
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
            parts.append(block.get(text_key, "") or block.get("text", ""))
    return "".join(parts).strip()


def _compact_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _pretty_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _patch_paths_from_text(text: str) -> List[str]:
    paths = []
    for line in (text or "").splitlines():
        for prefix in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
            if line.startswith(prefix):
                paths.append(line[len(prefix):].strip())
    return paths


def _normalize_path(path: str) -> Optional[str]:
    if not isinstance(path, str):
        return None
    normalized = path.strip().strip("\"'")
    if not normalized or normalized in {".", "..", "sessions"}:
        return None
    if normalized.startswith(("http://", "https://")):
        return None
    return normalized


def _extract_paths(value) -> List[str]:
    """从结构化字段中尽量提取文件路径。"""
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"path", "filePath", "file_path", "newPath", "oldPath"} and isinstance(item, str):
                normalized = _normalize_path(item)
                if normalized:
                    found.append(normalized)
            else:
                found.extend(_extract_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_extract_paths(item))
    elif isinstance(value, str) and value.startswith("*** Begin Patch"):
        found.extend(_patch_paths_from_text(value))
    return found


def _tool_input_for_item(item: Dict) -> Dict:
    item_type = item.get("type")
    if item_type == "commandExecution":
        command = item.get("command", "")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return {
            "command": command,
            "cwd": item.get("cwd", ""),
            "command_actions": item.get("commandActions", []),
            "source": item.get("source", ""),
        }
    if item_type == "fileChange":
        return {
            "changes": item.get("changes", []),
            "status": item.get("status", ""),
        }
    if item_type == "mcpToolCall":
        return {
            "server": item.get("server", ""),
            "tool": item.get("tool", ""),
            "arguments": item.get("arguments"),
        }
    if item_type == "dynamicToolCall":
        tool = item.get("tool", "")
        arguments = item.get("arguments") or {}
        if tool in {"exec_command", "shell_command"}:
            command = arguments.get("cmd") or arguments.get("command") or ""
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            return {
                "command": command,
                "cwd": arguments.get("workdir") or arguments.get("cwd") or "",
                "source": "dynamicToolCall",
                "raw_tool": tool,
                "arguments": arguments,
            }
        data = {
            "tool": tool,
            "arguments": arguments,
        }
        if item.get("input") is not None:
            data["input"] = item.get("input")
        return data
    if item_type == "collabAgentToolCall":
        return {
            "tool": item.get("tool", ""),
            "prompt": item.get("prompt"),
            "model": item.get("model"),
            "reasoning_effort": item.get("reasoningEffort"),
            "receiver_thread_ids": item.get("receiverThreadIds", []),
            "sender_thread_id": item.get("senderThreadId", ""),
        }
    if item_type == "webSearch":
        return {
            "query": item.get("query", ""),
            "action": item.get("action"),
        }
    if item_type == "imageView":
        return {"path": item.get("path", "")}
    if item_type == "imageGeneration":
        return {
            "result": item.get("result", ""),
            "saved_path": item.get("savedPath"),
            "revised_prompt": item.get("revisedPrompt"),
        }
    return {"raw_item": item}


def _tool_name_for_item(item: Dict) -> str:
    item_type = item.get("type")
    if item_type == "commandExecution":
        return "exec_command"
    if item_type == "fileChange":
        return "file_change"
    if item_type == "mcpToolCall":
        server = item.get("server", "")
        tool = item.get("tool", "")
        return f"mcp:{server}/{tool}" if server else f"mcp:{tool}"
    if item_type == "dynamicToolCall":
        tool = item.get("tool", "")
        if tool in {"exec_command", "shell_command"}:
            return "exec_command"
        return tool or "dynamic_tool_call"
    if item_type == "collabAgentToolCall":
        return f"collab:{item.get('tool', '')}".rstrip(":")
    if item_type == "webSearch":
        return "web_search"
    if item_type == "imageView":
        return "image_view"
    if item_type == "imageGeneration":
        return "image_generation"
    return item_type or "unknown_tool"


def _observation_for_item(item: Dict) -> Tuple[str, bool]:
    item_type = item.get("type")
    if item_type == "commandExecution":
        lines = []
        if item.get("aggregatedOutput"):
            lines.append(item["aggregatedOutput"])
        if item.get("exitCode") is not None:
            lines.append(f"\n[exit_code={item['exitCode']}]")
        if item.get("durationMs") is not None:
            lines.append(f"[duration_ms={item['durationMs']}]")
        if item.get("status"):
            lines.append(f"[status={item['status']}]")
        content = "\n".join(line for line in lines if line).strip()
        return content or _pretty_json(item), (item.get("exitCode") not in (None, 0))

    if item_type == "fileChange":
        changes = item.get("changes", [])
        return _pretty_json({"changes": changes, "status": item.get("status")}), False

    if item_type == "mcpToolCall":
        if item.get("error"):
            return _pretty_json(item.get("error")), True
        return _pretty_json(item.get("result")), False

    if item_type == "dynamicToolCall":
        output = item.get("output") or item.get("contentItems")
        success = item.get("success")
        return _pretty_json(output), (success is False)

    if item_type == "collabAgentToolCall":
        return _pretty_json({
            "status": item.get("status"),
            "agents_states": item.get("agentsStates", {}),
            "receiver_thread_ids": item.get("receiverThreadIds", []),
        }), False

    if item_type == "webSearch":
        return _pretty_json({
            "query": item.get("query", ""),
            "action": item.get("action"),
        }), False

    if item_type == "imageView":
        return item.get("path", ""), False

    if item_type == "imageGeneration":
        return _pretty_json({
            "status": item.get("status"),
            "saved_path": item.get("savedPath"),
            "result": item.get("result"),
        }), False

    return _pretty_json(item), False


def _rollout_find_last_token_usage(records: List[Dict]) -> Dict:
    latest = {}
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload", {})
        if payload.get("type") != "token_count" or not payload.get("info"):
            continue
        latest = payload["info"]
    return latest


def _rollout_extract_context(records: List[Dict]) -> Dict:
    session_meta = next((r.get("payload", {}) for r in records if r.get("type") == "session_meta"), {})
    turn_contexts = [r.get("payload", {}) for r in records if r.get("type") == "turn_context"]
    developer_messages = []
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload", {})
        if payload.get("type") == "message" and payload.get("role") == "developer":
            text = _join_text_blocks(payload.get("content", []), "text")
            if text:
                developer_messages.append(text)
    return {
        "session_meta": session_meta,
        "turn_contexts": turn_contexts,
        "developer_messages": developer_messages,
        "token_usage": _rollout_find_last_token_usage(records),
    }


def _parse_duration_ms(duration: Dict) -> Optional[int]:
    if not isinstance(duration, dict):
        return None
    secs = duration.get("secs", 0) or 0
    nanos = duration.get("nanos", 0) or 0
    return int(secs * 1000 + nanos / 1_000_000)


def _load_rollout_records(rollout_path: Optional[Path]) -> List[Dict]:
    if not rollout_path or not rollout_path.exists():
        return []
    records = []
    for line in rollout_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("跳过无法解析的 rollout 行: %s", rollout_path)
    return records


def _build_rollout_fallback_turns(records: List[Dict]) -> List[Dict]:
    """为旧版 Codex 会话构建近似 turn/items 结构。"""
    exec_end_map: Dict[str, Dict] = {}
    web_end_map: Dict[str, Dict] = {}
    call_output_map: Dict[str, Dict] = {}
    custom_output_map: Dict[str, Dict] = {}

    for record in records:
        payload = record.get("payload", {})
        if record.get("type") == "event_msg" and payload.get("type") == "exec_command_end":
            exec_end_map[payload.get("call_id", "")] = payload
        elif record.get("type") == "event_msg" and payload.get("type") == "web_search_end":
            web_end_map[payload.get("call_id", "")] = payload
        elif record.get("type") == "response_item" and payload.get("type") == "function_call_output":
            call_output_map[payload.get("call_id", "")] = payload
        elif record.get("type") == "response_item" and payload.get("type") == "custom_tool_call_output":
            custom_output_map[payload.get("call_id", "")] = payload

    turns: List[Dict] = []
    current_turn: Optional[Dict] = None
    turn_index = 0
    item_index = 0

    def ensure_turn() -> Dict:
        nonlocal current_turn, turn_index
        if current_turn is None:
            turn_index += 1
            current_turn = {"id": f"rollout-turn-{turn_index}", "items": [], "status": "completed"}
            turns.append(current_turn)
        return current_turn

    for record in records:
        payload = record.get("payload", {})
        rtype = record.get("type")

        if rtype == "event_msg" and payload.get("type") == "user_message":
            turn_index += 1
            current_turn = {"id": f"rollout-turn-{turn_index}", "items": [], "status": "completed"}
            turns.append(current_turn)
            item_index += 1
            current_turn["items"].append({
                "type": "userMessage",
                "id": f"item-{item_index}",
                "content": [{"type": "text", "text": payload.get("message", "")}],
                "timestamp": record.get("timestamp", ""),
            })
            continue

        if rtype != "response_item":
            continue

        turn = ensure_turn()
        ptype = payload.get("type")

        if ptype == "reasoning":
            summaries = []
            for block in payload.get("summary", []) or []:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if text:
                        summaries.append(text)
            if summaries:
                item_index += 1
                turn["items"].append({
                    "type": "reasoning",
                    "id": f"item-{item_index}",
                    "summary": summaries,
                    "content": [],
                    "timestamp": record.get("timestamp", ""),
                })
            continue

        if ptype == "message" and payload.get("role") == "assistant":
            text = _join_text_blocks(payload.get("content", []), "text")
            if text:
                item_index += 1
                turn["items"].append({
                    "type": "agentMessage",
                    "id": f"item-{item_index}",
                    "text": text,
                    "phase": payload.get("phase"),
                    "timestamp": record.get("timestamp", ""),
                })
            continue

        if ptype == "function_call":
            args = payload.get("arguments")
            try:
                parsed_args = json.loads(args) if isinstance(args, str) else (args or {})
            except json.JSONDecodeError:
                parsed_args = {"_raw_arguments": args, "_parse_error": True}

            call_id = payload.get("call_id", "")
            name = payload.get("name", "")
            if name == "exec_command":
                exec_evt = exec_end_map.get(call_id, {})
                output_evt = call_output_map.get(call_id, {})
                item_index += 1
                turn["items"].append({
                    "type": "commandExecution",
                    "id": call_id or f"item-{item_index}",
                    "command": parsed_args.get("cmd") or parsed_args.get("command") or "",
                    "cwd": parsed_args.get("workdir") or exec_evt.get("cwd") or "",
                    "processId": exec_evt.get("process_id"),
                    "source": exec_evt.get("source", "rollout"),
                    "status": exec_evt.get("status", "completed"),
                    "commandActions": exec_evt.get("parsed_cmd", []),
                    "aggregatedOutput": exec_evt.get("aggregated_output") or output_evt.get("output"),
                    "exitCode": exec_evt.get("exit_code"),
                    "durationMs": _parse_duration_ms(exec_evt.get("duration")),
                    "timestamp": record.get("timestamp", ""),
                })
            else:
                output_evt = call_output_map.get(call_id, {})
                item_index += 1
                turn["items"].append({
                    "type": "dynamicToolCall",
                    "id": call_id or f"item-{item_index}",
                    "tool": name,
                    "arguments": parsed_args,
                    "output": output_evt.get("output"),
                    "status": "completed",
                    "timestamp": record.get("timestamp", ""),
                })
            continue

        if ptype == "custom_tool_call":
            call_id = payload.get("call_id", "")
            output_evt = custom_output_map.get(call_id, {})
            output = output_evt.get("output")
            output_json = None
            try:
                output_json = json.loads(output) if isinstance(output, str) else output
            except json.JSONDecodeError:
                output_json = output
            item_index += 1
            turn["items"].append({
                "type": "dynamicToolCall",
                "id": call_id or f"item-{item_index}",
                "tool": payload.get("name", ""),
                "arguments": {"input": payload.get("input")},
                "input": payload.get("input"),
                "output": output_json,
                "status": payload.get("status", "completed"),
                "success": None if not isinstance(output_json, dict) else (output_json.get("metadata", {}).get("exit_code") == 0),
                "timestamp": record.get("timestamp", ""),
            })
            continue

        if ptype == "web_search_call":
            action = payload.get("action", {}) or {}
            query = action.get("query") or payload.get("query", "")
            end_evt = next(
                (
                    event.get("payload", {})
                    for event in records
                    if event.get("type") == "event_msg"
                    and event.get("payload", {}).get("type") == "web_search_end"
                    and event.get("payload", {}).get("query") == query
                ),
                {},
            )
            item_index += 1
            turn["items"].append({
                "type": "webSearch",
                "id": payload.get("call_id") or f"item-{item_index}",
                "query": query,
                "action": end_evt.get("action") or action,
                "timestamp": record.get("timestamp", ""),
            })

    return turns


def _has_semantic_action_items(thread: Dict) -> bool:
    for turn in thread.get("turns", []):
        for item in turn.get("items", []):
            if item.get("type") in ACTION_ITEM_TYPES:
                return True
    return False


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))


def _write_jsonl(path: Path, rows: Iterable[Dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n" for row in rows)
    path.write_text(content)


def _write_watch_state(path: Path, state: Dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    tmp_path.replace(path)


def _read_watch_state(path: Path) -> Dict:
    if not path.exists():
        return {"version": WATCH_STATE_VERSION, "files": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("watch 状态文件损坏，重新初始化: %s", path)
        return {"version": WATCH_STATE_VERSION, "files": {}}
    if not isinstance(data, dict):
        return {"version": WATCH_STATE_VERSION, "files": {}}
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    data["version"] = WATCH_STATE_VERSION
    return data


def _path_fingerprint(path: Path) -> Dict:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _fingerprint_changed(previous: Optional[Dict], current: Dict) -> bool:
    if not previous:
        return True
    return (
        previous.get("size") != current.get("size")
        or previous.get("mtime_ns") != current.get("mtime_ns")
    )


def _parse_thread_id_from_rollout_path(rollout_path: Path) -> Optional[str]:
    match = ROLLOUT_FILENAME_RE.match(rollout_path.name)
    if not match:
        return None
    return match.group("thread_id")


def _session_artifacts_missing(session_dir: Path) -> bool:
    for filename in SESSION_ARTIFACTS:
        if not (session_dir / filename).exists():
            return True
    return False


def _load_sqlite_rows(db_path: Path, query: str, params: Tuple = ()) -> List[Dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _load_thread_row(codex_home: Path, thread_id: str) -> Dict:
    rows = _load_sqlite_rows(
        codex_home / "state_5.sqlite",
        "select * from threads where id = ? limit 1",
        (thread_id,),
    )
    return rows[0] if rows else {}


def _load_spawn_links(codex_home: Path, thread_id: str) -> Dict:
    state_db = codex_home / "state_5.sqlite"
    parents = _load_sqlite_rows(
        state_db,
        "select parent_thread_id, child_thread_id, status from thread_spawn_edges where child_thread_id = ?",
        (thread_id,),
    )
    children = _load_sqlite_rows(
        state_db,
        "select parent_thread_id, child_thread_id, status from thread_spawn_edges where parent_thread_id = ?",
        (thread_id,),
    )
    return {"parents": parents, "children": children}


def _action_agent_name(thread: Dict) -> str:
    return thread.get("agentNickname") or ("subagent" if str(thread.get("source", "")).startswith("subAgent") else "primary")


def _extract_edited_paths(item: Dict, tool_name: str, tool_input: Dict) -> List[str]:
    item_type = item.get("type")
    paths: List[str] = []

    if item_type == "fileChange":
        for change in tool_input.get("changes", []) or []:
            paths.extend(_extract_paths(change))
        return paths

    if item_type != "dynamicToolCall":
        return paths

    if tool_name == "apply_patch":
        raw_patch = (
            (tool_input.get("arguments") or {}).get("input")
            or tool_input.get("input")
            or ""
        )
        return _extract_paths(raw_patch)

    if tool_name in {"write", "edit", "multi_edit", "notebook_edit", "delete_file"}:
        return _extract_paths(tool_input)

    return paths


def _derive_exit_status(thread: Dict) -> str:
    turns = thread.get("turns", []) or []
    thread_status = (thread.get("status") or {}).get("type", "")
    last_turn = turns[-1] if turns else {}
    last_turn_status = last_turn.get("status", "")

    if last_turn_status == "failed" or last_turn.get("error"):
        return "error"
    if last_turn_status == "interrupted":
        return "user_interrupt"
    if last_turn_status == "inProgress":
        return "partial"

    if thread_status == "systemError":
        return "error"
    if thread_status == "active":
        return "partial"

    for turn in reversed(turns):
        for item in reversed(turn.get("items", []) or []):
            item_type = item.get("type")
            if item_type == "agentMessage" and item.get("phase") != "commentary" and item.get("text", "").strip():
                return "end_turn"
            if item_type in ACTION_ITEM_TYPES or item_type in {"reasoning", "plan"}:
                return "partial"
            if item_type == "userMessage":
                return "user_interrupt"

    if last_turn_status == "completed" or thread_status == "idle":
        return "end_turn"

    return "unknown"


def _build_trajectory_from_turns(thread: Dict, rollout_ctx: Dict) -> Dict:
    history: List[Dict] = []
    trajectory: List[Dict] = []

    session_meta = rollout_ctx.get("session_meta", {})
    turn_contexts = rollout_ctx.get("turn_contexts", [])
    developer_messages = rollout_ctx.get("developer_messages", [])
    token_usage = rollout_ctx.get("token_usage", {})

    if session_meta.get("base_instructions", {}).get("text"):
        history.append({
            "role": "system",
            "content": session_meta["base_instructions"]["text"],
            "agent": "primary",
            "message_type": "instruction",
        })

    for text in developer_messages:
        history.append({
            "role": "system",
            "content": text,
            "agent": "primary",
            "message_type": "instruction",
            "source_role": "developer",
        })

    if turn_contexts:
        first_ctx = turn_contexts[0]
        if first_ctx.get("user_instructions"):
            history.append({
                "role": "system",
                "content": first_ctx["user_instructions"],
                "agent": "primary",
                "message_type": "instruction",
                "source_role": "user_instructions",
            })

    tools_used = set()
    files_edited = set()
    action_agent = _action_agent_name(thread)
    last_non_commentary_assistant = ""

    for turn in thread.get("turns", []):
        pending_thoughts: List[str] = []

        for item in turn.get("items", []):
            item_type = item.get("type")
            timestamp = item.get("timestamp") or ""

            if item_type == "userMessage":
                segments = []
                for block in item.get("content", []):
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            segments.append(block.get("text", ""))
                        elif block.get("text"):
                            segments.append(block.get("text", ""))
                text = "\n".join(seg for seg in segments if seg).strip()
                if text:
                    history.append({
                        "role": "user",
                        "content": text,
                        "agent": action_agent,
                        "message_type": "user_message",
                    })
                continue

            if item_type == "hookPrompt":
                history.append({
                    "role": "system",
                    "content": _pretty_json(item.get("fragments", [])),
                    "agent": action_agent,
                    "message_type": "instruction",
                    "source_role": "hook_prompt",
                })
                continue

            if item_type == "reasoning":
                summary_text = "\n".join(item.get("summary", []) or [])
                content_text = "\n".join(item.get("content", []) or [])
                thought_text = (summary_text or content_text).strip()
                if thought_text:
                    pending_thoughts.append(thought_text)
                    history.append({
                        "role": "assistant",
                        "content": thought_text,
                        "agent": action_agent,
                        "message_type": "thought",
                        "thought": thought_text,
                    })
                continue

            if item_type == "agentMessage":
                text = item.get("text", "").strip()
                phase = item.get("phase")
                if text:
                    history.append({
                        "role": "assistant",
                        "content": text,
                        "agent": action_agent,
                        "message_type": "assistant_message",
                        "phase": phase,
                    })
                if text and phase != "commentary":
                    last_non_commentary_assistant = text
                    trajectory.append({
                        "message_type": "action",
                        "role": "assistant",
                        "content": text,
                        "thought": text,
                        "action": "final_answer",
                        "agent": action_agent,
                        "timestamp": timestamp,
                        "tool_name": "final_answer",
                        "tool_input": {},
                        "tool_use_id": item.get("id", ""),
                        "source_item_type": item_type,
                    })
                    pending_thoughts = []
                continue

            if item_type not in ACTION_ITEM_TYPES:
                history.append({
                    "role": "assistant",
                    "content": _pretty_json(item),
                    "agent": action_agent,
                    "message_type": "unknown_item",
                    "source_item_type": item_type,
                })
                continue

            thought = "\n\n".join(t for t in pending_thoughts if t).strip()
            pending_thoughts = []
            tool_name = _tool_name_for_item(item)
            tool_input = _tool_input_for_item(item)
            tools_used.add(tool_name)
            for path in _extract_edited_paths(item, tool_name, tool_input):
                files_edited.add(path)

            content = (thought + f"\n\nTool: {tool_name}\nInput: {_compact_json(tool_input)}").strip()
            trajectory.append({
                "message_type": "action",
                "role": "assistant",
                "content": content,
                "thought": thought,
                "action": f"{tool_name}({_compact_json(tool_input)})",
                "agent": action_agent,
                "timestamp": timestamp,
                "tool_use_id": item.get("id", ""),
                "tool_name": tool_name,
                "tool_input": tool_input,
                "source_item_type": item_type,
            })

            history.append({
                "role": "assistant",
                "content": _pretty_json(item),
                "agent": action_agent,
                "message_type": "action",
                "thought": thought or None,
                "tool_calls": [{
                    "function": {
                        "name": tool_name,
                        "arguments": _compact_json(tool_input),
                    }
                }],
                "tool_call_ids": [item.get("id", "")] if item.get("id") else None,
            })

            obs_content, is_error = _observation_for_item(item)
            trajectory.append({
                "message_type": "observation",
                "role": "user",
                "content": obs_content,
                "agent": action_agent,
                "is_error": is_error,
                "tool_use_id": item.get("id", ""),
                "source_item_type": item_type,
            })
            history.append({
                "role": "tool",
                "content": obs_content,
                "agent": action_agent,
                "message_type": "observation",
                "tool_call_ids": [item.get("id", "")] if item.get("id") else None,
            })

    totals = token_usage.get("total_token_usage", {}) if isinstance(token_usage, dict) else {}
    thread_row = thread.get("_thread_row", {}) or {}
    spawn_links = thread.get("_spawn_links", {}) or {}
    created_at = _iso_from_unix(thread.get("createdAt"))
    updated_at = _iso_from_unix(thread.get("updatedAt"))
    model = thread_row.get("model") or next(
        (ctx.get("model") for ctx in turn_contexts if isinstance(ctx, dict) and ctx.get("model")),
        "",
    )
    reasoning_effort = thread_row.get("reasoning_effort") or next(
        (ctx.get("effort") for ctx in turn_contexts if isinstance(ctx, dict) and ctx.get("effort")),
        "",
    )

    metadata = {
        "session_id": thread.get("id", ""),
        "model": model,
        "start_time": created_at,
        "end_time": updated_at,
        "total_steps": len(trajectory),
        "total_api_calls": 0,
        "total_turns": len(thread.get("turns", [])),
        "total_tokens_sent": totals.get("input_tokens", 0),
        "total_tokens_received": totals.get("output_tokens", 0),
        "total_cache_read_tokens": totals.get("cached_input_tokens", 0),
        "total_reasoning_output_tokens": totals.get("reasoning_output_tokens", 0),
        "total_tokens": totals.get("total_tokens", thread_row.get("tokens_used", 0)),
        "total_cost_usd": 0.0,
        "exit_status": _derive_exit_status(thread),
        "tools_used": sorted(tools_used),
        "files_edited": sorted(files_edited),
        "has_thinking": any(s.get("type") == "reasoning" for turn in thread.get("turns", []) for s in turn.get("items", [])),
        "has_sub_agent": bool(spawn_links.get("children")) or str(thread.get("source", "")).startswith("subAgent"),
        "working_directory": thread.get("cwd", ""),
        "source_kind": thread.get("source", ""),
        "cli_version": thread.get("cliVersion", ""),
        "reasoning_effort": reasoning_effort,
        "agent_nickname": thread.get("agentNickname"),
        "agent_role": thread.get("agentRole"),
        "git": thread.get("gitInfo"),
        "preview": thread.get("preview", ""),
        "capture_channel": "codex_app_server+local_rollout",
        "rollout_path": thread.get("path"),
        "last_turn_status": (thread.get("turns", []) or [{}])[-1].get("status"),
        "user_prompts": [
            item.get("content", [{}])[0].get("text", "")
            for turn in thread.get("turns", [])
            for item in turn.get("items", [])
            if item.get("type") == "userMessage"
        ],
        "parent_threads": spawn_links.get("parents", []),
        "child_threads": spawn_links.get("children", []),
        "codex_tokens_used": thread_row.get("tokens_used", 0),
        "last_assistant_message": last_non_commentary_assistant,
    }

    return {
        "trajectory": trajectory,
        "history": history,
        "info": {
            "model_stats": {
                "tokens_sent": totals.get("input_tokens", 0),
                "tokens_received": totals.get("output_tokens", 0),
                "cache_read_tokens": totals.get("cached_input_tokens", 0),
                "reasoning_output_tokens": totals.get("reasoning_output_tokens", 0),
                "api_calls": 0,
                "total_cost_usd": 0.0,
            },
            "exit_status": metadata["exit_status"],
            "has_thinking": metadata["has_thinking"],
            "source_kind": metadata["source_kind"],
        },
        "metadata": metadata,
    }


class CodexAppServerClient:
    def __init__(self, codex_cmd: str = "codex", timeout_sec: int = 30):
        self.codex_cmd = codex_cmd
        self.timeout_sec = timeout_sec
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 1

    def __enter__(self):
        self.proc = subprocess.Popen(
            [self.codex_cmd, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._initialize()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.proc:
            self.proc.kill()
            self.proc = None

    def _send(self, obj: Dict):
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _recv_response(self, req_id: int) -> Dict:
        assert self.proc and self.proc.stdout
        deadline = time.time() + self.timeout_sec
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            obj = json.loads(line)
            if obj.get("id") == req_id:
                if obj.get("error"):
                    raise RuntimeError(obj["error"].get("message", "unknown app-server error"))
                return obj.get("result", {})
        stderr = ""
        if self.proc and self.proc.stderr:
            try:
                stderr = self.proc.stderr.read()
            except Exception:
                stderr = ""
        raise RuntimeError(f"等待 app-server 响应超时: id={req_id}, stderr={stderr[:400]}")

    def _rpc(self, method: str, params: Dict) -> Dict:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._recv_response(req_id)

    def _initialize(self):
        result = self._rpc(
            "initialize",
            {
                "clientInfo": {"name": "claude-trace-importer", "version": "0.1.0"},
                "capabilities": {},
            },
        )
        logger.debug("app-server 初始化完成: %s", result.get("codexHome", ""))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def list_threads(self, limit: int = 200, source_kinds: Optional[List[str]] = None) -> List[Dict]:
        threads = []
        cursor = None
        while True:
            params = {
                "limit": limit,
                "sourceKinds": source_kinds or ALL_SOURCE_KINDS,
            }
            if cursor:
                params["cursor"] = cursor
            result = self._rpc("thread/list", params)
            threads.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return threads

    def read_thread(self, thread_id: str) -> Dict:
        result = self._rpc("thread/read", {"threadId": thread_id, "includeTurns": True})
        return result.get("thread", {})


class CodexImporter:
    def __init__(self, codex_home: Path, output_dir: Path, codex_cmd: str = "codex"):
        self.codex_home = codex_home
        self.output_dir = output_dir
        self.codex_cmd = codex_cmd

    def export_threads(self, thread_ids: Optional[List[str]] = None, limit: int = 200):
        with CodexAppServerClient(codex_cmd=self.codex_cmd) as client:
            if thread_ids:
                targets = thread_ids
            else:
                targets = [t.get("id") for t in client.list_threads(limit=limit) if t.get("id")]

            exported = 0
            for thread_id in targets:
                try:
                    self.export_thread(client, thread_id)
                    exported += 1
                except Exception as exc:
                    logger.warning("导出失败 %s: %s", thread_id[:12], exc)
            logger.info("Codex 导出完成: %d 个线程", exported)

    def export_thread_once(self, thread_id: str) -> Dict:
        with CodexAppServerClient(codex_cmd=self.codex_cmd) as client:
            return self.export_thread(client, thread_id)

    def export_thread(self, client: CodexAppServerClient, thread_id: str) -> Dict:
        thread = client.read_thread(thread_id)
        if not thread:
            raise RuntimeError("thread/read 返回空数据")

        thread_row = _load_thread_row(self.codex_home, thread_id)
        spawn_links = _load_spawn_links(self.codex_home, thread_id)
        thread["_thread_row"] = thread_row
        thread["_spawn_links"] = spawn_links

        session_dir = self.output_dir / thread_id
        session_dir.mkdir(parents=True, exist_ok=True)

        rollout_path = Path(thread.get("path")) if thread.get("path") else None
        rollout_records = _load_rollout_records(rollout_path)
        rollout_ctx = _rollout_extract_context(rollout_records)

        # 新版 thread/read 能回放工具 item；旧版会话需要 fallback 解析 rollout.jsonl
        if not _has_semantic_action_items(thread) and rollout_records:
            thread["turns"] = _build_rollout_fallback_turns(rollout_records)
            thread["_capture_fallback"] = "rollout_jsonl"
        else:
            thread["_capture_fallback"] = "app_server"

        traj = _build_trajectory_from_turns(thread, rollout_ctx)
        traj["metadata"]["thread_status"] = thread.get("status", {})
        traj["metadata"]["capture_fallback"] = thread.get("_capture_fallback")

        save_trajectory(session_dir / "session.traj", traj)
        _write_json(session_dir / "codex_thread.json", thread)

        rollout_output_path = session_dir / "rollout.jsonl"
        if rollout_path and rollout_path.exists():
            shutil.copy2(rollout_path, session_dir / "rollout.jsonl")
        elif not rollout_output_path.exists():
            rollout_output_path.write_text("")

        state_db = self.codex_home / "state_5.sqlite"
        state_logs = _load_sqlite_rows(
            state_db,
            "select * from logs where thread_id = ? order by ts, ts_nanos, id",
            (thread_id,),
        )
        feedback_db = self.codex_home / "logs_1.sqlite"
        feedback_logs = _load_sqlite_rows(
            feedback_db,
            "select * from logs where thread_id = ? order by ts, ts_nanos, id",
            (thread_id,),
        )
        state_logs_path = session_dir / "state_logs.jsonl"
        feedback_logs_path = session_dir / "feedback_logs.jsonl"
        if state_logs or state_db.exists() or not state_logs_path.exists():
            _write_jsonl(session_dir / "state_logs.jsonl", state_logs)
        if feedback_logs or feedback_db.exists() or not feedback_logs_path.exists():
            _write_jsonl(session_dir / "feedback_logs.jsonl", feedback_logs)

        logger.info(
            "已导出 Codex 线程: %s | turns=%d | steps=%d | source=%s",
            thread_id[:12],
            len(thread.get("turns", [])),
            traj["metadata"]["total_steps"],
            thread.get("_capture_fallback"),
        )
        return {
            "thread_id": thread_id,
            "session_dir": session_dir,
            "rollout_path": rollout_path,
            "thread_status": (thread.get("status") or {}).get("type", ""),
            "capture_fallback": thread.get("_capture_fallback"),
            "total_steps": traj["metadata"]["total_steps"],
        }


class CodexRolloutWatcher:
    def __init__(
        self,
        importer: CodexImporter,
        codex_home: Path,
        state_file: Path,
        poll_interval: float = 2.0,
        debounce_sec: float = 2.0,
        finalize_sec: float = 8.0,
        retry_sec: float = 10.0,
        on_export: Optional[Callable[[Dict], None]] = None,
    ):
        self.importer = importer
        self.codex_home = codex_home
        self.state_file = state_file
        self.poll_interval = poll_interval
        self.debounce_sec = debounce_sec
        self.finalize_sec = finalize_sec
        self.retry_sec = retry_sec
        self.on_export = on_export
        self.state = _read_watch_state(state_file)
        self.pending: Dict[str, Dict] = {}
        self._client: Optional[CodexAppServerClient] = None

    def _get_client(self) -> CodexAppServerClient:
        if self._client is None:
            self._client = CodexAppServerClient(codex_cmd=self.importer.codex_cmd)
            self._client.__enter__()
        return self._client

    def _close_client(self):
        if self._client is None:
            return
        try:
            self._client.__exit__(None, None, None)
        finally:
            self._client = None

    def _persist_state(self):
        _write_watch_state(self.state_file, self.state)

    def _mark_dirty(self, thread_id: str, rollout_path: Path, fingerprint: Dict, reason: str):
        now = time.time()
        item = self.pending.get(thread_id, {})
        if (
            item.get("rollout_path") == str(rollout_path)
            and item.get("fingerprint") == fingerprint
        ):
            return
        item.update({
            "thread_id": thread_id,
            "rollout_path": str(rollout_path),
            "fingerprint": fingerprint,
            "due_at": now + self.debounce_sec,
            "final_due_at": now + self.finalize_sec,
            "reason": reason,
            "final_pass_scheduled": False,
        })
        self.pending[thread_id] = item
        logger.info("检测到 Codex 线程变更，已排队刷新: %s | reason=%s", thread_id[:12], reason)

    def _scan_rollouts(self):
        sessions_root = self.codex_home / "sessions"
        current_paths = set()
        for rollout_path in sorted(sessions_root.rglob("rollout-*.jsonl")):
            if not rollout_path.is_file():
                continue
            rollout_key = str(rollout_path)
            current_paths.add(rollout_key)
            thread_id = _parse_thread_id_from_rollout_path(rollout_path)
            if not thread_id:
                logger.debug("跳过无法识别 thread_id 的 rollout 文件: %s", rollout_path)
                continue

            fingerprint = _path_fingerprint(rollout_path)
            previous = self.state.get("files", {}).get(rollout_key)
            session_dir = self.importer.output_dir / thread_id

            reason = ""
            if _fingerprint_changed(previous, fingerprint):
                reason = "new_or_appended_rollout"
            elif _session_artifacts_missing(session_dir):
                reason = "missing_export_artifacts"

            if reason:
                self._mark_dirty(thread_id, rollout_path, fingerprint, reason)

        stale_paths = [
            rollout_key
            for rollout_key in self.state.get("files", {})
            if rollout_key not in current_paths
        ]
        for rollout_key in stale_paths:
            self.state["files"].pop(rollout_key, None)
        if stale_paths:
            self._persist_state()

    def _export_thread(self, thread_id: str, force: bool = False):
        item = self.pending.get(thread_id)
        if not item:
            return

        rollout_key = item.get("rollout_path", "")
        fingerprint = item.get("fingerprint", {})
        try:
            result = self.importer.export_thread(self._get_client(), thread_id)
            if rollout_key:
                self.state.setdefault("files", {})[rollout_key] = {
                    "thread_id": thread_id,
                    "size": fingerprint.get("size", 0),
                    "mtime_ns": fingerprint.get("mtime_ns", 0),
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                }
                self._persist_state()

            now = time.time()
            if (
                not force
                and self.finalize_sec > 0
                and not item.get("final_pass_scheduled")
                and now < item.get("final_due_at", 0)
            ):
                item["due_at"] = item["final_due_at"]
                item["final_pass_scheduled"] = True
                self.pending[thread_id] = item
                logger.info("线程已初次刷新，等待安静期后二次确认: %s", thread_id[:12])
            else:
                self.pending.pop(thread_id, None)
                if self.on_export:
                    try:
                        self.on_export(result)
                    except Exception as callback_exc:
                        logger.warning("导出后回调失败: %s | %s", thread_id[:12], callback_exc)
                logger.info(
                    "线程刷新完成: %s | status=%s | steps=%s",
                    thread_id[:12],
                    result.get("thread_status", ""),
                    result.get("total_steps", 0),
                )
        except Exception as exc:
            self._close_client()
            if force:
                raise
            item["due_at"] = time.time() + self.retry_sec
            item["last_error"] = str(exc)
            self.pending[thread_id] = item
            logger.warning("刷新线程失败，稍后重试: %s | %s", thread_id[:12], exc)

    def _drain_pending(self, force: bool = False):
        now = time.time()
        ready = [
            thread_id
            for thread_id, item in self.pending.items()
            if force or item.get("due_at", 0) <= now
        ]
        for thread_id in sorted(ready):
            self._export_thread(thread_id, force=force)

    def run_once(self):
        self._scan_rollouts()
        self._drain_pending(force=True)
        self._close_client()

    def watch_forever(self, stop_event=None):
        logger.info(
            "Codex watcher 已启动: %s | poll=%.1fs debounce=%.1fs finalize=%.1fs",
            self.codex_home / "sessions",
            self.poll_interval,
            self.debounce_sec,
            self.finalize_sec,
        )
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    logger.info("收到停止信号，准备结束 Codex watcher")
                    break
                self._scan_rollouts()
                self._drain_pending(force=False)
                if stop_event is not None:
                    stop_event.wait(self.poll_interval)
                else:
                    time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("收到中断信号，停止 Codex watcher")
        finally:
            self._close_client()


def main():
    parser = argparse.ArgumentParser(
        description="导入 Codex CLI 会话到统一轨迹格式",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--thread-id", action="append", help="指定 thread_id（可重复）")
    parser.add_argument("--all", action="store_true", help="导出所有可见线程")
    parser.add_argument("--watch", action="store_true", help="监听 ~/.codex/sessions 下 rollout 文件变化并持续刷新")
    parser.add_argument("--once", action="store_true", help="执行一次增量扫描后退出（适合测试或 cron）")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"), help="Codex 数据目录")
    parser.add_argument("--output", default="./trajectories/sessions", help="输出目录")
    parser.add_argument("--codex-cmd", default="codex", help="Codex CLI 命令")
    parser.add_argument("--limit", type=int, default=200, help="thread/list 分页大小")
    parser.add_argument("--state-file", default="", help="watch 模式状态文件路径")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="watch 扫描间隔（秒）")
    parser.add_argument("--debounce-sec", type=float, default=2.0, help="rollout 变更后的等待时间（秒）")
    parser.add_argument("--finalize-sec", type=float, default=8.0, help="安静期后二次确认导出时间（秒）")
    parser.add_argument("--retry-sec", type=float, default=10.0, help="导出失败后的重试间隔（秒）")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    output_dir = Path(args.output).expanduser()

    if not args.watch and not args.once and not args.all and not args.thread_id:
        parser.error("必须指定 --watch / --once / --all / --thread-id 中的至少一种模式")

    importer = CodexImporter(
        codex_home=codex_home,
        output_dir=output_dir,
        codex_cmd=args.codex_cmd,
    )
    if args.watch or args.once:
        state_file = Path(args.state_file).expanduser() if args.state_file else output_dir / ".codex_watch_state.json"
        watcher = CodexRolloutWatcher(
            importer=importer,
            codex_home=codex_home,
            state_file=state_file,
            poll_interval=args.poll_interval,
            debounce_sec=args.debounce_sec,
            finalize_sec=args.finalize_sec,
            retry_sec=args.retry_sec,
        )
        if args.once:
            watcher.run_once()
        else:
            watcher.watch_forever()
        return

    importer.export_threads(thread_ids=args.thread_id, limit=args.limit)


if __name__ == "__main__":
    main()
