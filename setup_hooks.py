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
#
# Fix: Claude Code 升级后新增了若干 hook 事件（BeforeModel / AfterModel /
# PreCompact / PostToolUseFailure / Notification / SessionResume 等）。
# 实测已上传的 events.jsonl 里已经出现 BeforeModel / AfterModel /
# InstructionsLoaded / PostToolUseFailure / StopFailure / SubagentStart，
# 而这里的订阅列表和 collector.py 的分支都不认识它们。
# 现在补全全量事件，缺失的事件在旧版 Claude Code 上会被忽略，不影响兼容。
HOOK_EVENTS = [
    # P0：核心事件 — 会话生命周期与用户输入
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    # P1：重要事件 — 工具调用与子代理
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    # P1：上下文压缩（PreCompact 带压缩前状态，PostCompact 带摘要）
    "PreCompact",
    "PostCompact",
    # P2：模型调用边界 — 用于精确对齐 API 请求与会话 turn
    "BeforeModel",
    "AfterModel",
    # P2：其他
    "PermissionRequest",
    "InstructionsLoaded",
    "StopFailure",
    "Notification",
]

COLLECTOR_PATH = Path.home() / ".claude" / "hooks" / "collector.py"

# collector.py 在 hooks 目录下运行时需要的同目录依赖
HOOK_DEPS = ["git_state.py"]


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
    """将 collector.py 及其依赖部署到 ~/.claude/hooks/"""
    dest = COLLECTOR_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(0o755)
    print(f"✅ collector.py 已部署到: {dest}")

    # git_state.py 是 collector.py 的依赖（采集 git HEAD / 脏状态）。
    # 必须同目录部署：hook 以 `python3 ~/.claude/hooks/collector.py` 方式运行，
    # sys.path[0] 即 hooks 目录，漏掉这个文件会让 collector 静默降级为不采 git 状态。
    for dep in HOOK_DEPS:
        src_dep = src.parent / dep
        if src_dep.exists():
            shutil.copy2(src_dep, dest.parent / dep)
            print(f"✅ {dep} 已部署到: {dest.parent / dep}")
        else:
            print(f"⚠️  依赖 {dep} 不存在于 {src_dep}，git 状态采集将不可用")


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
        # 只移除本工具配置的 hook 条目（包含 collector.py 命令的），保留用户自定义的
        for event in HOOK_EVENTS:
            if event not in settings["hooks"]:
                continue
            hook_groups = settings["hooks"][event]
            filtered = []
            for group in hook_groups:
                kept_hooks = [
                    h for h in group.get("hooks", [])
                    if "collector.py" not in h.get("command", "")
                ]
                if kept_hooks:
                    group["hooks"] = kept_hooks
                    filtered.append(group)
            if filtered:
                settings["hooks"][event] = filtered
            else:
                del settings["hooks"][event]
        if not settings["hooks"]:
            del settings["hooks"]
        save_settings(settings_path, settings)
        print(f"✅ hooks 已从 {settings_path} 移除")
        return

    # 部署 collector.py（源文件存在时总是覆盖，确保更新后的版本被部署）
    src_collector = Path(__file__).parent / "collector.py"
    if src_collector.exists():
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
    print("  # 终端 1：启动统一采集器（开发入口与生产入口相同）")
    print("  python3 trace_agent.py --port 4000 --output ./trajectories")
    print()
    print("  # 终端 2：通过代理启动 Claude Code")
    print("  ANTHROPIC_BASE_URL=http://localhost:4000 claude")


if __name__ == "__main__":
    main()
