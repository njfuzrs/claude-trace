#!/usr/bin/env python3
"""
thinking_coverage.py — 按上游 × 模型统计 thinking 覆盖率（只读）

问题：第三方网关会剥 thinking（2026-03 已证实），渠道又经常切。剥掉之后
session.traj 照样生成、照样上传，只是 has_thinking 全 false —— 没人会注意到。
本工具把「哪个上游、哪个模型的轨迹还有 CoT」打出来。

上游从哪来：raw.jsonl 不记上游地址，按响应 message id 的形状指纹归类：
  anthropic       msg_01 + 22 位 base58（官方 API 格式）
  bedrock         msg_bdrk_...
  gw-timestamp    msg_ + 14 位时间戳（如 msg_20261004100734，网关自造）
  gw-random       msg_ + 其他随机串（网关自造）
  unknown         首个成功响应没有 id
指纹只说明「id 是谁造的」，同一网关后面可能挂多家；要精确就按月份对照渠道切换记录。

口径：
  - 只看 raw.jsonl（原始数据），不信 session.traj（旧 builder 导出可能过期）。
  - 跳过 count_tokens 探测（首行无 max_tokens）和没有任何成功响应的会话。
  - 分母 eligible = 请求开了 thinking（adaptive / enabled）且有 tool_use 的会话。
    thinking=disabled / 未设置的会话本来就不该有 CoT，单独计数，不进分母。
  - covered  = 至少一个 thinking 块有正文，或有 redacted_thinking。
  - omitted  = 有 thinking 块但正文全空（display=omitted 或网关清空了正文）。
    这种块对 thinking-SFT 同样没用，所以不算 covered，单独列出来。

告警（说后果，不说现象）：某分组最近连续 N 个 eligible 会话全无 CoT →
「该上游的轨迹没有 CoT，不能进 thinking-SFT」。

用法：
    python3 tools/thinking_coverage.py --input ~/.claude-trace/trajectories/sessions
    python3 tools/thinking_coverage.py --input ... --by upstream,model,month
    python3 tools/thinking_coverage.py --input ... --since 2026-09-01 --json
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# 连续多少个 eligible 会话无 CoT 就告警（见改造文档 §6.4）
DEFAULT_STREAK = 20

GROUP_KEYS = ("upstream", "model", "month")

_RE_ANTHROPIC = re.compile(r"^msg_01[1-9A-HJ-NP-Za-km-z]{22}$")
_RE_TIMESTAMP = re.compile(r"^msg_\d{14}$")


def classify_upstream(message_id: str) -> str:
    """按响应 message id 形状推断上游指纹"""
    if not message_id:
        return "unknown"
    if message_id.startswith("msg_bdrk_"):
        return "bedrock"
    if _RE_ANTHROPIC.match(message_id):
        return "anthropic"
    if _RE_TIMESTAMP.match(message_id):
        return "gw-timestamp"
    return "gw-random"


def thinking_requested(request: Dict) -> bool:
    """请求体是否开了 thinking（adaptive / enabled 视为开）"""
    cfg = request.get("thinking") if isinstance(request, dict) else None
    return isinstance(cfg, dict) and cfg.get("type") in ("adaptive", "enabled")


def _iter_records(raw_path: Path) -> Iterable[Dict]:
    """逐行读 raw.jsonl，坏行跳过（截断的最后一行很常见）"""
    try:
        with raw_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except (OSError, UnicodeDecodeError):
        return


def scan_session(raw_path: Path) -> Optional[Dict]:
    """扫一个会话；探测请求或无成功响应时返回 None"""
    row = None
    for rec in _iter_records(raw_path):
        req = rec.get("request") or {}
        if row is None:
            # 首行保存完整 request_body；无 max_tokens 是 count_tokens 探测
            if not isinstance(req, dict) or "max_tokens" not in req:
                return None
            row = {
                "session": raw_path.parent.name,
                "timestamp": rec.get("timestamp") or "",
                "model": rec.get("model") or req.get("model") or "unknown",
                "upstream": None,
                "requested": False,
                "has_tool_use": False,
                "thinking_text": False,
                "thinking_empty": False,
                "responses": 0,
            }
        # 后续行的 request 也带 thinking 配置（增量行只省掉 messages）
        if thinking_requested(req):
            row["requested"] = True
        resp = rec.get("response")
        if not isinstance(resp, dict) or "error" in resp:
            continue
        content = resp.get("content")
        if not isinstance(content, list):
            continue
        row["responses"] += 1
        if row["upstream"] is None and resp.get("id"):
            row["upstream"] = classify_upstream(resp["id"])
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("tool_use", "server_tool_use"):
                row["has_tool_use"] = True
            elif btype == "redacted_thinking":
                row["thinking_text"] = True
            elif btype == "thinking":
                if (block.get("thinking") or "").strip():
                    row["thinking_text"] = True
                else:
                    row["thinking_empty"] = True
    if row is None or row["responses"] == 0:
        return None
    if row["upstream"] is None:
        row["upstream"] = "unknown"
    row["month"] = row["timestamp"][:7] or "unknown"
    return row


def collect(sessions_dir: Path, since: str = "") -> List[Dict]:
    rows = []
    for d in sorted(sessions_dir.iterdir()):
        raw = d / "raw.jsonl"
        if not raw.is_file():
            continue
        row = scan_session(raw)
        if row is None:
            continue
        if since and row["timestamp"][:10] < since:
            continue
        rows.append(row)
    return rows


def _status(row: Dict) -> str:
    """单个会话归入哪一类"""
    if not row["requested"]:
        return "not_requested"
    if not row["has_tool_use"]:
        return "no_tool_use"
    if row["thinking_text"]:
        return "covered"
    if row["thinking_empty"]:
        return "omitted"
    return "missing"


def summarize(rows: List[Dict], by: List[str], streak: int = DEFAULT_STREAK) -> List[Dict]:
    """按 by 分组汇总；每组带尾部连续无 CoT 计数"""
    groups: Dict[tuple, List[Dict]] = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in by)].append(r)
    out = []
    for key, items in groups.items():
        items.sort(key=lambda r: r["timestamp"])
        counts = {s: 0 for s in ("covered", "omitted", "missing", "not_requested", "no_tool_use")}
        for r in items:
            counts[_status(r)] += 1
        eligible = counts["covered"] + counts["omitted"] + counts["missing"]
        # 尾部连续无 CoT：从最新往回数 eligible 会话，遇到 covered 停
        tail = 0
        for r in reversed(items):
            s = _status(r)
            if s == "covered":
                break
            if s in ("omitted", "missing"):
                tail += 1
        out.append({
            "group": dict(zip(by, key, strict=True)),
            "sessions": len(items),
            "eligible": eligible,
            **counts,
            "coverage": counts["covered"] / eligible if eligible else None,
            "tail_no_cot": tail,
            "alert": tail >= streak,
            "last_seen": items[-1]["timestamp"],
        })
    out.sort(key=lambda g: tuple(str(g["group"][k]) for k in by))
    return out


def _pct(x: Optional[float]) -> str:
    return "   -  " if x is None else f"{x * 100:5.1f}%"


def print_report(groups: List[Dict], by: List[str], streak: int):
    widths = {k: max([len(k)] + [len(str(g["group"][k])) for g in groups]) for k in by}
    head = "  ".join(k.ljust(widths[k]) for k in by)
    print(f"{head}  会话  合格  有CoT  覆盖率  空正文  缺失  未开thinking  尾部连续无CoT")
    for g in groups:
        cols = "  ".join(str(g["group"][k]).ljust(widths[k]) for k in by)
        print(f"{cols}  {g['sessions']:4d}  {g['eligible']:4d}  {g['covered']:5d}  "
              f"{_pct(g['coverage'])}  {g['omitted']:6d}  {g['missing']:4d}  "
              f"{g['not_requested']:12d}  {g['tail_no_cot']:4d}")
    print()
    print("口径：合格 = 请求开了 thinking 且有 tool_use；空正文 = 有 thinking 块但正文为空，不算 CoT。")
    alerts = [g for g in groups if g["alert"]]
    if not alerts:
        print(f"未发现连续 {streak} 个合格会话无 CoT 的分组。")
        return
    for g in alerts:
        label = " / ".join(f"{k}={g['group'][k]}" for k in by)
        print(f"⚠️  {label}：最近 {g['tail_no_cot']} 个合格会话没有 CoT，"
              f"该上游的轨迹不能进 thinking-SFT（最后一条 {g['last_seen'][:19]}）")


def main():
    parser = argparse.ArgumentParser(description="按上游 × 模型统计 thinking 覆盖率（只读）")
    parser.add_argument("--input", required=True, help="sessions 目录")
    parser.add_argument("--by", default="upstream,model",
                        help=f"分组维度，逗号分隔，可选 {','.join(GROUP_KEYS)}（默认 upstream,model）")
    parser.add_argument("--since", default="", help="只统计该日期（YYYY-MM-DD）之后的会话")
    parser.add_argument("--streak", type=int, default=DEFAULT_STREAK,
                        help=f"尾部连续多少个合格会话无 CoT 即告警（默认 {DEFAULT_STREAK}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    by = [k.strip() for k in args.by.split(",") if k.strip()]
    bad = [k for k in by if k not in GROUP_KEYS]
    if bad or not by:
        parser.error(f"--by 只支持 {','.join(GROUP_KEYS)}，收到 {args.by}")

    sessions_dir = Path(args.input).expanduser()
    if not sessions_dir.is_dir():
        print(f"目录不存在: {sessions_dir}", file=sys.stderr)
        sys.exit(2)

    groups = summarize(collect(sessions_dir, args.since), by, args.streak)
    if args.json:
        print(json.dumps(groups, ensure_ascii=False, indent=2))
    else:
        print_report(groups, by, args.streak)
    # 有告警时退出码 1，方便挂 cron
    sys.exit(1 if any(g["alert"] for g in groups) else 0)


if __name__ == "__main__":
    main()
