#!/usr/bin/env python3
"""
combine_trajs.py — 合并 + shuffle SFT 数据

用法：
    python3 tools/combine_trajs.py --input sft/ --output training_data.jsonl --shuffle --seed 42
"""

import argparse
import json
import logging
import random
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("combine")


def main():
    parser = argparse.ArgumentParser(description="合并 SFT 数据", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input", required=True, help="输入 .jsonl 目录")
    parser.add_argument("--output", required=True, help="输出合并后的 .jsonl 文件")
    parser.add_argument("--max-per-session", type=int, default=0, help="每个会话最多保留 N 条（0=不限）")
    parser.add_argument("--shuffle", action="store_true", help="随机打乱")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    args = parser.parse_args()

    input_dir = Path(args.input)
    records = []

    for jsonl_file in sorted(input_dir.glob("*.jsonl")):
        for line in jsonl_file.read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # 每个 session 最多 N 条
    if args.max_per_session > 0:
        from collections import defaultdict
        by_session: dict[str, list] = defaultdict(list)
        for r in records:
            by_session[r.get("instance_id", "")].append(r)
        records = []
        for recs in by_session.values():
            records.extend(recs[:args.max_per_session])

    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(records)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    logger.info("合并完成: %d 条记录 → %s", len(records), output_path)


if __name__ == "__main__":
    main()
