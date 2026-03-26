#!/usr/bin/env python3
"""
viewer.py — 轨迹数据 HTML 查看器

将 .traj 文件转换为自包含的 HTML 文件，浏览器打开即可还原完整对话流程。

用法：
    python viewer.py trajectories/traj/xxx.traj          # 单文件
    python viewer.py trajectories/traj/                   # 目录索引
    python viewer.py trajectories/traj/xxx.traj -o out.html
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

# ─────────────────────────────────────────────
# HTML 模板
# ─────────────────────────────────────────────

_CSS = """
:root {
  --bg: #0d1117; --fg: #e6edf3; --border: #30363d;
  --system-bg: #1c1e26; --system-fg: #8b949e;
  --user-bg: #1a2233; --user-border: #1f6feb;
  --assistant-bg: #161b22; --assistant-border: #3fb950;
  --tool-bg: #13171e; --tool-border: #6e7681;
  --thinking-bg: #1a1520; --thinking-border: #a371f7;
  --error-bg: #2d1215; --error-border: #f85149;
  --meta-bg: #161b22; --accent: #58a6ff; --accent2: #3fb950;
  --code-bg: #1a1e24;
  --timeline-line: #21262d; --timeline-dot: #30363d;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
       background: var(--bg); color: var(--fg); line-height: 1.6; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── 整体布局 ── */
.page-layout { display: flex; min-height: 100vh; }
.sidebar { width: 220px; position: fixed; top: 0; left: 0; height: 100vh;
           background: var(--meta-bg); border-right: 1px solid var(--border);
           overflow-y: auto; padding: 12px 0; z-index: 200; font-size: 12px; }
.sidebar .sid { padding: 8px 14px; color: var(--accent); font-weight: 600;
                font-size: 13px; border-bottom: 1px solid var(--border); margin-bottom: 6px; }
.sidebar .nav-section { padding: 4px 14px; color: var(--system-fg); font-size: 10px;
                        text-transform: uppercase; letter-spacing: 0.5px; margin-top: 10px; }
.sidebar .nav-item { display: block; padding: 4px 14px; color: var(--fg);
                     text-decoration: none; border-left: 2px solid transparent;
                     white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sidebar .nav-item:hover { background: rgba(255,255,255,0.04); }
.sidebar .nav-item.active { border-left-color: var(--accent); color: var(--accent); background: rgba(88,166,255,0.06); }
.sidebar .nav-item.nav-user { color: var(--user-border); font-weight: 500; }
.sidebar .nav-item.nav-step { color: var(--system-fg); padding-left: 22px; font-size: 11px; }
.sidebar .nav-item .nav-badge { display: inline-block; background: var(--tool-bg); border: 1px solid var(--border);
                                border-radius: 3px; padding: 0 4px; font-size: 10px; margin-left: 4px; color: var(--system-fg); }
.main-content { margin-left: 220px; max-width: 900px; padding: 16px 24px; flex: 1; }

/* ── 元信息卡片 ── */
.meta-card { background: var(--meta-bg); border: 1px solid var(--border);
             border-radius: 8px; padding: 16px; margin-bottom: 20px; }
.meta-card h1 { font-size: 18px; margin-bottom: 8px; color: var(--accent); }
.meta-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
             gap: 8px; font-size: 13px; }
.meta-item { color: var(--system-fg); }
.meta-item strong { color: var(--fg); }
.tools-list { margin-top: 8px; font-size: 12px; color: var(--system-fg); }
.tools-list span { background: var(--tool-bg); border: 1px solid var(--border);
                   border-radius: 4px; padding: 1px 6px; margin: 2px; display: inline-block; }

/* ── 搜索栏 ── */
.search-bar { position: sticky; top: 0; z-index: 100; background: var(--bg);
              padding: 8px 0; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
.search-bar input { width: 100%; padding: 8px 12px; background: var(--meta-bg);
                    border: 1px solid var(--border); border-radius: 6px;
                    color: var(--fg); font-size: 14px; outline: none; }
.search-bar input:focus { border-color: var(--accent); }
.search-bar .stats { font-size: 12px; color: var(--system-fg); margin-top: 4px; }

/* ── 时间线 ── */
.timeline { position: relative; padding-left: 28px; }
.timeline::before { content: ''; position: absolute; left: 9px; top: 0; bottom: 0;
                    width: 2px; background: var(--timeline-line); }

/* 时间线节点 */
.tl-node { position: relative; margin: 0 0 2px 0; }
.tl-node::before { content: ''; position: absolute; left: -23px; top: 14px;
                   width: 10px; height: 10px; border-radius: 50%;
                   background: var(--timeline-dot); border: 2px solid var(--bg); z-index: 1; }

/* ── 用户输入节点 ── */
.tl-user { margin: 20px 0 12px 0; }
.tl-user::before { background: var(--user-border); width: 12px; height: 12px; left: -24px; top: 12px; }
.tl-user-card { background: var(--user-bg); border: 1px solid rgba(31,111,235,0.3);
                border-radius: 8px; padding: 12px 16px; }
.tl-user-card .tl-label { font-size: 11px; font-weight: 600; text-transform: uppercase;
                          color: var(--user-border); letter-spacing: 0.5px; margin-bottom: 4px; }
.tl-user-card .tl-label .tl-ts { color: var(--system-fg); font-weight: normal;
                                  float: right; font-size: 10px; text-transform: none; }

/* ── 工具循环节点（折叠的连续工具调用） ── */
.tl-toolloop { margin: 2px 0; }
.tl-toolloop::before { background: var(--tool-border); width: 8px; height: 8px; left: -22px; top: 10px; }
.tl-toolloop > summary { list-style: none; cursor: pointer; user-select: none;
                         background: var(--tool-bg); border: 1px solid var(--border); border-radius: 6px;
                         padding: 6px 12px; font-size: 12px; color: var(--system-fg); display: flex;
                         align-items: center; gap: 6px; }
.tl-toolloop > summary::-webkit-details-marker { display: none; }
.tl-toolloop > summary::before { content: '▶'; font-size: 9px; color: var(--system-fg); transition: transform 0.15s; }
.tl-toolloop[open] > summary::before { transform: rotate(90deg); }
.tl-toolloop > summary .loop-label { color: #79c0ff; font-weight: 500; }
.tl-toolloop > summary .loop-count { color: var(--system-fg); }
.tool-pill { display: inline-block; background: rgba(121,192,255,0.1); border: 1px solid rgba(121,192,255,0.2);
             border-radius: 3px; padding: 0 5px; font-size: 11px; color: #79c0ff; margin: 1px 2px; }
.tool-pill.t-error { border-color: rgba(248,81,73,0.3); color: var(--error-border); background: rgba(248,81,73,0.08); }
.tl-toolloop > .loop-body { padding: 4px 0 4px 8px; border-left: 2px solid var(--border); margin: 4px 0 4px 12px; }

/* 工具调用/结果（loop 内部） */
.tool-step { margin: 3px 0; }
.tool-step summary { cursor: pointer; font-size: 12px; padding: 3px 8px; border-radius: 4px;
                     list-style: none; user-select: none; }
.tool-step summary::-webkit-details-marker { display: none; }
.tool-step summary::before { content: '▶ '; font-size: 9px; }
.tool-step[open] > summary::before { content: '▼ '; }
.tool-step .detail-body { padding: 6px 10px; margin-top: 2px; border-radius: 4px;
                          font-size: 12px; overflow-x: auto; }
.tool-step.tc summary { color: #79c0ff; background: rgba(121,192,255,0.05); }
.tool-step.tc .detail-body { background: var(--tool-bg); border: 1px solid var(--border); }
.tool-step.tr summary { color: var(--system-fg); background: rgba(139,148,158,0.05); }
.tool-step.tr .detail-body { background: var(--tool-bg); border: 1px solid var(--border); }
.tool-step.tr.is-error summary { color: var(--error-border); }

/* ── Assistant 文本回复节点 ── */
.tl-text { margin: 8px 0; }
.tl-text::before { background: var(--assistant-border); width: 10px; height: 10px; left: -23px; top: 14px; }
.tl-text-card { background: var(--assistant-bg); border: 1px solid rgba(63,185,80,0.2);
                border-radius: 8px; padding: 12px 16px; }
.tl-text-card .tl-label { font-size: 11px; font-weight: 600; text-transform: uppercase;
                          color: var(--assistant-border); letter-spacing: 0.5px; margin-bottom: 4px; }
.tl-text-card .tl-label .tl-ts { color: var(--system-fg); font-weight: normal;
                                  float: right; font-size: 10px; text-transform: none; }

/* ── Thinking 节点 ── */
.tl-thinking { margin: 4px 0; }
.tl-thinking::before { background: var(--thinking-border); width: 8px; height: 8px; left: -22px; top: 10px; }
.tl-thinking > summary { list-style: none; cursor: pointer; user-select: none;
                         background: var(--thinking-bg); border: 1px solid var(--thinking-border);
                         border-radius: 6px; padding: 6px 12px; font-size: 12px; color: var(--thinking-border); }
.tl-thinking > summary::-webkit-details-marker { display: none; }
.tl-thinking > summary::before { content: '▶ '; font-size: 9px; }
.tl-thinking[open] > summary::before { content: '▼ '; }
.tl-thinking .detail-body { background: var(--thinking-bg); border: 1px solid var(--thinking-border);
                            border-radius: 6px; padding: 10px 14px; margin-top: 4px;
                            color: #c9d1d9; white-space: pre-wrap; font-size: 13px;
                            max-height: 500px; overflow-y: auto; }

/* ── System prompt 节点 ── */
.tl-system { margin: 4px 0; }
.tl-system::before { background: var(--system-fg); width: 8px; height: 8px; left: -22px; top: 10px; }
.tl-system > summary { list-style: none; cursor: pointer; user-select: none;
                       background: var(--system-bg); border: 1px solid var(--border);
                       border-radius: 6px; padding: 6px 12px; font-size: 12px; color: var(--system-fg); }
.tl-system > summary::-webkit-details-marker { display: none; }
.tl-system > summary::before { content: '▶ '; font-size: 9px; }
.tl-system[open] > summary::before { content: '▼ '; }
.tl-system .detail-body { background: var(--system-bg); border: 1px solid var(--border);
                          border-radius: 6px; padding: 10px 14px; margin-top: 4px;
                          color: var(--system-fg); white-space: pre-wrap; font-size: 12px;
                          max-height: 600px; overflow-y: auto; }

/* ── 空 assistant 占位 ── */
.tl-empty { margin: 2px 0; opacity: 0.5; }
.tl-empty::before { background: var(--timeline-dot); width: 6px; height: 6px; left: -21px; top: 10px; }
.tl-empty-inner { font-size: 11px; color: var(--system-fg); font-style: italic; padding: 2px 0; }

/* ── 通用 ── */
.msg-text { white-space: pre-wrap; word-break: break-word; font-size: 14px; }
.msg-text code { background: var(--code-bg); padding: 1px 4px; border-radius: 3px; font-size: 13px; }
.msg-text pre { background: var(--code-bg); padding: 10px; border-radius: 6px;
                overflow-x: auto; margin: 6px 0; font-size: 13px; }
.usage-tag { display: inline-block; font-size: 10px; color: var(--system-fg);
             background: var(--tool-bg); border: 1px solid var(--border);
             border-radius: 3px; padding: 0 4px; margin-left: 4px; }
mark { background: #6e4e00; color: #ffd700; border-radius: 2px; padding: 0 1px; }
.hidden { display: none !important; }

/* ── 索引页 ── */
.index-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.index-table th { text-align: left; padding: 8px; border-bottom: 2px solid var(--border);
                  color: var(--system-fg); font-size: 12px; text-transform: uppercase; }
.index-table td { padding: 8px; border-bottom: 1px solid var(--border); }
.index-table tr:hover td { background: var(--meta-bg); }

/* ── 响应式 ── */
@media (max-width: 768px) {
  .sidebar { display: none; }
  .main-content { margin-left: 0; padding: 12px; }
}
"""

_JS = """
function doSearch() {
  const q = document.getElementById('search-input').value.trim().toLowerCase();
  const nodes = document.querySelectorAll('.tl-node');
  let shown = 0;
  nodes.forEach(el => {
    if (!q) { el.classList.remove('hidden'); shown++; return; }
    const text = el.textContent.toLowerCase();
    if (text.includes(q)) { el.classList.remove('hidden'); shown++; }
    else { el.classList.add('hidden'); }
  });
  document.getElementById('search-stats').textContent = q ? shown + ' / ' + nodes.length + ' nodes' : '';
}

/* 侧边栏高亮当前可见节点 */
function initNavHighlight() {
  const navItems = document.querySelectorAll('.sidebar .nav-item');
  if (!navItems.length) return;
  const targets = [];
  navItems.forEach(a => {
    const id = a.getAttribute('href');
    if (id && id.startsWith('#')) {
      const el = document.getElementById(id.slice(1));
      if (el) targets.push({ nav: a, el: el });
    }
  });
  if (!targets.length) return;
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      const item = targets.find(t => t.el === e.target);
      if (item) item.visible = e.isIntersecting;
    });
    const first = targets.find(t => t.visible);
    navItems.forEach(a => a.classList.remove('active'));
    if (first) first.nav.classList.add('active');
  }, { rootMargin: '-80px 0px -60% 0px' });
  targets.forEach(t => obs.observe(t.el));
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('search-input');
  if (input) { let t; input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(doSearch, 200); }); }
  initNavHighlight();
});
"""


def _esc(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text)) if text else ""


def _format_content_text(text: str) -> str:
    """简单的 Markdown 渲染：代码块 + 行内代码"""
    if not text:
        return ""
    escaped = _esc(text)
    def _code_block(m):
        code = m.group(2)
        return f'<pre><code>{code}</code></pre>'
    escaped = re.sub(r'```(\w*)\n(.*?)```', _code_block, escaped, flags=re.DOTALL)
    escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)
    return escaped


def _truncate(text: str, limit: int = 5000) -> str:
    """截断过长文本"""
    if not text or len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated, {len(text)} chars total]"


def _format_usage(usage: Dict) -> str:
    """格式化 token 用量"""
    if not usage:
        return ""
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    parts = []
    if inp:
        parts.append(f"in:{inp}")
    if out:
        parts.append(f"out:{out}")
    if cache_read:
        parts.append(f"cache:{cache_read}")
    return " ".join(parts)


# ─────────────────────────────────────────────
# 渲染单个 .traj 文件
# ─────────────────────────────────────────────

def _build_tool_name_map(history: List[Dict]) -> Dict[str, str]:
    """从 history 中构建 tool_use_id -> tool_name 的映射"""
    mapping = {}
    for entry in history:
        if entry.get("role") != "assistant":
            continue
        content = entry.get("content", [])
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id", "")
                if tid:
                    mapping[tid] = b.get("name", "unknown")
        # 也从 tool_calls 字段补充（OpenAI 格式）
        for tc in (entry.get("tool_calls") or []):
            if isinstance(tc, dict):
                tid = tc.get("id", "")
                name = tc.get("function", {}).get("name", "")
                if tid and name:
                    mapping[tid] = name
    return mapping


def _tool_call_summary(tool_name: str, tool_input: Dict) -> str:
    """为工具调用生成摘要后缀"""
    if tool_name in ("Read", "read"):
        return f' — {_esc(tool_input.get("file_path", "")[-60:])}'
    elif tool_name in ("Edit", "edit", "Write", "write"):
        return f' — {_esc(tool_input.get("file_path", "")[-60:])}'
    elif tool_name in ("Bash", "bash"):
        cmd = tool_input.get("command", "")
        return f' — {_esc(cmd[:80])}'
    elif tool_name in ("Glob", "glob"):
        return f' — {_esc(tool_input.get("pattern", ""))}'
    elif tool_name in ("Grep", "grep"):
        return f' — {_esc(tool_input.get("pattern", "")[:40])}'
    elif tool_name == "Agent":
        return f' — {_esc(tool_input.get("description", "")[:50])}'
    elif tool_name in ("TaskCreate", "TaskUpdate"):
        return f' — {_esc(tool_input.get("subject", tool_input.get("taskId", ""))[:50])}'
    return ""


def _get_ts(entry: Dict) -> str:
    """安全提取 timestamp"""
    ts = entry.get("timestamp", "")
    return ts[:19] if isinstance(ts, str) else ""


def _parse_assistant_entry(entry: Dict) -> Dict:
    """解析 assistant history 条目，提取结构化信息"""
    content = entry.get("content", [])
    thinking_blocks = entry.get("thinking_blocks")
    tool_calls_field = entry.get("tool_calls") or []

    thinkings = []
    texts = []
    tool_uses = []

    # 从顶层 thinking_blocks
    if thinking_blocks:
        for tb in thinking_blocks:
            t = tb.get("thinking", "") if isinstance(tb, dict) else str(tb)
            if t:
                thinkings.append(t)

    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type", "")
            if btype == "thinking" and not thinking_blocks:
                t = b.get("thinking", "")
                if t:
                    thinkings.append(t)
            elif btype == "redacted_thinking":
                thinkings.append("[redacted]")
            elif btype == "text":
                t = b.get("text", "")
                if t.strip():
                    texts.append(t)
            elif btype == "tool_use":
                tool_uses.append({
                    "id": b.get("id", ""),
                    "name": b.get("name", "unknown"),
                    "input": b.get("input") or {},
                })
    elif isinstance(content, str) and content.strip():
        texts.append(content)

    # 从 tool_calls 字段补充
    if not tool_uses and tool_calls_field:
        for tc in tool_calls_field:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            name = func.get("name", "unknown")
            try:
                inp = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                inp = {"_raw": func.get("arguments", "")}
            tool_uses.append({"id": tc.get("id", ""), "name": name, "input": inp})

    return {
        "thinkings": thinkings,
        "texts": texts,
        "tool_uses": tool_uses,
        "usage": entry.get("usage", {}),
        "stop_reason": entry.get("stop_reason", ""),
        "timestamp": _get_ts(entry),
    }


def _linearize_history(history: List[Dict], tool_name_map: Dict[str, str]) -> List[Dict]:
    """将 history 转换为线性节点列表，合并连续工具调用为 tool_loop。

    节点类型:
      system, user_input, tool_loop, assistant_text, thinking, empty_assistant
    """
    nodes = []
    # 先收集所有 tool_result，按 tool_use_id 索引
    tool_results_by_id: Dict[str, Dict] = {}
    for entry in history:
        if entry.get("role") != "user":
            continue
        content = entry.get("content", [])
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id", "")
                if tid:
                    is_error = b.get("is_error", False)
                    rc = b.get("content", "")
                    if isinstance(rc, list):
                        rc = "\n".join(x.get("text", "") for x in rc if isinstance(x, dict))
                    tool_results_by_id[tid] = {"content": str(rc), "is_error": is_error}

    # 遍历 history 构建节点
    pending_tools = []  # 当前累积的工具调用步骤
    turn_num = 0

    def _flush_tools():
        """将累积的工具调用合并为一个 tool_loop 节点"""
        nonlocal pending_tools
        if not pending_tools:
            return
        nodes.append({
            "type": "tool_loop",
            "steps": list(pending_tools),
            "timestamp": pending_tools[0].get("timestamp", ""),
        })
        pending_tools = []

    for entry in history:
        role = entry.get("role", "")

        if role == "system":
            _flush_tools()
            content = entry.get("content", "")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            nodes.append({"type": "system", "text": text})

        elif role == "user":
            content = entry.get("content", "")
            # 判断是否为 tool_result
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if is_tool_result:
                continue  # tool_result 已通过 tool_results_by_id 索引，不单独渲染

            # 真实用户输入
            _flush_tools()
            if isinstance(content, str):
                if content.strip():
                    nodes.append({"type": "user_input", "text": content})
            elif isinstance(content, list):
                text_parts = [b.get("text", "") for b in content
                              if isinstance(b, dict) and b.get("type") == "text"]
                combined = "\n".join(t for t in text_parts if t.strip())
                if combined.strip():
                    nodes.append({"type": "user_input", "text": combined})

        elif role == "assistant":
            turn_num += 1
            parsed = _parse_assistant_entry(entry)

            # Thinking — 独立节点
            for t in parsed["thinkings"]:
                _flush_tools()
                nodes.append({"type": "thinking", "text": t, "timestamp": parsed["timestamp"]})

            # Text — 独立节点（先 flush 工具）
            if parsed["texts"]:
                _flush_tools()
                nodes.append({
                    "type": "assistant_text",
                    "texts": parsed["texts"],
                    "turn_num": turn_num,
                    "usage": parsed["usage"],
                    "stop_reason": parsed["stop_reason"],
                    "timestamp": parsed["timestamp"],
                })

            # Tool uses — 累积到 pending_tools
            if parsed["tool_uses"]:
                for tu in parsed["tool_uses"]:
                    result = tool_results_by_id.get(tu["id"], {})
                    pending_tools.append({
                        "name": tu["name"],
                        "input": tu["input"],
                        "result_content": result.get("content", ""),
                        "is_error": result.get("is_error", False),
                        "timestamp": parsed["timestamp"],
                        "usage": parsed["usage"],
                    })

            # 空 assistant（无 text、无 tool、无 thinking）
            if not parsed["texts"] and not parsed["tool_uses"] and not parsed["thinkings"]:
                nodes.append({
                    "type": "empty_assistant",
                    "turn_num": turn_num,
                    "usage": parsed["usage"],
                    "stop_reason": parsed["stop_reason"],
                    "timestamp": parsed["timestamp"],
                })

    _flush_tools()
    return nodes


def render_traj(traj: Dict) -> str:
    """将 .traj 数据渲染为时间线 HTML"""
    metadata = traj.get("metadata", {})
    history = traj.get("history", [])
    tool_name_map = _build_tool_name_map(history)
    nodes = _linearize_history(history, tool_name_map)

    parts: List[str] = []
    nav_items: List[str] = []  # 侧边栏导航

    # ── 元信息卡片 ──
    sid = metadata.get("session_id", "unknown")
    model = metadata.get("model", "unknown")
    start = metadata.get("start_time", "")[:19]
    end = metadata.get("end_time", "")[:19]
    steps = metadata.get("total_steps", 0)
    api_calls = metadata.get("total_api_calls", 0)
    tokens_in = metadata.get("total_tokens_sent", 0)
    tokens_out = metadata.get("total_tokens_received", 0)
    cost = metadata.get("total_cost_usd", 0)
    exit_status = metadata.get("exit_status", "")
    tools_used = metadata.get("tools_used", [])
    cwd = metadata.get("working_directory", "")

    parts.append(f"""
    <div class="meta-card">
      <h1>Session {_esc(sid[:12])}...</h1>
      <div class="meta-grid">
        <div class="meta-item">Model: <strong>{_esc(model)}</strong></div>
        <div class="meta-item">Start: <strong>{_esc(start)}</strong></div>
        <div class="meta-item">End: <strong>{_esc(end)}</strong></div>
        <div class="meta-item">API Calls: <strong>{api_calls}</strong></div>
        <div class="meta-item">Steps: <strong>{steps}</strong></div>
        <div class="meta-item">Tokens: <strong>{tokens_in:,} in / {tokens_out:,} out</strong></div>
        <div class="meta-item">Cost: <strong>${cost:.4f}</strong></div>
        <div class="meta-item">Exit: <strong>{_esc(exit_status)}</strong></div>
        {"<div class='meta-item'>CWD: <strong>" + _esc(cwd) + "</strong></div>" if cwd else ""}
      </div>
      {"<div class='tools-list'>Tools: " + " ".join(f"<span>{_esc(t)}</span>" for t in tools_used) + "</div>" if tools_used else ""}
    </div>
    """)

    # ── 搜索栏 ──
    parts.append("""
    <div class="search-bar">
      <input id="search-input" type="text" placeholder="Search in timeline..." />
      <div class="stats" id="search-stats"></div>
    </div>
    """)

    # ── 时间线 ──
    parts.append('<div class="timeline">')
    node_id = 0
    user_count = 0
    text_count = 0
    loop_count = 0

    for nd in nodes:
        ntype = nd["type"]
        node_id += 1
        anchor = f"n{node_id}"

        if ntype == "system":
            text = nd["text"]
            parts.append(f"""
            <details class="tl-node tl-system" id="{anchor}">
              <summary>System Prompt ({len(text):,} chars)</summary>
              <div class="detail-body">{_esc(_truncate(text, 10000))}</div>
            </details>
            """)

        elif ntype == "user_input":
            user_count += 1
            text = nd["text"]
            preview = text.strip()[:60].replace("\n", " ")
            nav_items.append(
                f'<a class="nav-item nav-user" href="#{anchor}">'
                f'User: {_esc(preview)}</a>'
            )
            parts.append(f"""
            <div class="tl-node tl-user" id="{anchor}">
              <div class="tl-user-card">
                <div class="tl-label">User #{user_count}</div>
                <div class="msg-text">{_format_content_text(text)}</div>
              </div>
            </div>
            """)

        elif ntype == "thinking":
            text = nd["text"]
            ts = nd.get("timestamp", "")
            if text == "[redacted]":
                parts.append(f"""
                <details class="tl-node tl-thinking" id="{anchor}">
                  <summary>Redacted Thinking</summary>
                  <div class="detail-body">[content redacted by API]</div>
                </details>
                """)
            else:
                parts.append(f"""
                <details class="tl-node tl-thinking" id="{anchor}">
                  <summary>Thinking ({len(text):,} chars){f' <span class="usage-tag">{_esc(ts)}</span>' if ts else ''}</summary>
                  <div class="detail-body">{_esc(_truncate(text, 8000))}</div>
                </details>
                """)

        elif ntype == "tool_loop":
            loop_count += 1
            loop_steps = nd["steps"]
            # 统计工具名
            from collections import Counter
            tool_counts = Counter(s["name"] for s in loop_steps)
            has_error = any(s.get("is_error") for s in loop_steps)
            pills_html = " ".join(
                f'<span class="tool-pill">{_esc(name)}'
                f'{"&times;" + str(cnt) if cnt > 1 else ""}</span>'
                for name, cnt in tool_counts.items()
            )
            if has_error:
                pills_html += ' <span class="tool-pill t-error">ERROR</span>'

            nav_items.append(
                f'<a class="nav-item nav-step" href="#{anchor}">'
                f'{", ".join(tool_counts.keys())}'
                f'<span class="nav-badge">{len(loop_steps)}</span></a>'
            )

            # 内部展开的每个工具步骤
            step_parts = []
            for si, step in enumerate(loop_steps):
                sname = step["name"]
                sinput = step["input"]
                input_str = json.dumps(sinput, ensure_ascii=False, indent=2)
                summary_extra = _tool_call_summary(sname, sinput)
                step_parts.append(f"""
                <details class="tool-step tc">
                  <summary>Tool: {_esc(sname)}{summary_extra}</summary>
                  <div class="detail-body"><pre>{_esc(_truncate(input_str, 5000))}</pre></div>
                </details>
                """)
                # 对应的 result
                rc = step.get("result_content", "")
                if rc:
                    rc = _truncate(rc, 8000)
                    err_cls = " is-error" if step.get("is_error") else ""
                    err_label = " [ERROR]" if step.get("is_error") else ""
                    step_parts.append(f"""
                    <details class="tool-step tr{err_cls}">
                      <summary>Result [{_esc(sname)}]{_esc(err_label)} ({len(rc):,} chars)</summary>
                      <div class="detail-body"><pre>{_esc(rc)}</pre></div>
                    </details>
                    """)

            parts.append(f"""
            <details class="tl-node tl-toolloop" id="{anchor}">
              <summary>
                <span class="loop-label">Tools</span>
                <span class="loop-count">({len(loop_steps)} calls)</span>
                {pills_html}
              </summary>
              <div class="loop-body">
                {"".join(step_parts)}
              </div>
            </details>
            """)

        elif ntype == "assistant_text":
            text_count += 1
            texts = nd["texts"]
            usage = nd.get("usage", {})
            stop_reason = nd.get("stop_reason", "")
            ts = nd.get("timestamp", "")
            turn = nd.get("turn_num", 0)
            usage_str = _format_usage(usage)
            usage_html = f'<span class="usage-tag">{_esc(usage_str)}</span>' if usage_str else ""
            stop_html = f'<span class="usage-tag">{_esc(stop_reason)}</span>' if stop_reason else ""
            ts_html = f'<span class="tl-ts">{_esc(ts)}</span>' if ts else ""

            preview = texts[0].strip()[:50].replace("\n", " ")
            nav_items.append(
                f'<a class="nav-item" href="#{anchor}">'
                f'#{turn} {_esc(preview)}</a>'
            )

            text_html = "\n".join(
                f'<div class="msg-text">{_format_content_text(t)}</div>' for t in texts
            )
            parts.append(f"""
            <div class="tl-node tl-text" id="{anchor}">
              <div class="tl-text-card">
                <div class="tl-label">Assistant #{turn} {usage_html}{stop_html}{ts_html}</div>
                {text_html}
              </div>
            </div>
            """)

        elif ntype == "empty_assistant":
            turn = nd.get("turn_num", 0)
            usage = nd.get("usage", {})
            ts = nd.get("timestamp", "")
            usage_str = _format_usage(usage)
            parts.append(f"""
            <div class="tl-node tl-empty" id="{anchor}">
              <div class="tl-empty-inner">
                #{turn} [empty]{f" {_esc(usage_str)}" if usage_str else ""}{f" {_esc(ts)}" if ts else ""}
              </div>
            </div>
            """)

    parts.append('</div>')  # end .timeline

    # ── 构建侧边栏 ──
    sidebar = f"""
    <nav class="sidebar">
      <div class="sid">{_esc(sid[:12])}...</div>
      <div class="nav-section">Navigation</div>
      {"".join(nav_items)}
    </nav>
    """

    return sidebar + '<div class="main-content">' + "\n".join(parts) + '</div>'


def generate_html(traj: Dict, title: str = "Trajectory Viewer") -> str:
    """生成完整的 HTML 页面"""
    body = render_traj(traj)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page-layout">
{body}
</div>
<script>{_JS}</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# 目录索引页
# ─────────────────────────────────────────────

def generate_index_html(traj_dir: Path) -> str:
    """生成目录索引页"""
    # 新布局：sessions/*/session.traj；旧布局：*.traj
    traj_files = sorted(traj_dir.glob("*/session.traj"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not traj_files:
        traj_files = sorted(traj_dir.glob("*.traj"), key=lambda f: f.stat().st_mtime, reverse=True)

    rows = []
    for f in traj_files:
        try:
            traj = json.loads(f.read_text())
            meta = traj.get("metadata", {})
            sid = meta.get("session_id", f.stem)
            model = meta.get("model", "?")
            start = meta.get("start_time", "")[:19]
            steps = meta.get("total_steps", 0)
            api_calls = meta.get("total_api_calls", 0)
            cost = meta.get("total_cost_usd", 0)
            exit_status = meta.get("exit_status", "")
            size_kb = f.stat().st_size / 1024
            html_name = f.stem + ".html"
            rows.append(f"""
            <tr>
              <td><a href="{_esc(html_name)}">{_esc(sid[:12])}...</a></td>
              <td>{_esc(model)}</td>
              <td>{_esc(start)}</td>
              <td>{steps}</td>
              <td>{api_calls}</td>
              <td>${cost:.3f}</td>
              <td>{_esc(exit_status)}</td>
              <td>{size_kb:.0f} KB</td>
            </tr>
            """)
        except Exception as e:
            rows.append(f'<tr><td colspan="8">Error loading {f.name}: {e}</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Trajectory Index</title>
<style>{_CSS}</style>
</head>
<body>
<div class="meta-card">
  <h1>Trajectory Index</h1>
  <div class="meta-item">{len(traj_files)} sessions in {_esc(str(traj_dir))}</div>
</div>
<table class="index-table">
  <thead>
    <tr><th>Session</th><th>Model</th><th>Start</th><th>Steps</th><th>API Calls</th><th>Cost</th><th>Exit</th><th>Size</th></tr>
  </thead>
  <tbody>
    {"".join(rows)}
  </tbody>
</table>
</body>
</html>"""


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trajectory HTML Viewer")
    parser.add_argument("path", help=".traj file or directory")
    parser.add_argument("-o", "--output", help="Output HTML path")
    parser.add_argument("--open", action="store_true", default=True, help="Open in browser (default)")
    parser.add_argument("--no-open", action="store_true", help="Don't open in browser")
    args = parser.parse_args()

    path = Path(args.path)

    if path.is_dir():
        # 目录模式：生成索引页 + 每个 traj 的 HTML
        out_dir = Path(args.output) if args.output else path
        if args.output:
            out_dir.mkdir(parents=True, exist_ok=True)

        # 生成每个 traj 的 HTML（新布局 + 旧布局）
        traj_files = sorted(path.glob("*/session.traj"))
        if not traj_files:
            traj_files = sorted(path.glob("*.traj"))
        for f in traj_files:
            try:
                sid = f.parent.name if f.name == "session.traj" else f.stem
                traj = json.loads(f.read_text())
                html_content = generate_html(traj, title=f"Session {sid[:12]}")
                html_path = out_dir / (sid + ".html")
                html_path.write_text(html_content)
                print(f"  {sid}.traj -> {html_path.name}")
            except Exception as e:
                print(f"  ERROR {f.name}: {e}", file=sys.stderr)

        # 生成索引页
        index_html = generate_index_html(path)
        index_path = out_dir / "index.html"
        index_path.write_text(index_html)
        print(f"\nIndex: {index_path}")

        if not args.no_open:
            import webbrowser
            webbrowser.open(f"file://{index_path.resolve()}")

    elif path.is_file() and path.suffix == ".traj":
        # 单文件模式
        traj = json.loads(path.read_text())
        html_content = generate_html(traj, title=f"Session {path.stem[:12]}")

        out_path = Path(args.output) if args.output else path.with_suffix(".html")
        out_path.write_text(html_content)
        print(f"Generated: {out_path}")

        if not args.no_open:
            import webbrowser
            webbrowser.open(f"file://{out_path.resolve()}")
    else:
        print(f"Error: {path} is not a .traj file or directory", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
