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
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

EVENTS_DIR = Path.home() / ".claude" / "trajectory_events"
PROXY_PORT = os.environ.get("CLAUDE_PROXY_PORT", "4000")
PROXY_BASE = f"http://127.0.0.1:{PROXY_PORT}"

# P0 #5: session_id 只允许字母数字和连字符，防止路径遍历
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# 官方 OTel tool_decision.source。只有决策字段也在时才抄 source，避免
# SessionStart/End 的 source（startup/resume）被当成决策来源。
_OFFICIAL_DECISION_SOURCES = frozenset({
    "config", "hook", "user_permanent", "user_temporary", "user_abort", "user_reject",
})
_DECISION_FIELD_KEYS = (
    "decision",
    "permission_decision",
    "decision_source",
    "permission_decision_source",
)


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


def copy_decision_fields(event, input_data):
    """把 Claude Code 可能带上的决策字段抄到事件上。

    2026-09-18 存量扫描（Claude Code 2.1.276）：PostToolUse / Notification /
    PermissionRequest 都还没有 decision / source。这里先把字段名写进采集路径，
    官方一旦补决策后 hook，events.jsonl 就能接到真实值，不必再改 collector。
    """
    for key in _DECISION_FIELD_KEYS:
        if key in input_data and input_data[key] not in (None, ""):
            event[key] = input_data[key]
    # 只有已经有决策时，才把裸 source 当成决策来源
    if (
        "decision_source" not in event
        and "permission_decision_source" not in event
        and (event.get("decision") or event.get("permission_decision"))
        and input_data.get("source") in _OFFICIAL_DECISION_SOURCES
    ):
        event["decision_source"] = input_data["source"]


def permission_mode_change_event(
    prev_mode,
    current_mode,
    timestamp,
    session_id,
    cwd,
    trigger=None,
):
    """相邻事件 permission_mode 边沿。trigger 拿不到就空，禁止编造官方枚举。"""
    if not prev_mode or not current_mode or prev_mode == current_mode:
        return None
    changed = {
        "timestamp": timestamp,
        "event": "PermissionModeChanged",
        "session_id": session_id,
        "cwd": cwd,
        "permission_mode": current_mode,
        "from": prev_mode,
        "to": current_mode,
    }
    if trigger:
        changed["trigger"] = trigger
    return changed


def _mode_state_path(session_id):
    return EVENTS_DIR / f".{session_id}.perm_mode"


def read_last_permission_mode(session_id):
    """跨 hook 进程记住上一次非空 permission_mode。

    collector 每次都是新进程，不能靠内存做边沿检测。sidecar 只有一行；
    读失败就当没有上一次，宁可漏一条变更也不能让 hook 崩。
    """
    path = _mode_state_path(session_id)
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except Exception:
        return None
    return None


def write_last_permission_mode(session_id, mode):
    try:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        _mode_state_path(session_id).write_text(mode, encoding="utf-8")
    except Exception:
        pass


# git 状态采集来自共享模块 git_state.py。
# 该文件由 setup_hooks.py 与 collector.py 一起部署到 ~/.claude/hooks/，
# 因此这里的 import 在部署后同样成立（sys.path[0] 即 hooks 目录）。
# 导入失败时降级为「不采集 git 状态」——hook 绝不能因此崩溃。
try:
    from git_state import collect_git_state, flatten_git_state
except Exception:  # pragma: no cover - 部署不完整时的兜底
    def collect_git_state(cwd: str, source: str = "hook") -> dict:  # type: ignore[misc]
        return {}

    def flatten_git_state(state: dict) -> dict:  # type: ignore[misc]
        return {}


def main():
    # stdin 为空时提前退出
    raw_input = sys.stdin.read()
    if not raw_input.strip():
        return

    input_data = json.loads(raw_input)

    event_name = input_data.get("hook_event_name", "unknown")
    session_id = input_data.get("session_id", "unknown")
    timestamp = datetime.now().isoformat()

    # P0 #5: 校验 session_id 防止路径遍历
    if not session_id or not _SAFE_ID_RE.match(session_id):
        session_id = "unknown"

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
        # P0（bench §8.4）：会话起点的 git HEAD 与脏状态，事后无法重建。
        # 扁平字段放 event 顶层便于直接查询，完整快照放 git_state 供精细分析。
        git_snapshot = collect_git_state(input_data.get("cwd") or "", source="hook")
        if git_snapshot:
            event.update(flatten_git_state(git_snapshot))
            event["git_state"] = git_snapshot
        # 关键：主动通知代理建立确定性 session_id 关联
        notify_proxy("session-register", {
            "session_id": session_id,
            "model": input_data.get("model"),
            "source": input_data.get("source"),
            "cwd": input_data.get("cwd"),
            "git_state": git_snapshot,
        })

    elif event_name == "SessionEnd":
        event["source"] = input_data.get("source")
        # 会话终点的 git 状态：与 SessionStart 的 git_head 一对比即知
        # 「这次会话有没有产生 commit」「结束时工作区留下多少改动」
        git_snapshot = collect_git_state(input_data.get("cwd") or "", source="hook")
        if git_snapshot:
            event.update(flatten_git_state(git_snapshot))
            event["git_state"] = git_snapshot
        # 通知代理会话结束，触发轨迹导出。
        # 终点快照必须随这条通知一起送出，不能只靠 events.jsonl：
        # 本函数是「先 notify、后写文件」，而 notify 会同步触发代理导出 traj，
        # 代理那时读 events.jsonl 往往还看不到这条 SessionEnd。
        notify_proxy("session-event", {
            "session_id": session_id,
            "event": "end",
            "source": input_data.get("source"),
            "git_state_end": git_snapshot,
        })

    elif event_name == "UserPromptSubmit":
        event["prompt"] = input_data.get("prompt")

    elif event_name == "Stop":
        event["last_assistant_message"] = input_data.get("last_assistant_message")
        event["transcript_path"] = input_data.get("transcript_path")
        # 通知代理 turn 结束，便于实时感知 turn 边界
        notify_proxy("session-event", {
            "session_id": session_id,
            "event": "stop",
        })

    # ── P1 事件 ──────────────────────────────────────────────
    elif event_name in ("PostToolUse", "PreToolUse", "PostToolUseFailure"):
        event["tool_name"] = input_data.get("tool_name")
        event["tool_input"] = input_data.get("tool_input")
        event["tool_use_id"] = input_data.get("tool_use_id")
        if event_name == "PostToolUse":
            event["tool_response"] = input_data.get("tool_response")
        elif event_name == "PostToolUseFailure":
            # 工具执行失败：记录错误信息，用于标注失败轨迹
            event["error"] = input_data.get("error")
            event["tool_response"] = input_data.get("tool_response")
        copy_decision_fields(event, input_data)

    elif event_name in ("SubagentStart", "SubagentStop"):
        event["agent_id"] = input_data.get("agent_id")
        event["agent_type"] = input_data.get("agent_type")
        # 新版 Claude Code 用 subagent_type / agent_name，做兜底兼容
        for k in ("subagent_type", "agent_name", "parent_session_id", "prompt"):
            if k in input_data:
                event[k] = input_data[k]

    elif event_name in ("PreCompact", "PostCompact"):
        event["trigger"] = input_data.get("trigger")           # manual / auto
        event["compact_summary"] = input_data.get("compact_summary")
        # PreCompact 带压缩前的上下文规模，用于分析 compaction 影响
        for k in ("custom_instructions", "token_count", "message_count"):
            if k in input_data:
                event[k] = input_data[k]

    # ── 模型调用边界 ─────────────────────────────────────────
    # Fix: BeforeModel / AfterModel 是新版 Claude Code 新增事件，
    # 实测已出现在采集数据中但 collector 没有对应分支（落到 raw_input 兜底）。
    # 显式处理并保留 model / turn 信息，用于把 hook 事件与 API 请求精确对齐。
    elif event_name in ("BeforeModel", "AfterModel"):
        event["model"] = input_data.get("model")
        for k in ("turn_id", "request_id", "stop_reason", "usage", "message_count"):
            if k in input_data:
                event[k] = input_data[k]

    elif event_name == "Notification":
        event["message"] = input_data.get("message")
        event["title"] = input_data.get("title")
        copy_decision_fields(event, input_data)

    # ── P2 事件 ──────────────────────────────────────────────
    elif event_name == "PermissionRequest":
        # PermissionRequest 是 pre-hook，发生在用户点按钮之前。
        # 2.1.276 实测没有 decision / tool_use_id；两者都抄，官方一旦补上就能接到。
        event["tool_name"] = input_data.get("tool_name")
        event["tool_input"] = input_data.get("tool_input")
        if input_data.get("tool_use_id"):
            event["tool_use_id"] = input_data.get("tool_use_id")
        copy_decision_fields(event, input_data)

    elif event_name == "InstructionsLoaded":
        # P2 fix: 使用 Claude Code 实际传入的字段名，兜底保存完整 input_data
        event["source"] = input_data.get("source")
        event["content_length"] = len(str(input_data.get("content", "")))
        # 保留原始 input 中的其他字段作为兜底
        for k in ("instructions_path", "instructions_hash", "path", "content"):
            if k in input_data:
                event[k] = input_data[k]

    elif event_name == "StopFailure":
        event["error"] = input_data.get("error")

    else:
        # P2 #19: 未知事件类型，保存完整 input_data 作为兜底
        event["raw_input"] = {
            k: v for k, v in input_data.items()
            if k not in ("hook_event_name", "session_id", "cwd", "permission_mode")
        }

    # 追加写入事件文件（按 session_id 分文件）
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    events_file = EVENTS_DIR / f"{session_id}.jsonl"
    current_mode = event.get("permission_mode")
    mode_changed = None
    if current_mode:
        mode_changed = permission_mode_change_event(
            read_last_permission_mode(session_id),
            current_mode,
            timestamp,
            session_id,
            event.get("cwd"),
        )
    with open(events_file, "a") as f:
        if mode_changed:
            f.write(json.dumps(mode_changed, ensure_ascii=False) + "\n")
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    if current_mode:
        write_last_permission_mode(session_id, current_mode)
    if event_name == "SessionEnd":
        try:
            _mode_state_path(session_id).unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 全局容错：任何异常都静默退出，不影响 Claude Code 正常使用
        pass
