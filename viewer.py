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
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
       background: var(--bg); color: var(--fg); line-height: 1.6;
       max-width: 960px; margin: 0 auto; padding: 16px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* 元信息卡片 */
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

/* 搜索栏 */
.search-bar { position: sticky; top: 0; z-index: 100; background: var(--bg);
              padding: 8px 0; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
.search-bar input { width: 100%; padding: 8px 12px; background: var(--meta-bg);
                    border: 1px solid var(--border); border-radius: 6px;
                    color: var(--fg); font-size: 14px; outline: none; }
.search-bar input:focus { border-color: var(--accent); }
.search-bar .stats { font-size: 12px; color: var(--system-fg); margin-top: 4px; }

/* 消息气泡 */
.msg { margin: 6px 0; padding: 10px 14px; border-radius: 8px;
       border-left: 3px solid transparent; position: relative; }
.msg-system { background: var(--system-bg); border-left-color: var(--system-fg);
              color: var(--system-fg); font-size: 12px; }
.msg-user { background: var(--user-bg); border-left-color: var(--user-border); }
.msg-assistant { background: var(--assistant-bg); border-left-color: var(--assistant-border); }
.msg-error { background: var(--error-bg); border-left-color: var(--error-border); }

.msg-label { font-size: 11px; font-weight: 600; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 4px; }
.msg-system .msg-label { color: var(--system-fg); }
.msg-user .msg-label { color: var(--user-border); }
.msg-assistant .msg-label { color: var(--assistant-border); }
.msg-assistant .msg-label .turn-num { color: var(--system-fg); font-weight: normal; }
.msg-ts { font-size: 10px; color: var(--system-fg); float: right; }

/* 文本内容 */
.msg-text { white-space: pre-wrap; word-break: break-word; font-size: 14px; }
.msg-text code { background: var(--code-bg); padding: 1px 4px; border-radius: 3px;
                 font-size: 13px; }
.msg-text pre { background: var(--code-bg); padding: 10px; border-radius: 6px;
                overflow-x: auto; margin: 6px 0; font-size: 13px; }

/* 折叠块 */
details { margin: 6px 0; }
summary { cursor: pointer; font-size: 13px; font-weight: 500; padding: 4px 8px;
          border-radius: 4px; user-select: none; list-style: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: '▶ '; font-size: 10px; }
details[open] > summary::before { content: '▼ '; }
details > .detail-body { padding: 8px 12px; margin-top: 4px; border-radius: 4px;
                         font-size: 13px; overflow-x: auto; }

/* 工具调用 */
.tool-call summary { background: var(--tool-bg); border: 1px solid var(--tool-border); color: #79c0ff; }
.tool-call .detail-body { background: var(--tool-bg); border: 1px solid var(--border); }
.tool-result summary { background: var(--tool-bg); border: 1px solid var(--border); color: var(--system-fg); }
.tool-result .detail-body { background: var(--tool-bg); border: 1px solid var(--border); }
.tool-result.is-error summary { color: var(--error-border); border-color: var(--error-border); }

/* Thinking */
.thinking summary { background: var(--thinking-bg); border: 1px solid var(--thinking-border); color: var(--thinking-border); }
.thinking .detail-body { background: var(--thinking-bg); border: 1px solid var(--thinking-border);
                         color: #c9d1d9; white-space: pre-wrap; }

/* System prompt */
.system-prompt summary { background: var(--system-bg); border: 1px solid var(--border); color: var(--system-fg); }
.system-prompt .detail-body { background: var(--system-bg); border: 1px solid var(--border);
                              color: var(--system-fg); white-space: pre-wrap; max-height: 600px; overflow-y: auto; }

/* Usage 标签 */
.usage-tag { display: inline-block; font-size: 11px; color: var(--system-fg);
             background: var(--tool-bg); border: 1px solid var(--border);
             border-radius: 3px; padding: 0 5px; margin-left: 6px; }

/* 搜索高亮 */
mark { background: #6e4e00; color: #ffd700; border-radius: 2px; padding: 0 1px; }
.hidden { display: none !important; }

/* 索引页 */
.index-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.index-table th { text-align: left; padding: 8px; border-bottom: 2px solid var(--border);
                  color: var(--system-fg); font-size: 12px; text-transform: uppercase; }
.index-table td { padding: 8px; border-bottom: 1px solid var(--border); }
.index-table tr:hover td { background: var(--meta-bg); }
"""

_JS = """
function doSearch() {
  const q = document.getElementById('search-input').value.trim().toLowerCase();
  const msgs = document.querySelectorAll('.msg');
  let shown = 0;
  msgs.forEach(el => {
    if (!q) { el.classList.remove('hidden'); shown++; return; }
    const text = el.textContent.toLowerCase();
    if (text.includes(q)) { el.classList.remove('hidden'); shown++; }
    else { el.classList.add('hidden'); }
  });
  document.getElementById('search-stats').textContent = q ? shown + ' / ' + msgs.length + ' messages' : '';
}
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('search-input');
  if (input) { let t; input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(doSearch, 200); }); }
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
    # 代码块 ```...```
    import re
    def _code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        return f'<pre><code>{code}</code></pre>'
    escaped = re.sub(r'```(\w*)\n(.*?)```', _code_block, escaped, flags=re.DOTALL)
    # 行内代码
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

def render_traj(traj: Dict) -> str:
    """将 .traj 数据渲染为 HTML body 内容"""
    metadata = traj.get("metadata", {})
    info = traj.get("info", {})
    history = traj.get("history", [])

    parts: List[str] = []

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
      <input id="search-input" type="text" placeholder="Search messages..." />
      <div class="stats" id="search-stats"></div>
    </div>
    """)

    # ── 对话流 ──
    turn_num = 0
    for idx, entry in enumerate(history):
        role = entry.get("role", "")
        content = entry.get("content", "")
        timestamp = entry.get("timestamp", "")[:19]
        usage = entry.get("usage", {})
        stop_reason = entry.get("stop_reason", "")
        thinking_blocks = entry.get("thinking_blocks")
        tool_calls = entry.get("tool_calls")

        if role == "system":
            # System prompt — 折叠
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            parts.append(f"""
            <div class="msg msg-system">
              <details class="system-prompt">
                <summary>System Prompt ({len(text):,} chars)</summary>
                <div class="detail-body">{_esc(_truncate(text, 10000))}</div>
              </details>
            </div>
            """)

        elif role == "user":
            if isinstance(content, str):
                # 纯文本用户输入
                if not content.strip():
                    continue
                parts.append(f"""
                <div class="msg msg-user">
                  <div class="msg-label">User {f'<span class="msg-ts">{_esc(timestamp)}</span>' if timestamp else ''}</div>
                  <div class="msg-text">{_format_content_text(content)}</div>
                </div>
                """)
            elif isinstance(content, list):
                # Content blocks (tool_result 或 text blocks)
                has_text = any(b.get("type") == "text" for b in content if isinstance(b, dict))
                has_tool_result = any(b.get("type") == "tool_result" for b in content if isinstance(b, dict))

                if has_text and not has_tool_result:
                    # 纯文本 blocks
                    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    combined = "\n".join(t for t in texts if t.strip())
                    if combined.strip():
                        parts.append(f"""
                        <div class="msg msg-user">
                          <div class="msg-label">User</div>
                          <div class="msg-text">{_format_content_text(combined)}</div>
                        </div>
                        """)

                if has_tool_result:
                    # Tool results
                    result_parts = []
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_result":
                            continue
                        tool_id = b.get("tool_use_id", "")
                        is_error = b.get("is_error", False)
                        result_content = b.get("content", "")
                        if isinstance(result_content, list):
                            result_content = "\n".join(
                                x.get("text", "") for x in result_content if isinstance(x, dict)
                            )
                        result_content = _truncate(str(result_content), 8000)
                        error_cls = " is-error" if is_error else ""
                        error_label = " [ERROR]" if is_error else ""
                        result_parts.append(f"""
                        <details class="tool-result{error_cls}">
                          <summary>Result{_esc(error_label)} ({len(result_content):,} chars)</summary>
                          <div class="detail-body"><pre>{_esc(result_content)}</pre></div>
                        </details>
                        """)
                    if result_parts:
                        parts.append(f"""
                        <div class="msg msg-user">
                          <div class="msg-label" style="color: var(--system-fg)">Tool Results</div>
                          {"".join(result_parts)}
                        </div>
                        """)

        elif role == "assistant":
            turn_num += 1
            usage_str = _format_usage(usage)
            usage_html = f'<span class="usage-tag">{_esc(usage_str)}</span>' if usage_str else ""
            stop_html = f'<span class="usage-tag">{_esc(stop_reason)}</span>' if stop_reason else ""

            inner_parts = []

            # Thinking blocks
            if thinking_blocks:
                for tb in thinking_blocks:
                    thinking_text = tb.get("thinking", "") if isinstance(tb, dict) else str(tb)
                    if thinking_text:
                        inner_parts.append(f"""
                        <details class="thinking">
                          <summary>Thinking ({len(thinking_text):,} chars)</summary>
                          <div class="detail-body">{_esc(_truncate(thinking_text, 8000))}</div>
                        </details>
                        """)

            # Content blocks
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type", "")
                    if btype == "thinking":
                        # 已在上面处理
                        if not thinking_blocks:
                            thinking_text = b.get("thinking", "")
                            if thinking_text:
                                inner_parts.append(f"""
                                <details class="thinking">
                                  <summary>Thinking ({len(thinking_text):,} chars)</summary>
                                  <div class="detail-body">{_esc(_truncate(thinking_text, 8000))}</div>
                                </details>
                                """)
                    elif btype == "text":
                        text = b.get("text", "")
                        if text.strip():
                            inner_parts.append(f'<div class="msg-text">{_format_content_text(text)}</div>')
                    elif btype == "tool_use":
                        tool_name = b.get("name", "unknown")
                        tool_input = b.get("input", {})
                        input_str = json.dumps(tool_input, ensure_ascii=False, indent=2)
                        # 对常见工具显示关键参数摘要
                        summary_extra = ""
                        if tool_name in ("Read", "read"):
                            summary_extra = f' — {_esc(tool_input.get("file_path", "")[-60:])}'
                        elif tool_name in ("Edit", "edit"):
                            summary_extra = f' — {_esc(tool_input.get("file_path", "")[-60:])}'
                        elif tool_name in ("Write", "write"):
                            summary_extra = f' — {_esc(tool_input.get("file_path", "")[-60:])}'
                        elif tool_name in ("Bash", "bash"):
                            cmd = tool_input.get("command", "")
                            summary_extra = f' — {_esc(cmd[:60])}'
                        elif tool_name in ("Glob", "glob"):
                            summary_extra = f' — {_esc(tool_input.get("pattern", ""))}'
                        elif tool_name in ("Grep", "grep"):
                            summary_extra = f' — {_esc(tool_input.get("pattern", "")[:40])}'
                        elif tool_name == "Agent":
                            summary_extra = f' — {_esc(tool_input.get("description", "")[:50])}'

                        inner_parts.append(f"""
                        <details class="tool-call">
                          <summary>Tool: {_esc(tool_name)}{summary_extra}</summary>
                          <div class="detail-body"><pre>{_esc(_truncate(input_str, 5000))}</pre></div>
                        </details>
                        """)
            elif isinstance(content, str) and content.strip():
                inner_parts.append(f'<div class="msg-text">{_format_content_text(content)}</div>')

            if inner_parts:
                parts.append(f"""
                <div class="msg msg-assistant">
                  <div class="msg-label">Assistant <span class="turn-num">#{turn_num}</span>
                    {usage_html}{stop_html}
                    {f'<span class="msg-ts">{_esc(timestamp)}</span>' if timestamp else ''}
                  </div>
                  {"".join(inner_parts)}
                </div>
                """)

    return "\n".join(parts)


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
{body}
<script>{_JS}</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# 目录索引页
# ─────────────────────────────────────────────

def generate_index_html(traj_dir: Path) -> str:
    """生成目录索引页"""
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
            tools = meta.get("tools_used", [])
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

        # 生成每个 traj 的 HTML
        traj_files = sorted(path.glob("*.traj"))
        for f in traj_files:
            try:
                traj = json.loads(f.read_text())
                html_content = generate_html(traj, title=f"Session {f.stem[:12]}")
                html_path = out_dir / (f.stem + ".html")
                html_path.write_text(html_content)
                print(f"  {f.name} -> {html_path.name}")
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
