#!/usr/bin/env python3
"""
merger.py — 双通道数据合并器

将 HTTP 代理采集的原始数据（raw/）与 Hooks 采集的事件数据（events/）
通过 session_id 关联合并，输出增强版 .traj 文件。

用法：
    python merger.py --session-id <sid> --raw-dir ./trajectories/raw --events-dir ~/.claude/trajectory_events --output ./trajectories/traj
    python merger.py --all --raw-dir ./trajectories/raw --events-dir ~/.claude/trajectory_events --output ./trajectories/traj
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from builder import SessionMetadata, build_trajectory, save_trajectory  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("merger")


# ─────────────────────────────────────────────
# 原始数据加载
# ─────────────────────────────────────────────

@dataclass
class RawPair:
    """从 raw JSONL 或 raw/{session_id}/ 目录加载的请求/响应对"""
    timestamp: str
    request_body: Dict
    response_body: Optional[Dict]
    usage: Dict
    stop_reason: str
    is_partial: bool
    model: str


def load_raw_pairs_from_dir(session_dir: Path) -> List[RawPair]:
    """从 raw/{session_id}/ 目录加载（每对一个 JSON 文件）"""
    pairs = []
    req_files = sorted(session_dir.glob("*_request.json"))
    for req_file in req_files:
        idx = req_file.stem.split("_")[0]
        resp_file = session_dir / f"{idx}_response.json"

        req_data = json.loads(req_file.read_text())
        resp_data = json.loads(resp_file.read_text()) if resp_file.exists() else {}

        pairs.append(RawPair(
            timestamp=req_data.get("timestamp", ""),
            request_body=req_data.get("body", req_data.get("request", {})),
            response_body=resp_data.get("body", resp_data.get("response")),
            usage=resp_data.get("usage", {}),
            stop_reason=resp_data.get("stop_reason", ""),
            is_partial=resp_data.get("is_partial", False),
            model=req_data.get("model", ""),
        ))
    return pairs


def load_raw_pairs_from_jsonl(jsonl_path: Path) -> List[RawPair]:
    """从 raw/{session_id}.jsonl 加载（一行一对）"""
    pairs = []
    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        pairs.append(RawPair(
            timestamp=record.get("timestamp", ""),
            request_body=record.get("request", {}),
            response_body=record.get("response"),
            usage=record.get("usage", {}),
            stop_reason=record.get("stop_reason", ""),
            is_partial=record.get("is_partial", False),
            model=record.get("model", ""),
        ))
    return pairs


def load_raw_pairs(raw_dir: Path, session_id: str) -> List[RawPair]:
    """自动检测并加载原始数据（目录或 JSONL）"""
    session_dir = raw_dir / session_id
    if session_dir.is_dir():
        return load_raw_pairs_from_dir(session_dir)

    jsonl_path = raw_dir / f"{session_id}.jsonl"
    if jsonl_path.exists():
        return load_raw_pairs_from_jsonl(jsonl_path)

    return []


# ─────────────────────────────────────────────
# Hook 事件加载
# ─────────────────────────────────────────────

def load_hook_events(events_dir: Path, session_id: str) -> List[Dict]:
    """加载 Hooks 采集的事件流"""
    events_file = events_dir / f"{session_id}.jsonl"
    if not events_file.exists():
        return []
    events = []
    for line in events_file.read_text().splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


# ─────────────────────────────────────────────
# 数据合并
# ─────────────────────────────────────────────

class DataMerger:
    def __init__(self, raw_dir: Path, events_dir: Path, output_dir: Path):
        self.raw_dir = raw_dir
        self.events_dir = events_dir
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def merge(self, session_id: str) -> Optional[Dict]:
        """合并双通道数据，构建增强版轨迹"""

        # 1. 加载代理原始数据
        raw_pairs = load_raw_pairs(self.raw_dir, session_id)
        if not raw_pairs:
            logger.warning("session %s 无原始数据，跳过", session_id[:8])
            return None

        # 2. 加载 Hook 事件（可选）
        hook_events = load_hook_events(self.events_dir, session_id)

        # 3. 从 Hook 事件提取元数据
        session_start = next((e for e in hook_events if e.get("event") == "SessionStart"), {})
        session_end = next((e for e in hook_events if e.get("event") == "SessionEnd"), {})
        compactions = [e for e in hook_events if e.get("event") == "PostCompact"]
        subagents = [e for e in hook_events if e.get("event") in ("SubagentStart", "SubagentStop")]
        user_prompts = [e["prompt"] for e in hook_events if e.get("event") == "UserPromptSubmit" and "prompt" in e]

        # 4. 构建 SessionMetadata
        model = session_start.get("model") or (raw_pairs[0].model if raw_pairs else "")
        metadata = SessionMetadata(
            session_id=session_id,
            start_time=raw_pairs[0].timestamp if raw_pairs else "",
            model=model,
            working_directory=session_start.get("cwd", ""),
            start_source=session_start.get("source", ""),
            end_source=session_end.get("source", ""),
            user_prompts=user_prompts,
            compactions=compactions,
            subagent_spans=subagents,
            has_sub_agent=len(subagents) > 0,
        )

        # 5. 将 RawPair 适配为 builder 期望的格式
        adapted_pairs = [_adapt_raw_pair(p) for p in raw_pairs]

        # 6. 构建轨迹
        traj = build_trajectory(session_id, adapted_pairs, metadata)

        # 7. 保存 .traj 文件
        traj_path = self.output_dir / f"{session_id}.traj"
        save_trajectory(traj_path, traj)

        logger.info(
            "合并完成: %s | 步骤=%d | API调用=%d | tokens=%d+%d | Hook事件=%d",
            session_id[:8],
            traj["metadata"]["total_steps"],
            traj["metadata"]["total_api_calls"],
            traj["metadata"]["total_tokens_sent"],
            traj["metadata"]["total_tokens_received"],
            len(hook_events),
        )
        return traj

    def merge_all(self) -> List[str]:
        """合并 raw_dir 下所有会话"""
        session_ids = set()

        # 从目录结构发现会话
        if self.raw_dir.exists():
            for p in self.raw_dir.iterdir():
                if p.is_dir():
                    session_ids.add(p.name)
                elif p.suffix == ".jsonl":
                    session_ids.add(p.stem)

        # 也从 events_dir 发现会话（可能只有 Hook 数据）
        if self.events_dir.exists():
            for p in self.events_dir.glob("*.jsonl"):
                session_ids.add(p.stem)

        merged = []
        for sid in sorted(session_ids):
            result = self.merge(sid)
            if result:
                merged.append(sid)

        logger.info("共合并 %d 个会话", len(merged))
        return merged


@dataclass
class _AdaptedPair:
    """将 RawPair 适配为 builder.build_trajectory 期望的 pair 格式"""
    timestamp: str
    request_body: Dict
    response_body: Dict
    usage: Dict
    stop_reason: str
    is_partial: bool
    model: str


def _adapt_raw_pair(raw: RawPair) -> _AdaptedPair:
    return _AdaptedPair(
        timestamp=raw.timestamp,
        request_body=raw.request_body,
        response_body=raw.response_body or {},
        usage=raw.usage,
        stop_reason=raw.stop_reason,
        is_partial=raw.is_partial,
        model=raw.model,
    )


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="合并 HTTP 代理数据 + Hooks 事件数据，输出 .traj 文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--session-id", help="指定会话 ID（不指定则用 --all）")
    parser.add_argument("--all", action="store_true", help="合并所有会话")
    parser.add_argument("--raw-dir", default="./trajectories/raw", help="原始数据目录")
    parser.add_argument(
        "--events-dir",
        default=str(Path.home() / ".claude" / "trajectory_events"),
        help="Hooks 事件数据目录",
    )
    parser.add_argument("--output", default="./trajectories/traj", help=".traj 输出目录")
    args = parser.parse_args()

    merger = DataMerger(
        raw_dir=Path(args.raw_dir),
        events_dir=Path(args.events_dir),
        output_dir=Path(args.output),
    )

    if args.all:
        merged = merger.merge_all()
        print(f"合并完成，共 {len(merged)} 个会话")
    elif args.session_id:
        result = merger.merge(args.session_id)
        if result:
            traj_path = Path(args.output) / f"{args.session_id}.traj"
            print(f"✅ 轨迹已保存: {traj_path}")
        else:
            print("❌ 合并失败，请检查数据目录")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
