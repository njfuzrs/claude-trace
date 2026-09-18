#!/usr/bin/env python3
"""版本号解析 —— 单一事实源是仓库根的 `version` 文件

为什么需要这个模块：在它出现之前，跑着的进程没法自报「我是哪一次构建」。
所有构建都叫 0.2.0，问「当前用的是哪个版本」只能去看二进制的 mtime / sha256，
或者从 `--help` 里的默认值反推 —— 实测同一个 0.2.0 对应过相差 13 天、
session-timeout 从 300 变 1800 的两份二进制。

打包后的二进制里没有仓库目录，所以 build 把 `version` 文件作为 PyInstaller
的 datas 塞进包内，运行时从 `sys._MEIPASS` 读。三级回落：
包内 → 源码树 → "unknown"（绝不抛异常，版本号读不到不该让采集起不来）。
"""

import os
import sys
from pathlib import Path

UNKNOWN = "unknown"


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []

    # 1. PyInstaller 包内（onefile 解包目录 / onedir 的 _internal）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "version")

    # 2. 源码树：本文件就在仓库根
    paths.append(Path(__file__).resolve().parent / "version")

    # 3. 二进制同级目录（安装目录里 version 与 bin/ 同级，故也看上一层）
    exe_dir = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else None
    if exe_dir:
        paths.append(exe_dir / "version")
        paths.append(exe_dir.parent / "version")

    return paths


def resolve_version() -> str:
    """读版本号。任何异常都吞掉并回落 —— 这不是关键路径。"""
    for path in _candidate_paths():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0].strip()
    return UNKNOWN


def build_fingerprint() -> str:
    """可执行文件的 sha256 前 12 位。版本号相同时，这是唯一能区分两份构建的东西。"""
    import hashlib

    target = sys.executable if getattr(sys, "frozen", False) else None
    if not target:
        return ""
    try:
        digest = hashlib.sha256(Path(target).read_bytes()).hexdigest()
    except OSError:
        return ""
    return digest[:12]


def version_string() -> str:
    """`--version` 的输出：版本号 + 运行形态 + 指纹。"""
    parts = [f"claude-trace {resolve_version()}"]
    if getattr(sys, "frozen", False):
        fp = build_fingerprint()
        parts.append(f"(binary{', sha256:' + fp if fp else ''})")
    else:
        parts.append(f"(source, python {sys.version.split()[0]})")
    return " ".join(parts)


if __name__ == "__main__":
    print(version_string())
    # 便于排查：把实际探测到的路径也打出来
    if os.environ.get("CLAUDE_TRACE_VERSION_DEBUG"):
        for p in _candidate_paths():
            print(f"  {'✓' if p.exists() else '✗'} {p}")
