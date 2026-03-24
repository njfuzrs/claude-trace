#!/usr/bin/env python3
"""
setup_hooks.py — 自动配置 Claude Code settings.json 的 hooks

用法：
    python setup_hooks.py                    # 写入 ~/.claude/settings.json
    python setup_hooks.py --local            # 写入 .claude/settings.local.json（当前项目）
    python setup_hooks.py --show             # 只打印配置，不写入
    python setup_hooks.py --remove           # 移除已配置的 hooks
"""

import argparse
import json
import shutil
from pathlib import Path

# 需要订阅的 Hook 事件（按优先级）
HOOK_EVENTS = [
    # P0：核心事件
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    # P1：重要事件
    "PostToolUse",
    "SubagentStart",
    "SubagentStop",
    "PostCompact",
    # P2：可选事件
    "PreToolUse",
    "PermissionRequest",
    "InstructionsLoaded",
    "StopFailure",
]

COLLECTOR_PATH = Path.home() / ".claude" / "hooks" / "collector.py"


def build_hooks_config(collector_path: Path) -> dict:
    """构建 hooks 配置字典"""
    hooks = {}
    for event in HOOK_EVENTS:
        timeout = 10 if event == "Stop" else 5
        hooks[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 {collector_path}",
                        "timeout": timeout,
                    }
                ]
            }
        ]
    return hooks


def load_settings(settings_path: Path) -> dict:
    """加载已有的 settings.json，不存在则返回空字典"""
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            print(f"⚠️  {settings_path} 格式错误，将覆盖写入")
    return {}


def save_settings(settings_path: Path, data: dict):
    """保存 settings.json"""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def deploy_collector(src: Path):
    """将 collector.py 部署到 ~/.claude/hooks/"""
    dest = COLLECTOR_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(0o755)
    print(f"✅ collector.py 已部署到: {dest}")


def main():
    parser = argparse.ArgumentParser(
        description="自动配置 Claude Code Hooks",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--local", action="store_true", help="写入当前项目的 .claude/settings.local.json")
    parser.add_argument("--show", action="store_true", help="只打印配置，不写入文件")
    parser.add_argument("--remove", action="store_true", help="移除已配置的 hooks")
    parser.add_argument(
        "--collector",
        default=str(COLLECTOR_PATH),
        help="collector.py 的路径",
    )
    args = parser.parse_args()

    # 确定 settings.json 路径
    if args.local:
        settings_path = Path.cwd() / ".claude" / "settings.local.json"
    else:
        settings_path = Path.home() / ".claude" / "settings.json"

    collector_path = Path(args.collector)
    hooks_config = build_hooks_config(collector_path)

    if args.show:
        print(json.dumps({"hooks": hooks_config}, indent=2, ensure_ascii=False))
        return

    if args.remove:
        settings = load_settings(settings_path)
        if "hooks" not in settings:
            print("ℹ️  未找到 hooks 配置，无需移除")
            return
        # 只移除本工具配置的事件
        for event in HOOK_EVENTS:
            settings["hooks"].pop(event, None)
        if not settings["hooks"]:
            del settings["hooks"]
        save_settings(settings_path, settings)
        print(f"✅ hooks 已从 {settings_path} 移除")
        return

    # 部署 collector.py（如果源文件存在）
    src_collector = Path(__file__).parent / "collector.py"
    if src_collector.exists() and not collector_path.exists():
        deploy_collector(src_collector)
    elif not collector_path.exists():
        print(f"⚠️  collector.py 不存在于 {collector_path}，请手动部署")
        print(f"   cp collector.py {collector_path}")

    # 合并写入 settings.json
    settings = load_settings(settings_path)
    existing_hooks = settings.get("hooks", {})

    # 合并：已有的其他 hooks 保留，只覆盖本工具管理的事件
    existing_hooks.update(hooks_config)
    settings["hooks"] = existing_hooks

    save_settings(settings_path, settings)

    print(f"✅ hooks 已写入: {settings_path}")
    print(f"   订阅事件: {', '.join(HOOK_EVENTS)}")
    print()
    print("启动方式：")
    print("  # 终端 1：启动代理")
    print("  python3 proxy.py --port 4000 --output ./trajectories")
    print()
    print("  # 终端 2：通过代理启动 Claude Code")
    print("  ANTHROPIC_BASE_URL=http://localhost:4000 claude")


if __name__ == "__main__":
    main()
