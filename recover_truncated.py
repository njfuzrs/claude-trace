#!/usr/bin/env python3
"""
recover_truncated.py — 修复被截断的历史轨迹并重新上云

针对的是「会话复活截断」留下的历史损坏（详见 builder.save_trajectory 的注释）：
代理按 --session-timeout 清理不活跃会话，用户隔几分钟再提问时新建了一个
pairs 为空的 Session，index 从 1 重来，而 save_trajectory 是覆盖写 ——
于是几小时的完整轨迹被只含最后几步的短轨迹冲掉，并且那份残缺版本还先
上传过一次，被服务端的 409 幂等锁死在云端。

raw.jsonl 是 append 写入的，历史完好，所以全部可以重建。

四个阶段（每个都可单独跑、可重复跑）：
  scan     只报告：哪些会话被截断、能恢复多少步
  rebuild  用 raw.jsonl 重建 session.traj（原文件备份为 .traj.bak）
  reupload 带 force=true 重新上传，覆盖云端的残缺版本
  purge    清理重试到死的残留 .gz（源文件仍在，可随时重压）

用法：
    python recover_truncated.py --dir ~/.claude-trace/trajectories/sessions scan
    python recover_truncated.py --dir ~/.claude-trace/trajectories/sessions rebuild
    python recover_truncated.py --dir ~/.claude-trace/trajectories/sessions reupload
    python recover_truncated.py --dir ~/.claude-trace/trajectories/sessions purge

    # 一条龙（推荐先跑 scan 看一眼）
    python recover_truncated.py --dir ... all
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from rebuild_trajs import rebuild_one  # noqa: E402
from uploader import UploadManager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("recover")


# ─────────────────────────────────────────────
# 截断检测
# ─────────────────────────────────────────────

def raw_index_stats(raw_path: Path) -> tuple:
    """返回 (记录数, index==1 出现次数, 最大 index)

    index 重复从 1 开始就是会话复活的指纹：正常会话的 index 单调递增。
    """
    n = ones = mx = 0
    try:
        with raw_path.open(errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    idx = json.loads(line).get("index")
                except json.JSONDecodeError:
                    continue
                n += 1
                if idx == 1:
                    ones += 1
                if isinstance(idx, int) and idx > mx:
                    mx = idx
    except OSError as e:
        logger.debug("读取 %s 失败: %s", raw_path, e)
    return n, ones, mx


def traj_steps(traj_path: Path) -> int:
    """traj 的步骤数，读不出来返回 -1"""
    if not traj_path.exists():
        return -1
    try:
        data = json.loads(traj_path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return -1
    if not isinstance(data, dict):
        return -1
    return len(data.get("trajectory") or [])


def find_truncated(sessions_dir: Path) -> List[Dict]:
    """扫出所有被复活截断的会话"""
    found = []
    for d in sorted(sessions_dir.iterdir()):
        if not d.is_dir():
            continue
        raw = d / "raw.jsonl"
        if not raw.exists():
            continue
        n, ones, mx = raw_index_stats(raw)
        if ones <= 1:
            continue
        found.append({
            "dir": d,
            "session_id": d.name,
            "raw_records": n,
            "incarnations": ones,
            "max_index": mx,
            "steps": traj_steps(d / "session.traj"),
            "uploaded": (d / ".uploaded").exists(),
            "raw_bytes": raw.stat().st_size,
        })
    return found


# ─────────────────────────────────────────────
# 阶段实现
# ─────────────────────────────────────────────

def do_scan(sessions_dir: Path) -> List[Dict]:
    """扫描并区分「已修复」与「待修复」

    注意：复活指纹（raw.jsonl 里 index 多次从 1 开始）是历史事实，修完也还在 ——
    它记录的是当时发生过截断，不是现在还坏着。所以判断修没修不能看指纹，
    要看 session.traj 是否已按完整 raw.jsonl 重建过（.traj.bak 是重建的凭据）。
    """
    items = find_truncated(sessions_dir)
    if not items:
        print("没有发现被复活截断过的会话 ✅")
        return items

    for i in items:
        i["repaired"] = (i["dir"] / "session.traj.bak").exists()

    pending = [i for i in items if not i["repaired"]]
    repaired = [i for i in items if i["repaired"]]

    print(f"\n有复活截断指纹的会话: {len(items)} 个（指纹是历史事实，修复后仍在）")
    print(f"  已重建（存在 .traj.bak）: {len(repaired)}")
    print(f"  待重建                  : {len(pending)}")
    print(f"  raw.jsonl 的 API 调用总数: {sum(i['raw_records'] for i in items)}")
    print(f"  当前 traj 的步骤总数     : {sum(max(0, i['steps']) for i in items)}")
    print(f"  已上云                   : {sum(1 for i in items if i['uploaded'])}")
    print(f"  合计磁盘                 : {sum(i['raw_bytes'] for i in items) / 1e9:.2f} GB")

    if not pending:
        print("\n✅ 全部已重建，无待修复项")
        return items

    worst = sorted(pending, key=lambda i: i["raw_records"] - max(0, i["steps"]), reverse=True)[:12]
    print("\n待修复中截断最严重的（会话id / 复活次数 / raw记录 / 现有步骤）:")
    for i in worst:
        print(f"  {i['session_id'][:8]}  复活 {i['incarnations']:3} 次  "
              f"raw={i['raw_records']:4}  steps={i['steps']:4}")
    return items


def do_rebuild(sessions_dir: Path, dry_run: bool = False,
               include_repaired: bool = False) -> List[str]:
    """重建截断会话的 traj，返回成功重建的 session_id 列表

    默认跳过已重建过的会话（存在 .traj.bak）：重复重建会用当前 traj 覆盖
    原始备份，把「修复前长什么样」这个凭据丢掉。--include-repaired 可强制重跑。
    """
    items = find_truncated(sessions_dir)
    if not include_repaired:
        skipped_repaired = [i for i in items if (i["dir"] / "session.traj.bak").exists()]
        items = [i for i in items if not (i["dir"] / "session.traj.bak").exists()]
        if skipped_repaired:
            print(f"跳过已重建的 {len(skipped_repaired)} 个会话"
                  f"（已有 .traj.bak；--include-repaired 可强制重跑）")
    if not items:
        print("没有需要重建的会话")
        return []

    rebuilt, skipped, failed = [], [], []
    gained = 0
    for i, item in enumerate(items, 1):
        sid = item["session_id"]
        before = item["steps"]
        try:
            # force=False：含子会话数据的会话会被 rebuild_one 主动跳过，
            # 因为子会话的 pair 从未落盘到 raw.jsonl，重建反而会丢数据。
            res = rebuild_one(item["dir"], dry_run=dry_run, backup=True, force=False)
        except Exception as e:
            failed.append((sid, str(e)))
            logger.warning("[%d/%d] %s 重建异常: %s", i, len(items), sid[:8], e)
            continue
        if res is None:
            failed.append((sid, "rebuild_one 返回 None"))
            continue
        if res.get("skipped"):
            skipped.append((sid, res["skipped"]))
            logger.info("[%d/%d] %s 跳过: %s", i, len(items), sid[:8], res["skipped"])
            continue
        after = traj_steps(item["dir"] / "session.traj") if not dry_run else before
        rebuilt.append(sid)
        if after > before:
            gained += after - before
        logger.info("[%d/%d] %s 重建完成: %d → %d 步",
                    i, len(items), sid[:8], before, after)

    tag = "[dry-run] " if dry_run else ""
    print(f"\n{tag}重建结果")
    print(f"  成功    : {len(rebuilt)}")
    print(f"  跳过    : {len(skipped)}（含子会话数据，重建会丢，需人工确认）")
    print(f"  失败    : {len(failed)}")
    print(f"  恢复步骤: +{gained}")
    for sid, why in skipped[:10]:
        print(f"    跳过 {sid[:8]}: {why}")
    for sid, why in failed[:10]:
        print(f"    失败 {sid[:8]}: {why}")
    return rebuilt


async def do_reupload(sessions_dir: Path, session_ids: Optional[List[str]] = None,
                      dry_run: bool = False) -> None:
    """带 force=true 重新上传，覆盖云端的残缺版本

    upload_session 内部已带 force=true（见 _upload_single_file），
    所以这里只要把 .uploaded 标记清掉、重新走一遍上传即可。
    """
    url = os.environ.get("TRAJ_PLATFORM_URL", "").strip()
    token = os.environ.get("TRAJ_UPLOAD_TOKEN", "").strip()
    if not url or not token:
        print("❌ 未配置 TRAJ_PLATFORM_URL / TRAJ_UPLOAD_TOKEN，无法上传")
        return

    if session_ids is None:
        session_ids = [i["session_id"] for i in find_truncated(sessions_dir)]
    if not session_ids:
        print("没有需要重传的会话")
        return

    print(f"待重传: {len(session_ids)} 个会话 → {url}")
    if dry_run:
        print("[dry-run] 未发起任何上传")
        return

    # cleanup_after_upload=False：恢复过程绝不删本地数据
    # backfill_enabled=False：这里显式指定要传哪些，不让它自己再扫一遍
    mgr = UploadManager(
        upload_url=url, upload_token=token,
        cleanup_after_upload=False,
        sessions_dir=sessions_dir, backfill_enabled=False,
    )
    await mgr.start()
    ok = failed = 0
    try:
        for n, sid in enumerate(session_ids, 1):
            d = sessions_dir / sid
            if not d.is_dir():
                continue
            # 清掉旧标记，强制重新走完整上传流程
            (d / ".uploaded").unlink(missing_ok=True)
            try:
                await mgr.upload_session(d, sid, tool_source="claude-code")
            except Exception as e:
                failed += 1
                logger.warning("[%d/%d] %s 重传异常: %s", n, len(session_ids), sid[:8], e)
                continue
            if (d / ".uploaded").exists():
                ok += 1
                logger.info("[%d/%d] %s 重传成功", n, len(session_ids), sid[:8])
            else:
                failed += 1
                logger.warning("[%d/%d] %s 重传未完成（已入队或过大）",
                               n, len(session_ids), sid[:8])
            await asyncio.sleep(0.5)
    finally:
        await mgr.stop()

    print(f"\n重传结果: 成功 {ok}，未完成 {failed}")
    print(f"  队列状态: {mgr.queue_stats()}")


def dedup_history(traj: Dict) -> tuple:
    """删除 history 里逐字节重复的条目，返回 (新 traj, 删除条数)

    只删「序列化后完全相同」的条目，所以是无损的：留下的是同一内容的第一份。
    trajectory（TAO 步骤）一律不动。

    这批数据坏在老版 builder：它把 user message 里的 tool_result 原样留在
    history，而 Claude Code 每轮都重发完整历史 —— 于是同一个 tool_result
    被写进 history 几百次。实测一个会话 42105 条 history 里只有 2362 条唯一，
    90.8% 的体积来自纯 tool_result 条目，traj 因此涨到 210MB 传不上云
    （网关上限 ~64MB）。当前 builder 的 _strip_tool_results 已修掉这个源头。
    """
    history = traj.get("history") or []
    if not history:
        return traj, 0
    seen = set()
    kept = []
    for msg in history:
        try:
            key = json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            kept.append(msg)
            continue
        h = hashlib.sha256(key.encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        kept.append(msg)
    removed = len(history) - len(kept)
    if removed:
        traj["history"] = kept
    return traj, removed


def _tool_use_ids(messages: List[Dict]) -> set:
    """收集一批 message 里出现的所有 tool_use_id（用于无损校验）"""
    out = set()
    for msg in messages or []:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("tool_use_id"):
                out.add(block["tool_use_id"])
    return out


def do_dedup(sessions_dir: Path, dry_run: bool = False,
             min_bytes: int = 64 * 1024 * 1024) -> List[str]:
    """对超过上限的 traj 做无损 history 去重，让它们能上云

    只处理「大到传不上去」的会话（默认 >64MB），因为去重要整份读进内存。
    每个会话都做无损校验：tool_use_id 集合与 trajectory 步数必须不变，
    任一不符就跳过并保留原文件。
    """
    fixed = []
    candidates = []
    for d in sorted(sessions_dir.iterdir()):
        if not d.is_dir():
            continue
        traj_path = d / "session.traj"
        try:
            if traj_path.exists() and traj_path.stat().st_size > min_bytes:
                candidates.append(d)
        except OSError:
            continue

    if not candidates:
        print(f"没有超过 {min_bytes / 1e6:.0f}MB 的 traj")
        return fixed

    print(f"发现 {len(candidates)} 个超大 traj（>{min_bytes / 1e6:.0f}MB）")
    for n, d in enumerate(candidates, 1):
        traj_path = d / "session.traj"
        before_bytes = traj_path.stat().st_size
        try:
            data = json.loads(traj_path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[%d/%d] %s 解析失败: %s", n, len(candidates), d.name[:8], e)
            continue
        if not isinstance(data, dict):
            continue

        old_hist = data.get("history") or []
        old_ids = _tool_use_ids(old_hist)
        old_steps = len(data.get("trajectory") or [])

        data, removed = dedup_history(data)
        if not removed:
            logger.info("[%d/%d] %s 无重复可删（%.0fMB）",
                        n, len(candidates), d.name[:8], before_bytes / 1e6)
            continue

        # 无损校验：tool_use_id 一个都不能少，trajectory 一步都不能变
        new_ids = _tool_use_ids(data.get("history") or [])
        new_steps = len(data.get("trajectory") or [])
        if new_ids != old_ids or new_steps != old_steps:
            logger.warning(
                "[%d/%d] %s 去重会丢数据，跳过（tool_use_id %d→%d，步骤 %d→%d）",
                n, len(candidates), d.name[:8],
                len(old_ids), len(new_ids), old_steps, new_steps,
            )
            continue

        payload = json.dumps(data, ensure_ascii=False, indent=2)
        after_bytes = len(payload.encode())
        logger.info(
            "[%d/%d] %s history %d→%d 条，%.0fMB→%.1fMB%s",
            n, len(candidates), d.name[:8], len(old_hist),
            len(data.get("history") or []),
            before_bytes / 1e6, after_bytes / 1e6,
            "  [dry-run]" if dry_run else "",
        )
        if dry_run:
            fixed.append(d.name)
            continue

        # 备份原文件后原子替换
        bak = traj_path.with_suffix(".traj.predup")
        if not bak.exists():
            traj_path.replace(bak)
        tmp = traj_path.with_suffix(".traj.tmp")
        tmp.write_text(payload)
        tmp.replace(traj_path)
        # 判据过期了，删掉 oversize 标记让它重新进补传
        (d / ".upload_oversize").unlink(missing_ok=True)
        fixed.append(d.name)

    tag = "[dry-run] " if dry_run else ""
    print(f"\n{tag}去重完成: {len(fixed)} 个会话")
    return fixed


def do_purge(sessions_dir: Path, queue_file: Optional[Path] = None,
             dry_run: bool = False) -> None:
    """清理重试到死的残留 .gz

    这些是 status=failed/oversize 的队列项留下的压缩文件。源文件都还在，
    真要重传随时能重新压缩，没有理由让它们永久占着盘。
    """
    queue_file = queue_file or (Path.home() / ".claude-trace" / ".upload_queue.jsonl")

    freed = 0
    removed = []
    dropped = 0
    # 1) 队列里终态项：释放 .gz，并丢掉已经没意义的条目
    if queue_file.exists():
        kept_lines = []
        for line in queue_file.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            gz = item.get("gz_path") or ""
            terminal = item.get("status") in ("failed", "oversize")
            if terminal and gz and Path(gz).exists():
                sz = Path(gz).stat().st_size
                if not dry_run:
                    Path(gz).unlink(missing_ok=True)
                    item["gz_path"] = ""
                freed += sz
                removed.append((gz, sz))

            # 会话已经拿到 .uploaded 标记 → 这条终态项是历史残留，留着只会
            # 让 --upload-status / /health 的积压数字长期不归零，误导排查。
            # 源文件仍在盘上，真要重传随时能重新入队。
            if terminal:
                session_dir = Path(item.get("filepath") or "").parent
                if (session_dir / ".uploaded").exists():
                    dropped += 1
                    continue
            kept_lines.append(json.dumps(item, ensure_ascii=False))
        if not dry_run:
            queue_file.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""))

    # 2) 会话目录里的孤儿 .gz（队列里已经没有对应条目了）
    for gz in sessions_dir.rglob("*.gz"):
        if any(gz.as_posix() == r[0] for r in removed):
            continue
        try:
            sz = gz.stat().st_size
        except OSError:
            continue
        if not dry_run:
            gz.unlink(missing_ok=True)
        freed += sz
        removed.append((gz.as_posix(), sz))

    tag = "[dry-run] " if dry_run else ""
    print(f"\n{tag}清理残留压缩文件: {len(removed)} 个，释放 {freed / 1e9:.2f} GB")
    for path, sz in sorted(removed, key=lambda r: -r[1])[:10]:
        print(f"    {sz / 1e6:8.1f}MB  {path}")
    if dropped:
        print(f"{tag}丢弃已上云会话的终态队列项: {dropped} 条"
              f"（源文件仍在盘上，可随时重新入队）")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "phase",
        choices=["scan", "rebuild", "dedup", "reupload", "purge", "all"],
    )
    parser.add_argument("--dir", required=True, help="sessions 目录")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不改动任何文件")
    parser.add_argument(
        "--include-repaired", action="store_true",
        help="连已重建过的会话（存在 .traj.bak）一起重跑。默认跳过它们，"
             "避免用当前 traj 覆盖掉「修复前」的原始备份。",
    )
    parser.add_argument(
        "--min-bytes", type=int, default=64 * 1024 * 1024,
        help="dedup 阶段只处理大于该体积的 traj（默认 64MB，即服务端上限）",
    )
    args = parser.parse_args()

    sessions_dir = Path(args.dir).expanduser()
    if not sessions_dir.is_dir():
        print(f"❌ 目录不存在: {sessions_dir}")
        return 1

    if args.phase == "scan":
        do_scan(sessions_dir)
    elif args.phase == "rebuild":
        do_rebuild(sessions_dir, dry_run=args.dry_run,
                   include_repaired=args.include_repaired)
    elif args.phase == "dedup":
        do_dedup(sessions_dir, dry_run=args.dry_run, min_bytes=args.min_bytes)
    elif args.phase == "reupload":
        asyncio.run(do_reupload(sessions_dir, dry_run=args.dry_run))
    elif args.phase == "purge":
        do_purge(sessions_dir, dry_run=args.dry_run)
    else:  # all
        items = do_scan(sessions_dir)
        rebuilt = []
        if items:
            rebuilt = do_rebuild(sessions_dir, dry_run=args.dry_run,
                                 include_repaired=args.include_repaired)
        # 超大 traj 去重后才可能传得上去，所以放在重传之前
        deduped = do_dedup(sessions_dir, dry_run=args.dry_run, min_bytes=args.min_bytes)
        targets = sorted(set(rebuilt) | set(deduped))
        if targets:
            asyncio.run(do_reupload(sessions_dir, targets, dry_run=args.dry_run))
        else:
            print("本轮没有新重建/去重的会话，跳过重传")
        do_purge(sessions_dir, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
