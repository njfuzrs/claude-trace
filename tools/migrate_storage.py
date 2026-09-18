#!/usr/bin/env python3
"""
migrate_storage.py — 本地存储迁移脚本

将 trajectories/raw/ 和 trajectories/traj/ 的数据按会话维度重组到 trajectories/sessions/。

迁移映射：
  raw/{sid}.jsonl           → sessions/{sid}/raw.jsonl
  raw/{sid}/                → sessions/{sid}/raw/
  traj/{sid}.traj           → sessions/{sid}/session.traj
  ~/.claude/trajectory_events/{sid}.jsonl → sessions/{sid}/events.jsonl (copy)

用法：
    python3 tools/migrate_storage.py                    # dry-run 预览
    python3 tools/migrate_storage.py --execute          # 执行迁移
    python3 tools/migrate_storage.py --execute --cleanup  # 执行迁移并清理空目录
"""

import argparse
import shutil
from pathlib import Path

TRAJ_ROOT = Path(__file__).resolve().parent.parent / "trajectories"
RAW_DIR = TRAJ_ROOT / "raw"
TRAJ_DIR = TRAJ_ROOT / "traj"
SESSIONS_DIR = TRAJ_ROOT / "sessions"
EVENTS_DIR = Path.home() / ".claude" / "trajectory_events"


def collect_session_ids() -> set:
    """从 raw/ 和 traj/ 收集所有 session_id"""
    ids = set()
    if RAW_DIR.exists():
        for p in RAW_DIR.iterdir():
            if p.is_dir():
                ids.add(p.name)
            elif p.suffix == ".jsonl":
                ids.add(p.stem)
    if TRAJ_DIR.exists():
        for p in TRAJ_DIR.glob("*.traj"):
            ids.add(p.stem)
    return ids


def migrate(execute: bool = False, cleanup: bool = False):
    session_ids = collect_session_ids()
    if not session_ids:
        print("没有找到需要迁移的数据")
        return

    print(f"发现 {len(session_ids)} 个会话")
    label = "迁移" if execute else "预览"

    moved_files = 0
    skipped = 0

    for sid in sorted(session_ids):
        dest = SESSIONS_DIR / sid
        actions = []

        # 1. raw/{sid}.jsonl → sessions/{sid}/raw.jsonl
        src_jsonl = RAW_DIR / f"{sid}.jsonl"
        dst_jsonl = dest / "raw.jsonl"
        if src_jsonl.exists() and not dst_jsonl.exists():
            actions.append(("move", src_jsonl, dst_jsonl))
        elif src_jsonl.exists():
            actions.append(("skip", src_jsonl, dst_jsonl))

        # 2. raw/{sid}/ → sessions/{sid}/raw/
        src_raw_dir = RAW_DIR / sid
        dst_raw_dir = dest / "raw"
        if src_raw_dir.is_dir() and not dst_raw_dir.exists():
            actions.append(("move_dir", src_raw_dir, dst_raw_dir))
        elif src_raw_dir.is_dir():
            actions.append(("skip", src_raw_dir, dst_raw_dir))

        # 3. traj/{sid}.traj → sessions/{sid}/session.traj
        src_traj = TRAJ_DIR / f"{sid}.traj"
        dst_traj = dest / "session.traj"
        if src_traj.exists() and not dst_traj.exists():
            actions.append(("move", src_traj, dst_traj))
        elif src_traj.exists():
            actions.append(("skip", src_traj, dst_traj))

        # 4. ~/.claude/trajectory_events/{sid}.jsonl → sessions/{sid}/events.jsonl (copy)
        src_events = EVENTS_DIR / f"{sid}.jsonl"
        dst_events = dest / "events.jsonl"
        if src_events.exists() and not dst_events.exists():
            actions.append(("copy", src_events, dst_events))
        elif src_events.exists():
            actions.append(("skip", src_events, dst_events))

        if not actions:
            continue

        for action, src, dst in actions:
            if action == "skip":
                skipped += 1
                continue

            print(f"  [{label}] {action}: {src.relative_to(src.parents[2] if 'claude' in str(src) else TRAJ_ROOT.parent)} → {dst.relative_to(TRAJ_ROOT)}")

            if execute:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if action == "move":
                    shutil.move(str(src), str(dst))
                elif action == "move_dir":
                    shutil.move(str(src), str(dst))
                elif action == "copy":
                    shutil.copy2(str(src), str(dst))

            moved_files += 1

    # 清理空目录
    if execute and cleanup:
        for old_dir in [RAW_DIR, TRAJ_DIR]:
            if not old_dir.exists():
                continue
            # 从深到浅删除空目录
            for d in sorted(old_dir.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            if old_dir.exists() and not any(old_dir.iterdir()):
                old_dir.rmdir()
                print(f"  已清理空目录: {old_dir.name}/")

    print(f"\n{label}完成: 处理 {moved_files} 个文件/目录, 跳过 {skipped} (已存在)")
    if not execute:
        print("添加 --execute 参数执行实际迁移")


def main():
    parser = argparse.ArgumentParser(description="本地存储迁移：raw/ + traj/ → sessions/")
    parser.add_argument("--execute", action="store_true", help="执行迁移（默认 dry-run）")
    parser.add_argument("--cleanup", action="store_true", help="迁移后清理空的旧目录")
    args = parser.parse_args()
    migrate(execute=args.execute, cleanup=args.cleanup)


if __name__ == "__main__":
    main()
