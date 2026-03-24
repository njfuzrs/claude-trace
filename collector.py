#!/usr/bin/env python3
"""
collector.py — 统一 Hook 采集脚本，所有事件共用一个入口

部署位置：~/.claude/hooks/collector.py
触发方式：Claude Code settings.json 中配置 hooks

注意：整个脚本包在 try/except 中，任何异常都静默退出。
因为 Hook 脚本崩溃会导致 Claude Code 报错（blocking hook），
数据采集失败不应影响用户正常使用。
"""

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

EVENTS_DIR = Path.home() / ".claude" / "trajectory_events"
PROXY_PORT = os.environ.get("CLAUDE_PROXY_PORT", "4000")
PROXY_BASE = f"http://127.0.0.1:{PROXY_PORT}"


def notify_proxy(endpoint: str, data: dict):
    """通过 HTTP 回调通知代理（非阻塞，失败静默）"""
    try:
        req = urllib.request.Request(
            f"{PROXY_BASE}/_internal/{endpoint}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # 代理未启动或网络异常，静默忽略


def main():
    # stdin 为空时提前退出
    raw_input = sys.stdin.read()
    if not raw_input.strip():
        return

    input_data = json.loads(raw_input)

    event_name = input_data.get("hook_event_name", "unknown")
    session_id = input_data.get("session_id", "unknown")
    timestamp = datetime.now().isoformat()

    # 构建基础事件记录
    event: dict = {
        "timestamp": timestamp,
        "event": event_name,
        "session_id": session_id,
        "cwd": input_data.get("cwd"),
        "permission_mode": input_data.get("permission_mode"),
    }

    # ── P0 事件 ──────────────────────────────────────────────
    if event_name == "SessionStart":
        event["source"] = input_data.get("source")   # startup / resume / clear
        event["model"] = input_data.get("model")
        # 关键：主动通知代理建立确定性 session_id 关联
        notify_proxy("session-register", {
            "session_id": session_id,
            "model": input_data.get("model"),
            "source": input_data.get("source"),
            "cwd": input_data.get("cwd"),
        })

    elif event_name == "SessionEnd":
        event["source"] = input_data.get("source")
        # 通知代理会话结束，触发轨迹导出
        notify_proxy("session-event", {
            "session_id": session_id,
            "event": "end",
            "source": input_data.get("source"),
        })

    elif event_name == "UserPromptSubmit":
        event["prompt"] = input_data.get("prompt")

    elif event_name == "Stop":
        event["last_assistant_message"] = input_data.get("last_assistant_message")
        event["transcript_path"] = input_data.get("transcript_path")

    # ── P1 事件 ──────────────────────────────────────────────
    elif event_name in ("PostToolUse", "PreToolUse"):
        event["tool_name"] = input_data.get("tool_name")
        event["tool_input"] = input_data.get("tool_input")
        event["tool_use_id"] = input_data.get("tool_use_id")
        if event_name == "PostToolUse":
            event["tool_response"] = input_data.get("tool_response")

    elif event_name in ("SubagentStart", "SubagentStop"):
        event["agent_id"] = input_data.get("agent_id")
        event["agent_type"] = input_data.get("agent_type")

    elif event_name == "PostCompact":
        event["trigger"] = input_data.get("trigger")           # manual / auto
        event["compact_summary"] = input_data.get("compact_summary")

    # ── P2 事件 ──────────────────────────────────────────────
    elif event_name == "PermissionRequest":
        event["tool_name"] = input_data.get("tool_name")
        event["tool_input"] = input_data.get("tool_input")
        event["decision"] = input_data.get("decision")

    elif event_name == "InstructionsLoaded":
        event["instructions_path"] = input_data.get("instructions_path")
        event["instructions_hash"] = input_data.get("instructions_hash")

    elif event_name == "StopFailure":
        event["error"] = input_data.get("error")

    # 追加写入事件文件（按 session_id 分文件）
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    events_file = EVENTS_DIR / f"{session_id}.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 全局容错：任何异常都静默退出，不影响 Claude Code 正常使用
        pass
