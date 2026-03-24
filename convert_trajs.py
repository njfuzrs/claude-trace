#!/usr/bin/env python3
"""
convert_trajs.py — 轨迹格式转换（.traj → SFT .jsonl）

支持三种输出格式（参考 SWE-smith collect_trajs.py）：
  xml      — tool_calls → <function=name><parameter=k>v</parameter></function>
  tool     — 原始 function calling 格式透传
  messages — 通用 messages 格式

用法：
    python convert_trajs.py --input filtered/ --output sft/ --style xml
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("convert")


def traj_to_messages_xml(traj: dict) -> List[Dict]:
    """将轨迹转换为 XML 格式的 messages（SWE-agent-LM 默认）"""
    messages: List[Dict] = []
    steps = traj.get("trajectory", [])

    for step in steps:
        msg_type = step.get("message_type")

        if msg_type == "action":
            thought = step.get("thought", "")
            tool_name = step.get("tool_name", "")
            tool_input = step.get("tool_input", {})

            if tool_name and tool_name != "final_answer":
                # 构建 XML 格式的工具调用
                params = "".join(
                    f"<parameter={k}>{json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}</parameter>"
                    for k, v in tool_input.items()
                    if not k.startswith("_")  # 跳过内部标记字段（如 _parse_error）
                )
                action_xml = f"<function={tool_name}>{params}</function>"
                content = f"{thought}\n\n{action_xml}" if thought else action_xml
            else:
                content = thought

            messages.append({"role": "assistant", "content": content})

        elif msg_type == "observation":
            obs = step.get("content", "")
            prefix = "ERROR: " if step.get("is_error") else ""
            messages.append({"role": "user", "content": f"OBSERVATION:\n{prefix}{obs}"})

    return messages


def traj_to_messages_tool(traj: dict) -> List[Dict]:
    """将轨迹转换为原始 function calling 格式"""
    messages: List[Dict] = []
    steps = traj.get("trajectory", [])

    for step in steps:
        msg_type = step.get("message_type")

        if msg_type == "action":
            tool_name = step.get("tool_name", "")
            tool_input = step.get("tool_input", {})
            thought = step.get("thought", "")

            if tool_name and tool_name != "final_answer":
                msg: Dict = {"role": "assistant", "content": thought or None}
                msg["tool_calls"] = [{
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_input, ensure_ascii=False),
                    },
                }]
                messages.append(msg)
            else:
                messages.append({"role": "assistant", "content": thought})

        elif msg_type == "observation":
            messages.append({
                "role": "tool",
                "content": step.get("content", ""),
                "tool_call_id": step.get("tool_use_id", ""),
            })

    return messages


def traj_to_messages_plain(traj: dict) -> List[Dict]:
    """将轨迹转换为通用 messages 格式"""
    messages: List[Dict] = []
    steps = traj.get("trajectory", [])

    for step in steps:
        msg_type = step.get("message_type")
        if msg_type == "action":
            messages.append({"role": "assistant", "content": step.get("content", "")})
        elif msg_type == "observation":
            messages.append({"role": "user", "content": step.get("content", "")})

    return messages


CONVERTERS = {
    "xml": traj_to_messages_xml,
    "tool": traj_to_messages_tool,
    "messages": traj_to_messages_plain,
}

DEFAULT_SYSTEM_MESSAGE = (
    "You are an interactive agent that helps users with software engineering tasks. "
    "Use the tools available to you to assist the user."
)


def convert_traj(traj: dict, style: str, system_message: str = DEFAULT_SYSTEM_MESSAGE) -> Dict:
    """将单个 .traj 转换为 SFT 训练格式"""
    converter = CONVERTERS[style]
    messages = converter(traj)

    # SFT 格式要求 system message 作为第一条（参考 SWE-smith collect_trajs.py）
    if system_message:
        messages.insert(0, {"role": "system", "content": system_message})

    meta = traj.get("metadata", {})
    return {
        "messages": messages,
        "instance_id": meta.get("session_id", ""),
        "model": meta.get("model", ""),
        "resolved": meta.get("exit_status") == "end_turn",
        "tools_used": meta.get("tools_used", []),
        "total_steps": meta.get("total_steps", 0),
    }


def main():
    parser = argparse.ArgumentParser(description="轨迹格式转换", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input", required=True, help="输入 .traj 目录")
    parser.add_argument("--output", required=True, help="输出 .jsonl 目录")
    parser.add_argument("--style", choices=["xml", "tool", "messages"], default="xml", help="输出格式")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for traj_file in sorted(input_dir.glob("*.traj")):
        traj = json.loads(traj_file.read_text())
        record = convert_traj(traj, args.style)

        out_file = output_dir / f"{traj_file.stem}.jsonl"
        out_file.write_text(json.dumps(record, ensure_ascii=False) + "\n")
        count += 1

    logger.info("转换完成: %d 个文件 (style=%s)", count, args.style)


if __name__ == "__main__":
    main()
