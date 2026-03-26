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
    has_tool_use = any(
        s.get("tool_name") and s["tool_name"] != "final_answer"
        for s in steps if s.get("message_type") == "action"
    )

    if step_count < args.min_steps:
        return False, f"步骤数不足: {step_count} < {args.min_steps}"

    if args.max_steps and step_count > args.max_steps:
        return False, f"步骤数过多: {step_count} > {args.max_steps}"

    if args.require_end_turn and exit_status != "end_turn":
        return False, f"非正常结束: {exit_status}"

    if args.require_tool_use and not has_tool_use:
        return False, "无工具调用"

    return True, ""


def compute_quality_metrics(traj: dict) -> dict:
    """P2 #20: 计算单条轨迹的质量指标"""
    steps = traj.get("trajectory", [])
    info = traj.get("info", {})
    meta = traj.get("metadata", {})

    actions = [s for s in steps if s.get("message_type") == "action"]
    observations = [s for s in steps if s.get("message_type") == "observation"]
    tool_actions = [s for s in actions if s.get("tool_name") and s["tool_name"] != "final_answer"]

    # Observation 覆盖率：有 observation 的 tool_use action 占比
    tool_use_ids_with_obs = {s.get("tool_use_id") for s in observations if s.get("tool_use_id")}
    tool_use_ids_total = {s.get("tool_use_id") for s in tool_actions if s.get("tool_use_id")}
    obs_coverage = len(tool_use_ids_with_obs) / len(tool_use_ids_total) if tool_use_ids_total else 1.0

    # SSE 完整率
    model_stats = info.get("model_stats", {})
    api_calls = model_stats.get("api_calls", meta.get("total_api_calls", 0))

    # partial 步骤数：通过 history 中 assistant 消息的 stop_reason 判断
    # stop_reason 为空字符串不代表 partial（可能只是未记录），
    # 需要检查 _complete 标记或 stop_reason 是否明确缺失
    history = traj.get("history", [])
    partial_count = sum(
        1 for h in history
        if h.get("role") == "assistant"
        and h.get("stop_reason") is not None  # 字段存在
        and h.get("stop_reason") == ""         # 但值为空（SSE 流中断）
        and not h.get("tool_calls")            # 排除正常的 tool_use 响应
    )

    sse_complete_rate = 1.0 - (partial_count / api_calls) if api_calls else 1.0

    return {
        "step_count": len(steps),
        "action_count": len(actions),
        "observation_count": len(observations),
        "tool_action_count": len(tool_actions),
        "observation_coverage": round(obs_coverage, 3),
        "sse_complete_rate": round(sse_complete_rate, 3),
        "api_calls": api_calls,
        "tokens_sent": model_stats.get("tokens_sent", 0),
        "tokens_received": model_stats.get("tokens_received", 0),
        "has_thinking": info.get("has_thinking", False),
        "exit_status": info.get("exit_status", ""),
    }


def main():
    parser = argparse.ArgumentParser(description="过滤无效轨迹", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input", required=True, help="输入 .traj 目录")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--min-steps", type=int, default=3, help="最少步骤数")
    parser.add_argument("--max-steps", type=int, default=0, help="最多步骤数（0=不限）")
    parser.add_argument("--require-end-turn", action="store_true", help="必须正常结束")
    parser.add_argument("--require-tool-use", action="store_true", help="必须有工具调用")
    parser.add_argument("--report", action="store_true", help="P2 #20: 输出质量指标报告")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    total = passed = 0
    all_metrics = []
    # 新布局：sessions/*/session.traj；旧布局：*.traj
    traj_files = sorted(input_dir.glob("*/session.traj")) or sorted(input_dir.glob("*.traj"))
    for traj_file in traj_files:
        total += 1
        traj = load_traj(traj_file)
        ok, reason = passes_filter(traj, args)
        if ok:
            passed += 1
            shutil.copy2(traj_file, output_dir / traj_file.name)
        else:
            logger.info("过滤: %s — %s", traj_file.parent.name if traj_file.name == "session.traj" else traj_file.stem[:12], reason)

        if args.report:
            metrics = compute_quality_metrics(traj)
            metrics["session_id"] = traj_file.parent.name if traj_file.name == "session.traj" else traj_file.stem
            metrics["passed_filter"] = ok
            all_metrics.append(metrics)

    logger.info("过滤完成: %d/%d 通过 (%.0f%%)", passed, total, (passed / total * 100) if total else 0)

    # P2 #20: 输出质量指标报告
    if args.report and all_metrics:
        report_path = output_dir / "_quality_report.jsonl"
        with open(report_path, "w") as f:
            for m in all_metrics:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

        # 汇总统计
        avg_obs_cov = sum(m["observation_coverage"] for m in all_metrics) / len(all_metrics)
        avg_sse_rate = sum(m["sse_complete_rate"] for m in all_metrics) / len(all_metrics)
        avg_steps = sum(m["step_count"] for m in all_metrics) / len(all_metrics)
        total_tokens = sum(m["tokens_sent"] + m["tokens_received"] for m in all_metrics)

        logger.info("─── 质量报告 ───")
        logger.info("  轨迹总数:        %d", len(all_metrics))
        logger.info("  通过过滤:        %d (%.0f%%)", passed, (passed / total * 100) if total else 0)
        logger.info("  平均步骤数:      %.1f", avg_steps)
        logger.info("  Observation覆盖: %.1f%%", avg_obs_cov * 100)
        logger.info("  SSE完整率:       %.1f%%", avg_sse_rate * 100)
        logger.info("  总 token 用量:   %d", total_tokens)
        logger.info("  报告已保存:      %s", report_path)


if __name__ == "__main__":
    main()
