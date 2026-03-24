#!/usr/bin/env python3
"""
filter_trajs.py — 过滤无效轨迹

用法：
    python filter_trajs.py --input traj/ --output filtered/ --min-steps 3 --require-end-turn
"""

import argparse
import json
import logging
import shutil
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("filter")


def load_traj(path: Path) -> dict:
    return json.loads(path.read_text())


def passes_filter(traj: dict, args) -> tuple[bool, str]:
    """检查轨迹是否通过过滤条件，返回 (通过, 原因)"""
    meta = traj.get("metadata", {})
    steps = traj.get("trajectory", [])
    info = traj.get("info", {})

    step_count = len(steps)
    exit_status = info.get("exit_status", meta.get("exit_status", ""))
    has_tool_use = any(s.get("tool_name") for s in steps if s.get("message_type") == "action")

    if step_count < args.min_steps:
        return False, f"步骤数不足: {step_count} < {args.min_steps}"

    if args.max_steps and step_count > args.max_steps:
        return False, f"步骤数过多: {step_count} > {args.max_steps}"

    if args.require_end_turn and exit_status != "end_turn":
        return False, f"非正常结束: {exit_status}"

    if args.require_tool_use and not has_tool_use:
        return False, "无工具调用"

    return True, ""


def main():
    parser = argparse.ArgumentParser(description="过滤无效轨迹", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input", required=True, help="输入 .traj 目录")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--min-steps", type=int, default=3, help="最少步骤数")
    parser.add_argument("--max-steps", type=int, default=0, help="最多步骤数（0=不限）")
    parser.add_argument("--require-end-turn", action="store_true", help="必须正常结束")
    parser.add_argument("--require-tool-use", action="store_true", help="必须有工具调用")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    total = passed = 0
    for traj_file in sorted(input_dir.glob("*.traj")):
        total += 1
        traj = load_traj(traj_file)
        ok, reason = passes_filter(traj, args)
        if ok:
            passed += 1
            shutil.copy2(traj_file, output_dir / traj_file.name)
        else:
            logger.info("过滤: %s — %s", traj_file.stem[:12], reason)

    logger.info("过滤完成: %d/%d 通过 (%.0f%%)", passed, total, (passed / total * 100) if total else 0)


if __name__ == "__main__":
    main()
