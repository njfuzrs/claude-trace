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
from xml.sax.saxutils import escape as xml_escape

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
                # P2 #12: 构建 XML 格式的工具调用，对参数值进行 XML 转义
                params = "".join(
                    f"<parameter={k}>{xml_escape(json.dumps(v, ensure_ascii=False)) if isinstance(v, (dict, list)) else xml_escape(str(v))}</parameter>"
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


def _extract_patch(traj: dict) -> str:
    """P1 fix: 从轨迹中提取 patch（文件编辑的 diff 内容）

    遍历 trajectory 中的 write/edit 工具调用，提取文件路径和内容变更。
    由于代理采集的是工具输入而非 git diff，这里拼接为伪 patch 格式，
    记录哪些文件被修改了以及修改内容，供下游 SFT 训练参考。
    """
    edits = []
    steps = traj.get("trajectory", [])
    for step in steps:
        if step.get("message_type") != "action":
            continue
        tool_name = step.get("tool_name", "")
        tool_input = step.get("tool_input", {})
        if not tool_name or not tool_input:
            continue

        if tool_name == "write":
            fp = tool_input.get("file_path") or tool_input.get("path", "")
            content = tool_input.get("content", "")
            if fp and content:
                edits.append(f"--- /dev/null\n+++ {fp}\n{content}")
        elif tool_name == "edit":
            fp = tool_input.get("file_path") or tool_input.get("path", "")
            old = tool_input.get("old_string", tool_input.get("old_text", ""))
            new = tool_input.get("new_string", tool_input.get("new_text", ""))
            if fp and (old or new):
                edits.append(f"--- {fp}\n+++ {fp}\n-{old}\n+{new}")
        elif tool_name == "bash":
            cmd = tool_input.get("command", "")
            # 检测 sed/patch 等常见文件修改命令
            if cmd and any(kw in cmd for kw in ("sed -i", "patch ", "tee ", "cat >", "echo >")):
                edits.append(f"# bash: {cmd}")

    return "\n".join(edits)


def convert_traj(traj: dict, style: str, system_message: str = DEFAULT_SYSTEM_MESSAGE) -> Dict:
    """将单个 .traj 转换为 SFT 训练格式"""
    converter = CONVERTERS[style]
    messages = converter(traj)

    # SFT 格式要求 system message 作为第一条（参考 SWE-smith collect_trajs.py）
    if system_message:
        messages.insert(0, {"role": "system", "content": system_message})

    meta = traj.get("metadata", {})
    patch = _extract_patch(traj)
    return {
        "messages": messages,
        "instance_id": meta.get("session_id", ""),
        "model": meta.get("model", ""),
        "resolved": meta.get("exit_status") == "end_turn",
        "tools_used": meta.get("tools_used", []),
        "total_steps": meta.get("total_steps", 0),
        "patch": patch,
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
    # 新布局：sessions/*/session.traj；旧布局：*.traj
    traj_files = sorted(input_dir.glob("*/session.traj")) or sorted(input_dir.glob("*.traj"))
    for traj_file in traj_files:
        traj = json.loads(traj_file.read_text())
        record = convert_traj(traj, args.style)

        sid = traj_file.parent.name if traj_file.name == "session.traj" else traj_file.stem
        out_file = output_dir / f"{sid}.jsonl"
        out_file.write_text(json.dumps(record, ensure_ascii=False) + "\n")
        count += 1

    logger.info("转换完成: %d 个文件 (style=%s)", count, args.style)


if __name__ == "__main__":
    main()
