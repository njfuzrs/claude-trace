#!/usr/bin/env python3
"""
sync.py — 轨迹数据手动批量补传（会话维度）

将本地 sessions/ 下的会话数据上传到云端平台。
每个会话目录包含 session.traj、raw.jsonl、events.jsonl 三个文件。
支持增量同步（只上传新文件）和全量同步。

与 uploader.py 的分工：
- uploader.py：采集进程内自动传 + 启动补传（异步队列）
- sync.py：uploader 没跑时的手动批量（历史积压、其它机器目录）

不把逻辑并进 uploader.py：一边是异步队列，一边是同步 CLI。

上传是 opt-in：TRAJ_PLATFORM_URL 与 TRAJ_UPLOAD_TOKEN 都必须显式提供，
仓内无默认端点。身份字段只认环境变量，空则不上报（不回退系统用户名/主机名）。

默认读安装器落点 ~/.claude-trace/trajectories/sessions/；
该目录不存在时回退到 ./trajectories/sessions。可用 TRAJ_LOCAL_DIR 覆盖。

用法：
    python3 sync.py                          # 增量同步（只传新文件）
    python3 sync.py --all                    # 全量同步
    python3 sync.py --session <session_id>   # 上传单个会话
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
import http.client
from pathlib import Path
from urllib.parse import urlparse


def _require_env(name: str) -> str:
    """读必填环境变量。缺失即退出 —— 无内置端点、无内置凭据。"""
    val = os.environ.get(name, "")
    if not val:
        raise SystemExit(f"缺少必需的环境变量 {name}（上传是 opt-in，须显式 export）")
    return val


def _default_sessions_dir() -> Path:
    """安装器落点优先，源码开发目录其次。一律 expanduser。"""
    override = os.environ.get("TRAJ_LOCAL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    installed = (Path.home() / ".claude-trace" / "trajectories" / "sessions").expanduser()
    if installed.exists():
        return installed
    return (Path.cwd() / "trajectories" / "sessions").expanduser()


PLATFORM_URL = _require_env("TRAJ_PLATFORM_URL").rstrip("/")
UPLOAD_TOKEN = _require_env("TRAJ_UPLOAD_TOKEN")
LOCAL_SESSIONS_DIR = _default_sessions_dir()
SYNC_STATE_DIR = Path.home() / ".claude-trace"
SYNC_STATE_FILE = SYNC_STATE_DIR / ".sync_state.json"

# 会话目录下需要上传的文件 → (file_type, filename)
SESSION_FILES = [
    ("traj", "session.traj"),
    ("raw", "raw.jsonl"),
    ("events", "events.jsonl"),
]

# 只认环境变量；空则不上报，不回退系统登录名或主机名
USER_ID = os.environ.get("TRAJ_USER_ID", "").strip()
DEVICE_ID = os.environ.get("TRAJ_DEVICE_ID", "").strip()


def compress_and_hash(filepath: Path) -> tuple:
    """gzip 压缩文件并计算压缩后的 SHA256（stdlib only）

    返回 (gz_path, sha256_hex)。上传完成后调用方负责清理 gz_path。
    """
    gz_path = filepath.with_suffix(filepath.suffix + ".gz")
    with open(filepath, "rb") as f_in:
        with gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    sha256 = hashlib.sha256()
    with open(gz_path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return gz_path, sha256.hexdigest()


def load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        return json.loads(SYNC_STATE_FILE.read_text())
    return {"uploaded": {}, "stats": {"total_uploaded": 0, "total_errors": 0}}


def save_sync_state(state: dict):
    SYNC_STATE_DIR.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def session_fingerprint(session_dir: Path) -> str:
    """会话级指纹：所有文件指纹的组合"""
    parts = []
    for _, filename in SESSION_FILES:
        fp = session_dir / filename
        if fp.exists():
            parts.append(f"{filename}={file_fingerprint(fp)}")
    return "|".join(parts)


def upload_session_file(
    session_id: str, file_type: str, filepath: Path, tool_source: str = "claude-code",
) -> dict:
    """上传单个会话文件到云端平台（gzip 压缩 + SHA256 校验）"""
    gz_path, sha256 = compress_and_hash(filepath)
    try:
        return _do_upload(session_id, file_type, gz_path, sha256, tool_source)
    finally:
        gz_path.unlink(missing_ok=True)


def _do_upload(
    session_id: str, file_type: str, gz_path: Path, sha256: str, tool_source: str,
) -> dict:
    """执行实际的 HTTP 上传（带重试）"""
    boundary = "----TrajectoryUploadBoundary"
    gz_filename = gz_path.name
    gz_size = gz_path.stat().st_size

    def _form_field(name: str, value: str) -> str:
        return (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}"
        )

    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{gz_filename}"\r\n'
        "Content-Type: application/gzip\r\n\r\n"
    )

    fields = (
        _form_field("session_id", session_id)
        + _form_field("file_type", file_type)
        + _form_field("tool_source", tool_source)
        + _form_field("compressed", "true")
        + _form_field("user_id", USER_ID)
        + _form_field("device_id", DEVICE_ID)
        + f"\r\n--{boundary}--\r\n"
    )

    header_bytes = file_header.encode()
    tail_bytes = fields.encode()
    content_length = len(header_bytes) + gz_size + len(tail_bytes)

    parsed = urlparse(PLATFORM_URL)
    host = parsed.hostname
    if not host:
        return {"error": f"PLATFORM_URL 解析失败,缺少 host: {PLATFORM_URL}"}
    is_https = parsed.scheme == "https"
    port = parsed.port or (443 if is_https else 80)
    base_path = (parsed.path or "").rstrip("/")
    upload_path = f"{base_path}/api/v1/upload/session-file"

    for attempt in range(3):
        conn = None
        try:
            if is_https:
                conn = http.client.HTTPSConnection(host, port, timeout=60)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=60)
            conn.connect()
            conn.putrequest("POST", upload_path)
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(content_length))
            conn.putheader("X-Upload-Token", UPLOAD_TOKEN)
            conn.putheader("X-Content-SHA256", sha256)
            conn.endheaders()

            conn.send(header_bytes)
            with open(gz_path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
            conn.send(tail_bytes)

            resp = conn.getresponse()
            body = resp.read()
            conn.close()

            if resp.status == 409:
                return {"status": "skipped", "reason": "already exists"}
            if resp.status in (200, 201):
                result = json.loads(body)
                server_sha = result.get("sha256", "")
                if server_sha and server_sha != sha256:
                    return {"error": f"SHA256 不匹配: local={sha256[:16]} server={server_sha[:16]}"}
                return result
            return {"error": f"HTTP {resp.status}: {body.decode()[:200]}"}

        except Exception as e:
            if attempt < 2:
                wait = [5, 15][attempt]
                print(f"    重试 ({attempt + 1}/3)，等待 {wait}s...")
                time.sleep(wait)
            else:
                return {"error": str(e)}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return {"error": "重试耗尽"}


def sync_session(session_dir: Path, tool_source: str = "claude-code") -> dict:
    """同步单个会话目录下的所有文件"""
    session_id = session_dir.name
    results = {}

    for file_type, filename in SESSION_FILES:
        filepath = session_dir / filename
        if not filepath.exists():
            continue

        size_kb = filepath.stat().st_size / 1024
        result = upload_session_file(session_id, file_type, filepath, tool_source)

        if "error" in result:
            print(f"    ❌ {filename} ({size_kb:.1f} KB): {result['error']}")
            results[filename] = "error"
        elif result.get("status") == "skipped":
            results[filename] = "skipped"
        else:
            print(f"    ✅ {filename} ({size_kb:.1f} KB) → {result.get('status', 'ok')}")
            results[filename] = "ok"

    return results


def sync_incremental(tool_source: str = "claude-code", force: bool = False):
    """增量同步：只上传新会话或已修改的会话"""
    state = load_sync_state()
    uploaded = state.get("uploaded", {})

    if not LOCAL_SESSIONS_DIR.exists():
        print(f"错误: 本地目录不存在: {LOCAL_SESSIONS_DIR}")
        sys.exit(1)

    session_dirs = sorted([
        d for d in LOCAL_SESSIONS_DIR.iterdir()
        if d.is_dir() and (d / "session.traj").exists()
    ])

    if not session_dirs:
        print("没有找到有效的会话目录")
        return

    new_count = 0
    skip_count = 0
    error_count = 0

    print(f"平台地址: {PLATFORM_URL}")
    print(f"本地目录: {LOCAL_SESSIONS_DIR}")
    print(f"\n扫描 {len(session_dirs)} 个会话...\n")

    for session_dir in session_dirs:
        sid = session_dir.name

        # 跳过已由 SDK 自动上传的会话
        if (session_dir / ".uploaded").exists():
            skip_count += 1
            continue

        fp = session_fingerprint(session_dir)

        if not force and sid in uploaded and uploaded[sid] == fp:
            skip_count += 1
            continue

        print(f"同步会话: {sid[:12]}...")
        results = sync_session(session_dir, tool_source)

        if any(v == "error" for v in results.values()):
            error_count += 1
        else:
            uploaded[sid] = fp
            if all(v == "skipped" for v in results.values()):
                print("    ⏭️  全部已存在")
                skip_count += 1
            else:
                new_count += 1

    state["uploaded"] = uploaded
    state["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    state["stats"]["total_uploaded"] = state["stats"].get("total_uploaded", 0) + new_count
    state["stats"]["total_errors"] = state["stats"].get("total_errors", 0) + error_count
    save_sync_state(state)

    print(f"\n同步完成: 上传 {new_count}, 跳过 {skip_count}, 失败 {error_count}")


def sync_single(session_id: str, tool_source: str = "claude-code"):
    """上传单个会话"""
    session_dir = LOCAL_SESSIONS_DIR / session_id
    if not session_dir.is_dir():
        print(f"错误: 会话目录不存在: {session_dir}")
        sys.exit(1)

    print(f"同步会话: {session_id[:12]}...")
    results = sync_session(session_dir, tool_source)

    if any(v == "error" for v in results.values()):
        sys.exit(1)

    state = load_sync_state()
    state["uploaded"][session_id] = session_fingerprint(session_dir)
    state["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_sync_state(state)


def main():
    parser = argparse.ArgumentParser(description="轨迹数据手动批量补传（会话维度）")
    parser.add_argument("--all", action="store_true", help="全量同步（忽略已上传记录）")
    parser.add_argument("--session", help="上传单个会话（session_id）")
    parser.add_argument("--tool-source", default="claude-code", help="工具来源标识")
    args = parser.parse_args()

    print(f"平台地址: {PLATFORM_URL}")
    print(f"本地目录: {LOCAL_SESSIONS_DIR}\n")

    if args.session:
        sync_single(args.session, args.tool_source)
    else:
        sync_incremental(args.tool_source, force=args.all)


if __name__ == "__main__":
    main()
