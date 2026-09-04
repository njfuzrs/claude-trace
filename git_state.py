#!/usr/bin/env python3
"""
git_state.py — git 仓库状态快照采集（P0）

为什么必须在采集时点记录：
会话开始时的 HEAD 与工作区脏状态是「事后无法重建」的信息。靠会话时间反查
base_commit 在多分支仓库上极其脆弱 —— 未合并分支、squash 为主的合并策略、
多个活跃 worktree 都会让时间反查落到主线上根本不存在的中间状态；而工作区
是否脏（有未提交改动）事后完全无从得知，脏工作区意味着 HEAD 压根不代表
会话的真实起点。

设计约束：
1. 永不抛异常 —— 采集失败返回空 dict，绝不影响代理与 Claude Code 正常工作
2. 永不阻塞 —— 每条 git 命令都带超时，总耗时上界约 2 秒
3. 只读 —— 只执行 rev-parse / status / symbolic-ref 等只读命令
"""

import subprocess
from datetime import datetime
from typing import Dict, Optional

# 单条 git 命令的超时（秒）。仓库很大时 status 可能偏慢，但 2 秒足够，
# 超时即放弃该字段，不影响其他字段。
_GIT_TIMEOUT = 2.0

# status --porcelain 的解析上限，防止超大脏工作区把内存吃满。
# 超过此数量只统计计数，不再保留文件名。
_MAX_DIRTY_FILES = 50


def _git(args, cwd: str, timeout: float = _GIT_TIMEOUT, strip: bool = True) -> Optional[str]:
    """执行一条只读 git 命令，返回 stdout。失败返回 None。

    strip=False 时只去掉尾部换行，保留行首空格 —— `status --porcelain` 的
    状态码位于每行前两个字符且第一位常为空格（如 " M builder.py"），
    整体 strip() 会把首行的行首空格吃掉，导致状态码错位、路径少一个字符。
    """
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # 禁掉交互式凭证提示，避免 git 卡在等待输入
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                 "PATH": _default_path(), "HOME": _default_home()},
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() if strip else proc.stdout.rstrip("\n")


def _default_path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")


def _default_home() -> str:
    import os
    return os.environ.get("HOME", "")


def _parse_porcelain(text: str) -> Dict:
    """解析 git status --porcelain 输出

    porcelain v1 格式：每行前两字符是 XY 状态码，`??` 表示未跟踪。
    """
    modified = 0
    untracked = 0
    staged = 0
    files = []
    for line in text.splitlines():
        if not line or len(line) < 3:
            continue
        code = line[:2]
        path = line[3:].strip()
        if code == "??":
            untracked += 1
        else:
            modified += 1
            # 索引位非空格 = 已 stage
            if code[0] not in (" ", "?"):
                staged += 1
        if len(files) < _MAX_DIRTY_FILES:
            files.append({"status": code, "path": path})
    return {
        "modified_files": modified,
        "untracked_files": untracked,
        "staged_files": staged,
        "dirty_file_list": files,
        "dirty_file_list_truncated": (modified + untracked) > _MAX_DIRTY_FILES,
    }


def collect_git_state(cwd: str, source: str = "proxy") -> Dict:
    """采集 cwd 所在 git 仓库的状态快照

    Args:
        cwd: 工作目录。非 git 仓库或路径无效时返回空 dict。
        source: 采集方（写入快照的 source 字段）。"hook" 表示由 collector.py
            在 SessionStart/SessionEnd 时点采集（时点最准）；"proxy" 表示代理侧
            兜底采集（要等首个 API 请求才知道 cwd，时点偏晚）。下游据此判断
            快照与会话真实起点的贴合程度。

    Returns:
        快照 dict；采集失败或非 git 仓库返回 {}。
        字段：
          head              — HEAD 的完整 sha
          head_short        — 短 sha（便于人读）
          branch            — 分支名；detached HEAD 时为 ""
          detached          — 是否处于 detached HEAD
          dirty             — 工作区是否有未提交改动（含未跟踪文件）
          modified_files    — 已跟踪文件的改动数
          untracked_files   — 未跟踪文件数
          staged_files      — 已 stage 的文件数
          dirty_file_list   — 脏文件明细（上限 50 条）
          repo_root         — 仓库工作树根目录
          is_linked_worktree— 是否为 linked worktree（非主工作树）
          upstream          — 上游分支（如 origin/master）；无则 ""
          ahead / behind    — 相对上游的领先/落后提交数；无上游时为 None
          captured_at       — 采集时间
    """
    if not cwd:
        return {}

    # 先确认是 git 仓库；不是就直接返回，避免后续无意义的命令
    repo_root = _git(["rev-parse", "--show-toplevel"], cwd)
    if not repo_root:
        return {}

    head = _git(["rev-parse", "HEAD"], cwd)
    if not head:
        # 空仓库（还没有任何 commit）：保留仓库信息，head 留空
        head = ""

    state: Dict = {
        "head": head,
        "head_short": head[:12] if head else "",
        "repo_root": repo_root,
        "captured_at": datetime.now().isoformat(),
        "source": source,
    }

    # 分支名：detached HEAD 时 symbolic-ref 会失败
    branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd)
    state["branch"] = branch or ""
    state["detached"] = not bool(branch)

    # 工作区脏状态
    porcelain = _git(["status", "--porcelain", "--untracked-files=normal"], cwd, strip=False)
    if porcelain is None:
        # status 失败（超时等）：dirty 未知，用 None 区分「干净」与「没采到」
        state["dirty"] = None
    else:
        parsed = _parse_porcelain(porcelain)
        state.update(parsed)
        state["dirty"] = bool(parsed["modified_files"] or parsed["untracked_files"])

    # linked worktree 判定：主工作树的 .git 是目录，linked worktree 是文件
    common_dir = _git(["rev-parse", "--git-common-dir"], cwd)
    git_dir = _git(["rev-parse", "--absolute-git-dir"], cwd)
    if common_dir and git_dir:
        # git-common-dir 可能是相对路径，统一成绝对路径再比较
        import os
        common_abs = common_dir if os.path.isabs(common_dir) else os.path.abspath(os.path.join(cwd, common_dir))
        state["is_linked_worktree"] = os.path.normpath(common_abs) != os.path.normpath(git_dir)
    else:
        state["is_linked_worktree"] = None

    # 上游分支与领先/落后数：用于判断 HEAD 是否已推送
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd)
    state["upstream"] = upstream or ""
    if upstream:
        counts = _git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], cwd)
        if counts:
            parts = counts.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                state["behind"] = int(parts[0])
                state["ahead"] = int(parts[1])
    state.setdefault("behind", None)
    state.setdefault("ahead", None)

    return state


def normalize_hook_git_state(payload: Dict) -> Dict:
    """把 collector.py（hook 侧）采集的扁平 git_* 字段转成本模块的规范结构

    接受两种输入形态，都归一到本模块的规范结构：

      1. **规范嵌套形态**（当前 collector.py）——`payload["git_state"]` 直接就是
         本模块 collect_git_state() 的产物。setup_hooks.py 会把 git_state.py
         与 collector.py 一起部署到 ~/.claude/hooks/，因此 hook 侧能直接 import
         本模块，产出的就是规范结构，无需转换。
      2. **扁平兼容形态**（旧版 collector 落下的历史数据）——顶层带 git_head /
         git_dirty / git_branch / git_dirty_files 等扁平字段。保留这条路径是为了
         能读回已经采集的存量 events.jsonl。

    hook 侧的快照优先级更高：它在真正的 SessionStart 时点、在真实 cwd 下采集，
    而代理侧只能在首个 API 请求到达后才知道 cwd，时点偏晚，期间 HEAD 可能已变。
    """
    if not isinstance(payload, dict):
        return {}

    # 形态 1：已是规范结构，打上来源标记后原样返回
    nested = payload.get("git_state")
    if isinstance(nested, dict) and nested.get("head"):
        state = dict(nested)
        state.setdefault("source", "hook")
        if not state.get("captured_at"):
            state["captured_at"] = payload.get("timestamp") or ""
        return state

    # 形态 2：扁平历史数据
    head = payload.get("git_head") or ""
    if not head:
        return {}

    dirty_files = payload.get("git_dirty_files") or []
    normalized_files = []
    for item in dirty_files:
        if isinstance(item, dict):
            normalized_files.append({
                "status": item.get("status", ""),
                "path": item.get("path", ""),
            })
        elif isinstance(item, str):
            # 兼容旧版 collector（只落路径字符串，无状态码）
            normalized_files.append({"status": "", "path": item})

    branch = payload.get("git_branch") or ""
    state: Dict = {
        "head": head,
        "head_short": head[:12],
        "branch": branch,
        "detached": payload.get("git_detached", not bool(branch)),
        "dirty": payload.get("git_dirty"),
        "dirty_file_list": normalized_files,
        "dirty_file_list_truncated": bool(payload.get("git_dirty_files_truncated", False)),
        "captured_at": payload.get("timestamp") or payload.get("captured_at") or "",
        "source": "hook",
    }
    count = payload.get("git_dirty_count")
    if isinstance(count, int):
        # hook 侧不区分 modified / untracked，只有总数。按状态码补算，
        # 拿不到状态码（旧版数据）时留 None，不猜。
        if normalized_files and all(f["status"] for f in normalized_files):
            state["untracked_files"] = sum(1 for f in normalized_files if f["status"] == "??")
            state["modified_files"] = count - state["untracked_files"]
        else:
            state["modified_files"] = None
            state["untracked_files"] = None
        state["dirty_file_count"] = count
    return state


def coerce_git_state(payload: Dict) -> Dict:
    """把任意来源的 git 状态统一成本模块的规范结构

    需要兼容两种输入：
      1. 规范结构（本模块 collect_git_state 的输出，键为 head/branch/dirty）——
         新版 collector.py 直接 import 本模块，送来的就是这种；
      2. 扁平结构（键为 git_head/git_branch/git_dirty）—— 旧版已部署的
         collector.py 内联过一份精简采集逻辑，升级前发出的仍是这种。

    代理必须同时认这两种：用户升级代理后，~/.claude/hooks/ 里可能还是旧
    collector（下次 setup_hooks / install 才会覆盖），此时若只认规范结构，
    git 状态会被静默丢弃。
    """
    if not isinstance(payload, dict) or not payload:
        return {}
    # 规范结构：已有 head 字段，原样返回
    if payload.get("head"):
        return payload
    # 扁平结构：转换
    if payload.get("git_head"):
        return normalize_hook_git_state(payload)
    return {}


def flatten_git_state(state: Dict) -> Dict:
    """从快照中抽出扁平字段，便于直接写进 traj metadata 顶层"""
    if not state:
        return {"git_head": "", "git_branch": "", "git_dirty": None}
    return {
        "git_head": state.get("head", "") or "",
        "git_branch": state.get("branch", "") or "",
        "git_dirty": state.get("dirty"),
    }


if __name__ == "__main__":
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(collect_git_state(target), ensure_ascii=False, indent=2))
