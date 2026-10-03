#!/usr/bin/env python3
"""
events_coverage.py — 统计 hook events 对代理会话的覆盖率（只读）

问题：切分 user step 要靠 hook 的 UserPromptSubmit 对齐。若有 raw.jsonl 的会话里
events.jsonl 大面积缺失，下游只能信 raw.jsonl。本工具把这个比例打出来。

会话按 raw.jsonl 首行请求体 metadata.user_id 里的真实 session_id 分四类：
  main     目录名 == 请求 session_id（策略 0 命中，应当有 events）
  child    请求 session_id 指向另一个已存在的会话目录（sub-agent / 标题生成，本就没有 events）
  orphan   请求带 session_id，但目录名对不上、也找不到父会话（关联失败）
  probe    count_tokens 探测请求（无 max_tokens），不是会话，见 rebuild_trajs.py
  legacy   请求里没有 session_id（老版本 Claude Code，无法确定性关联）

覆盖率分母只取 main + orphan —— child 本来就不该有 events，legacy 无从判断。
orphan 偏多说明策略 0 没生效，先查 session_id，不要加 hook。

用法：
    python3 tools/events_coverage.py --input ~/.claude-trace/trajectories/sessions
    python3 tools/events_coverage.py --input ... --since 2026-09-01 --by-month
    python3 tools/events_coverage.py --input ... --json
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Optional

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SESSION_SUFFIX_RE = re.compile(r"_session_([0-9a-fA-F-]{36})$")

# 覆盖率低于这个值就告警（只针对 main + orphan）
DEFAULT_THRESHOLD = 0.8

CATEGORIES = ("main", "child", "orphan", "probe", "legacy")


def extract_request_session_id(request: Dict) -> str:
    """从请求体 metadata.user_id 取真实 session_id（与 proxy.py 策略 0 同口径）"""
    metadata = request.get("metadata") if isinstance(request, dict) else None
    if not isinstance(metadata, dict):
        return ""
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return ""
    # 格式 1：JSON 串
    if user_id.lstrip().startswith("{"):
        try:
            sid = json.loads(user_id).get("session_id")
            if isinstance(sid, str) and _UUID_RE.match(sid):
                return sid
        except (json.JSONDecodeError, AttributeError):
            pass
    # 格式 2：user_<hash>_account__session_<uuid>
    m = _SESSION_SUFFIX_RE.search(user_id)
    return m.group(1) if m else ""


def read_first_record(raw_path: Path) -> Optional[Dict]:
    """只读 raw.jsonl 首行（首行保存完整 request_body，足够取 session_id 与时间）"""
    try:
        with raw_path.open("r", encoding="utf-8") as f:
            line = f.readline()
        rec = json.loads(line)
        return rec if isinstance(rec, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def scan_events(events_path: Path) -> Dict[str, bool]:
    """events.jsonl 是否非空、是否含 UserPromptSubmit"""
    result = {"nonempty": False, "has_prompt": False}
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                result["nonempty"] = True
                if isinstance(ev, dict) and ev.get("event") == "UserPromptSubmit":
                    result["has_prompt"] = True
                    break
    except (OSError, UnicodeDecodeError):
        pass
    return result


def classify(dir_name: str, req_sid: str, known_dirs: set, is_probe: bool = False) -> str:
    if is_probe:
        return "probe"
    if not req_sid:
        return "legacy"
    if req_sid == dir_name:
        return "main"
    # 只有父会话目录真实存在才算 child。父 sid 只出现在 hook 文件里、却没有
    # 同名目录 —— 这是策略 0 之前的主会话被落进随机 uuid 目录，属于关联失败
    if req_sid in known_dirs:
        return "child"
    return "orphan"


def collect(sessions_dir: Path, events_dir: Optional[Path], since: str = "") -> list:
    """扫描所有有 raw.jsonl 的会话，返回逐会话记录"""
    dirs = [d for d in sessions_dir.iterdir() if d.is_dir()]
    known_dirs = {d.name for d in dirs}
    hook_sids = set()
    if events_dir and events_dir.is_dir():
        hook_sids = {p.stem for p in events_dir.glob("*.jsonl")}

    rows = []
    for d in dirs:
        raw = d / "raw.jsonl"
        if not raw.is_file():
            continue
        rec = read_first_record(raw)
        if rec is None:
            rows.append({"sid": d.name, "category": "unreadable", "month": "", "ts": "",
                         "nonempty": False, "has_prompt": False, "hook_file": False})
            continue
        ts = str(rec.get("timestamp", ""))
        if since and ts[:10] < since:
            continue
        request = rec.get("request") or {}
        req_sid = extract_request_session_id(request)
        # count_tokens 请求体没有 max_tokens，曾被当成会话落盘
        is_probe = isinstance(request, dict) and "max_tokens" not in request
        cat = classify(d.name, req_sid, known_dirs, is_probe)
        ev = scan_events(d / "events.jsonl")
        rows.append({
            "sid": d.name,
            "category": cat,
            "month": ts[:7],
            "ts": ts,
            "nonempty": ev["nonempty"],
            "has_prompt": ev["has_prompt"],
            # hook 原始文件在、会话目录却没 events → 是复制环节漏了，不是 hook 没触发
            "hook_file": d.name in hook_sids,
        })
    return rows


def summarize(rows: list) -> Dict:
    cats = Counter(r["category"] for r in rows)
    denom = [r for r in rows if r["category"] in ("main", "orphan")]
    n = len(denom)
    nonempty = sum(r["nonempty"] for r in denom)
    prompt = sum(r["has_prompt"] for r in denom)
    copy_missed = sum(1 for r in denom if r["hook_file"] and not r["nonempty"])
    return {
        "raw_sessions": len(rows),
        "by_category": {c: cats.get(c, 0) for c in CATEGORIES + ("unreadable",)},
        "denominator": n,
        "events_nonempty": nonempty,
        "events_nonempty_ratio": nonempty / n if n else 0.0,
        "has_user_prompt": prompt,
        "has_user_prompt_ratio": prompt / n if n else 0.0,
        "orphan_ratio": cats.get("orphan", 0) / n if n else 0.0,
        "hook_file_but_not_copied": copy_missed,
        # 旧口径：所有有 raw 的会话为分母（09-04 的 38% / 22.9% 是这个口径）
        "legacy_nonempty_ratio": sum(r["nonempty"] for r in rows) / len(rows) if rows else 0.0,
        "legacy_prompt_ratio": sum(r["has_prompt"] for r in rows) / len(rows) if rows else 0.0,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def print_report(summary: Dict, by_month: Optional[Dict], threshold: float):
    s = summary
    print(f"有 raw.jsonl 的会话: {s['raw_sessions']}")
    print("  分类: " + "  ".join(f"{k}={v}" for k, v in s["by_category"].items()))
    print()
    print(f"分母（main + orphan，应当有 events 的会话）: {s['denominator']}")
    print(f"  events.jsonl 非空:     {s['events_nonempty']:>6}  {_pct(s['events_nonempty_ratio'])}")
    print(f"  含 UserPromptSubmit:   {s['has_user_prompt']:>6}  {_pct(s['has_user_prompt_ratio'])}")
    print(f"  orphan（策略 0 未命中）: {s['by_category']['orphan']:>6}  {_pct(s['orphan_ratio'])}")
    # events 在会话结束时才复制，进行中的会话也会落在这里，少量不算缺陷
    print(f"  hook 文件在但未复制进会话目录: {s['hook_file_but_not_copied']}（含进行中的会话）")
    print()
    print(f"旧口径（全部 raw 会话为分母）: 非空 {_pct(s['legacy_nonempty_ratio'])}"
          f" / UserPromptSubmit {_pct(s['legacy_prompt_ratio'])}")

    if by_month:
        print()
        print(f"{'月份':<8} {'main':>6} {'child':>6} {'orphan':>7} {'probe':>6} {'legacy':>7}"
              f" {'events%':>8} {'prompt%':>8}")
        for month in sorted(by_month):
            m = by_month[month]
            c = m["by_category"]
            print(f"{month or '?':<8} {c['main']:>6} {c['child']:>6} {c['orphan']:>7}"
                  f" {c['probe']:>6} {c['legacy']:>7}"
                  f" {_pct(m['events_nonempty_ratio']):>8} {_pct(m['has_user_prompt_ratio']):>8}")

    print()
    if s["denominator"] == 0:
        print("结论：样本内没有可判定的会话，无法给出覆盖率。")
    elif s["has_user_prompt_ratio"] < threshold:
        missing = s["denominator"] - s["has_user_prompt"]
        print(f"告警：{missing} 个会话缺 UserPromptSubmit，"
              f"这些会话的 user step 切分将只能信 raw.jsonl。")
        if s["orphan_ratio"] > 0.05:
            print("      orphan 占比偏高，先查请求体 session_id 关联（策略 0），不要加 hook。")
    else:
        print(f"结论：覆盖率可接受（UserPromptSubmit ≥ {_pct(threshold)}）。")


def main():
    parser = argparse.ArgumentParser(description="统计 hook events 对代理会话的覆盖率（只读）")
    parser.add_argument("--input", required=True, help="会话目录（sessions/）")
    parser.add_argument("--events-dir", default=str(Path.home() / ".claude" / "trajectory_events"),
                        help="hook 原始事件目录，用于识别父会话与复制遗漏")
    parser.add_argument("--since", default="", help="只统计首条请求时间 ≥ 该日期的会话（YYYY-MM-DD）")
    parser.add_argument("--by-month", action="store_true", help="按月拆分")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"UserPromptSubmit 覆盖率告警阈值（默认 {DEFAULT_THRESHOLD}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    sessions_dir = Path(args.input).expanduser()
    if not sessions_dir.is_dir():
        print(f"目录不存在: {sessions_dir}", file=sys.stderr)
        sys.exit(2)

    rows = collect(sessions_dir, Path(args.events_dir).expanduser(), since=args.since)
    summary = summarize(rows)

    by_month = None
    if args.by_month:
        groups = defaultdict(list)
        for r in rows:
            groups[r["month"]].append(r)
        by_month = {m: summarize(rs) for m, rs in groups.items()}

    if args.json:
        out = {"summary": summary}
        if by_month:
            out["by_month"] = by_month
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print_report(summary, by_month, args.threshold)


if __name__ == "__main__":
    main()
