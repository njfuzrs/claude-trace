#!/usr/bin/env python3
"""
rebuild_trajs.py — 用当前版本的 builder 重建历史 .traj 文件

历史轨迹的原始数据（raw.jsonl / events.jsonl）是完整的，坏的只是构建环节：
orphan tool_result、files_edited 恒空、history 缺 system prompt、
tool_result 重复膨胀、新模型成本为 0 等，全部可以通过重跑 builder 修复。

同时识别并清理 count_tokens 探测请求产生的垃圾会话目录
（proxy.py 的 _is_messages_request 曾把 /v1/messages/count_tokens 也当成
真实会话采集，实测污染了约 21% 的目录）。

用法：
    # 先看一遍会做什么（不写任何文件）
    python rebuild_trajs.py --dir trajectories/sessions --dry-run

    # 重建所有会话的 session.traj（原文件备份为 session.traj.bak）
    python rebuild_trajs.py --dir trajectories/sessions

    # 重建 + 把 count_tokens 垃圾目录移到 _trash/
    python rebuild_trajs.py --dir trajectories/sessions --quarantine-garbage

    # 只统计垃圾目录，不重建
    python rebuild_trajs.py --dir trajectories/sessions --scan-only
"""

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from builder import (  # noqa: E402
    SessionMetadata,
    apply_hook_events_to_metadata,
    build_trajectory,
    save_trajectory,
)
from merger import _adapt_raw_pair, load_raw_pairs_from_jsonl  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("rebuild")

# 垃圾会话判定：count_tokens 探测请求的特征
_GARBAGE_MARKERS = (
    "count_tokens",
    "Invalid URL (POST /v1/messages/count_tokens)",
)


def load_hook_events(events_path: Path) -> List[Dict]:
    """读取 events.jsonl（容忍坏行）"""
    if not events_path.exists():
        return []
    events = []
    try:
        for line in events_path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as e:
        logger.debug("读取 events 失败 %s: %s", events_path, e)
    return events


def _traj_has_steps(session_dir: Path) -> Optional[bool]:
    """会话的 session.traj 是否含有 TAO 步骤

    返回 None 表示无法判定（文件缺失或解析失败）。
    大文件不做完整解析，只在头尾各读一段找 trajectory 的首个元素。
    """
    traj_path = session_dir / "session.traj"
    if not traj_path.exists():
        return None
    size = traj_path.stat().st_size
    try:
        if size <= 400 * 1024:
            data = json.loads(traj_path.read_text(errors="replace"))
            if not isinstance(data, dict):
                return None
            return bool(data.get("trajectory"))
        # 大文件：trajectory 是首个 key，读头部足够判断它是否为空数组
        with traj_path.open(errors="replace") as f:
            head = f.read(4096)
        if '"trajectory"' not in head:
            return None
        tail = head.split('"trajectory"', 1)[1].lstrip()
        if tail.startswith(":"):
            tail = tail[1:].lstrip()
        return not tail.startswith("[]")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def classify_session(session_dir: Path) -> Tuple[str, str]:
    """判定会话类型，返回 (kind, reason)

    kind ∈ {"garbage", "no_raw", "ok"}
    """
    raw_path = session_dir / "raw.jsonl"
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        return "no_raw", "缺少 raw.jsonl"

    try:
        with raw_path.open(errors="replace") as f:
            first_line = f.readline()
    except OSError as e:
        return "no_raw", f"raw.jsonl 读取失败: {e}"

    # count_tokens 垃圾：首条记录是 count_tokens 探测
    if any(m in first_line for m in _GARBAGE_MARKERS):
        # 关键：首个请求恰好是 count_tokens 探测、但后续有真实请求的会话
        # 是有效数据，不能只看首行就判垃圾。实测按首行判定会误隔离
        # 80/1794（4.5%）个含完整轨迹的会话。有 TAO 步骤就一律放行。
        if _traj_has_steps(session_dir):
            return "ok", ""
        # 轨迹为空时，再看后续记录里是否存在「非 count_tokens」的真实请求。
        # 单纯多行 count_tokens 错误（探测被重试几次）仍是垃圾。
        try:
            with raw_path.open(errors="replace") as f:
                for i, line in enumerate(f):
                    if i == 0 or not line.strip():
                        continue
                    if i > 50:          # 只看前 50 行，够判定且不拖慢扫描
                        break
                    if not any(m in line for m in _GARBAGE_MARKERS):
                        return "ok", ""
        except OSError:
            pass
        try:
            rec = json.loads(first_line)
        except json.JSONDecodeError:
            return "garbage", "count_tokens 标记 + 首行非法 JSON"
        resp = rec.get("response") or {}
        if isinstance(resp, dict) and "error" in resp:
            return "garbage", "count_tokens 探测请求（上游报错）"
        return "garbage", "count_tokens 探测请求"

    return "ok", ""


def _old_traj_api_calls(traj: Dict) -> int:
    """旧 traj 记录的 API 调用数（含已合并的子会话）"""
    return int(((traj.get("info") or {}).get("model_stats") or {}).get("api_calls", 0) or 0)


def rebuild_one(session_dir: Path, dry_run: bool = False,
                backup: bool = True, force: bool = False) -> Optional[Dict]:
    """重建单个会话的 session.traj，返回旧/新统计对比

    重要限制：sub-agent 子会话的 pair 只存在于代理进程内存中，导出时被合并进
    session.traj，但从未单独落盘到 raw.jsonl。因此对含子会话的会话做重建会
    丢失这部分数据（实测一个会话 68 个 API 调用里有 25 个来自子会话）。
    这类会话默认跳过；--force 可强制重建（会丢子会话数据）。
    """
    session_id = session_dir.name
    raw_path = session_dir / "raw.jsonl"

    try:
        raw_pairs = load_raw_pairs_from_jsonl(raw_path)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("%s raw.jsonl 解析失败: %s", session_id[:8], e)
        return None
    if not raw_pairs:
        return None

    hook_events = load_hook_events(session_dir / "events.jsonl")

    # 读旧 traj 的统计做对比（读不出来不影响重建）
    old_stats = {}
    traj_path = session_dir / "session.traj"
    if traj_path.exists():
        try:
            old = json.loads(traj_path.read_text())
            old_meta = old.get("metadata") or {}
            old_traj = old.get("trajectory") or []
            old_stats = {
                "steps": len(old_traj),
                "history": len(old.get("history") or []),
                "orphan": sum(1 for s in old_traj if s.get("_orphan")),
                "files_edited": len(old_meta.get("files_edited") or []),
                "cost": old_meta.get("total_cost_usd", 0.0),
                "has_system": any(
                    h.get("role") == "system" for h in (old.get("history") or [])
                ),
                "api_calls": _old_traj_api_calls(old),
                "child_sessions": len(old_meta.get("child_sessions") or []),
            }
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    # 子会话数据不在 raw.jsonl 中，重建会丢失 → 默认跳过
    old_calls = old_stats.get("api_calls", 0)
    if not force and old_calls > len(raw_pairs):
        return {
            "session_id": session_id,
            "skipped": "subagent_data_only_in_traj",
            "old": old_stats,
            "new": {},
            "lost_pairs": old_calls - len(raw_pairs),
        }

    # 构建 metadata：原 traj 的 metadata 里可能有 hooks 之外的信息（如 model），
    # 优先沿用；hook events 再做一轮补全。
    model = raw_pairs[0].model if raw_pairs else ""
    if not model and old_stats:
        try:
            model = (json.loads(traj_path.read_text()).get("metadata") or {}).get("model", "")
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    metadata = SessionMetadata(
        session_id=session_id,
        start_time=raw_pairs[0].timestamp if raw_pairs else "",
        model=model,
    )
    apply_hook_events_to_metadata(metadata, hook_events)

    adapted = [_adapt_raw_pair(p, i + 1) for i, p in enumerate(raw_pairs)]
    traj = build_trajectory(session_id, adapted, metadata)

    new_meta = traj["metadata"]
    dq = new_meta.get("data_quality") or {}
    new_stats = {
        "steps": new_meta["total_steps"],
        "history": len(traj["history"]),
        "orphan": dq.get("orphan_observations", 0),
        "files_edited": len(new_meta.get("files_edited") or []),
        "cost": new_meta.get("total_cost_usd", 0.0),
        "has_system": dq.get("has_system_prompt", False),
    }

    if not dry_run:
        if backup and traj_path.exists():
            bak = traj_path.with_suffix(".traj.bak")
            if not bak.exists():          # 已备份过就不覆盖，保住最原始版本
                shutil.copy2(traj_path, bak)
        save_trajectory(traj_path, traj)

    return {"session_id": session_id, "old": old_stats, "new": new_stats}


def main():
    parser = argparse.ArgumentParser(
        description="用当前 builder 重建历史 .traj 文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dir", required=True, help="sessions 目录（每个子目录一个会话）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    parser.add_argument("--scan-only", action="store_true", help="只做垃圾目录扫描统计")
    parser.add_argument("--quarantine-garbage", action="store_true",
                        help="把 count_tokens 垃圾目录移动到 _trash/")
    parser.add_argument("--no-backup", action="store_true",
                        help="不生成 session.traj.bak 备份")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个会话（0=全部）")
    parser.add_argument("--force", action="store_true",
                        help="强制重建含 sub-agent 子会话的会话（会丢失子会话数据，不建议）")
    args = parser.parse_args()

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        logger.error("目录不存在: %s", root)
        return 1

    session_dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name != "_trash")
    if args.limit:
        session_dirs = session_dirs[: args.limit]
    logger.info("发现 %d 个会话目录", len(session_dirs))

    counts = {"garbage": 0, "no_raw": 0, "ok": 0, "rebuilt": 0, "failed": 0}
    garbage_dirs: List[Path] = []
    improvements = {
        "orphan_fixed": 0, "files_edited_gained": 0,
        "system_recovered": 0, "cost_recovered": 0, "history_shrunk": 0,
    }

    for d in session_dirs:
        kind, reason = classify_session(d)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "garbage":
            garbage_dirs.append(d)
            continue
        if kind == "no_raw":
            continue
        if args.scan_only:
            continue

        result = rebuild_one(d, dry_run=args.dry_run, backup=not args.no_backup,
                             force=args.force)
        if result is None:
            counts["failed"] += 1
            continue
        if result.get("skipped"):
            counts["skipped_subagent"] = counts.get("skipped_subagent", 0) + 1
            counts["skipped_lost_pairs"] = (
                counts.get("skipped_lost_pairs", 0) + result.get("lost_pairs", 0)
            )
            continue
        counts["rebuilt"] += 1

        old, new = result["old"], result["new"]
        if old:
            if old.get("orphan", 0) > new["orphan"]:
                improvements["orphan_fixed"] += old["orphan"] - new["orphan"]
            if new["files_edited"] > old.get("files_edited", 0):
                improvements["files_edited_gained"] += 1
            if new["has_system"] and not old.get("has_system"):
                improvements["system_recovered"] += 1
            if new["cost"] > old.get("cost", 0):
                improvements["cost_recovered"] += 1
            if new["history"] < old.get("history", 0):
                improvements["history_shrunk"] += 1

    # ── 垃圾目录处理 ──────────────────────────────────────
    if garbage_dirs:
        logger.info("count_tokens 垃圾目录: %d 个", len(garbage_dirs))
        if args.quarantine_garbage and not args.dry_run:
            trash = root / "_trash"
            trash.mkdir(exist_ok=True)
            moved = 0
            for d in garbage_dirs:
                target = trash / d.name
                if target.exists():
                    continue
                try:
                    shutil.move(str(d), str(target))
                    moved += 1
                except OSError as e:
                    logger.warning("移动失败 %s: %s", d.name, e)
            logger.info("已隔离 %d 个垃圾目录到 %s", moved, trash)
        elif args.quarantine_garbage:
            logger.info("[dry-run] 将隔离 %d 个垃圾目录到 %s",
                        len(garbage_dirs), root / "_trash")

    # ── 报告 ──────────────────────────────────────────────
    print("\n" + "=" * 56)
    print("扫描结果")
    print("=" * 56)
    print(f"  正常会话       : {counts['ok']}")
    print(f"  count_tokens垃圾: {counts['garbage']}")
    print(f"  缺少 raw.jsonl : {counts['no_raw']}")
    if not args.scan_only:
        print(f"  重建成功       : {counts['rebuilt']}")
        print(f"  重建失败       : {counts['failed']}")
        skipped = counts.get("skipped_subagent", 0)
        if skipped:
            print(f"  跳过(含子会话)  : {skipped}"
                  f"  — 重建会丢失 {counts.get('skipped_lost_pairs', 0)} 个子会话 pair")
            print("                    子会话数据只存在于旧 traj 中，raw.jsonl 未落盘；")
            print("                    这些会话保持原样，新采集的会话不受影响。")
        print("\n修复效果（对比重建前）")
        print(f"  修复的 orphan observation : {improvements['orphan_fixed']}")
        print(f"  files_edited 由空变有的会话: {improvements['files_edited_gained']}")
        print(f"  恢复 system prompt 的会话  : {improvements['system_recovered']}")
        print(f"  成本从 0 变为有值的会话    : {improvements['cost_recovered']}")
        print(f"  history 去重后变短的会话   : {improvements['history_shrunk']}")
    if args.dry_run:
        print("\n[dry-run] 未写入任何文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
