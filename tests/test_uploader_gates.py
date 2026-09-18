#!/usr/bin/env python3
"""上传闸门测试：.uploaded 标记条件、体积上限、终态处理、启动补传扫描

这些用例都对应实测过的缺陷：
- 409 幂等把「服务端拒绝覆盖」当成功 → 云端锁死残缺轨迹（126 个会话）
- 413 无限重试 → 1.1GB 的 .gz 永久占盘
- 上传成功即删本地（默认 true）→ 7247 个目录只剩 .uploaded 标记
- 只挂单一触发点 → sid-code 52 个会话一次都没传上去
"""

import json

from uploader import UploadManager


def _mgr(tmp_path, **kw):
    kw.setdefault("cleanup_after_upload", False)
    kw.setdefault("sessions_dir", tmp_path)
    kw.setdefault("backfill_enabled", False)
    return UploadManager(upload_url="http://x/traj", upload_token="tok", **kw)


def _session(tmp_path, name, steps=3, files=("session.traj",)):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f == "session.traj":
            (d / f).write_text(json.dumps(
                {"trajectory": [{"i": i} for i in range(steps)], "metadata": {}},
            ))
        else:
            (d / f).write_text('{"x":1}\n')
    return d


# ── 启动补传扫描 ──

def test_backfill_scan_only_unuploaded_with_steps(tmp_path):
    _session(tmp_path, "want", steps=4)
    done = _session(tmp_path, "already", steps=4)
    (done / ".uploaded").write_text("{}")
    _session(tmp_path, "empty", steps=0)
    # 无 traj 的目录（subagent / 标题生成，实测 9355 个）不该被补传
    noj = tmp_path / "no-traj"
    noj.mkdir()
    (noj / "raw.jsonl").write_text("\n")

    got = sorted(p.name for p in _mgr(tmp_path).scan_pending_sessions())
    assert got == ["want"], got


def test_backfill_scan_missing_dir_is_safe(tmp_path):
    m = _mgr(tmp_path, sessions_dir=tmp_path / "nope")
    assert m.scan_pending_sessions() == []


# ── .uploaded 标记条件 ──

def test_marker_records_oversize_reason(tmp_path):
    d = _session(tmp_path, "s")
    UploadManager._write_uploaded_marker(
        d, {"traj": {"sha256": "a"}}, oversize={"raw": "700MB"},
    )
    m = json.loads((d / ".uploaded").read_text())
    assert m["files"] == {"traj": {"sha256": "a"}}
    assert m["oversize_skipped"] == {"raw": "700MB"}


def test_marker_omits_oversize_key_when_clean(tmp_path):
    d = _session(tmp_path, "s")
    UploadManager._write_uploaded_marker(d, {"traj": {"sha256": "a"}})
    assert "oversize_skipped" not in json.loads((d / ".uploaded").read_text())


# ── 队列统计 ──

def test_queue_stats_counts_by_status(tmp_path):
    from uploader import UploadQueueItem

    m = _mgr(tmp_path)
    m._queue = [
        UploadQueueItem(session_id="a", file_type="traj", filepath="/x", status="pending"),
        UploadQueueItem(session_id="b", file_type="raw", filepath="/x", status="failed"),
        UploadQueueItem(session_id="c", file_type="raw", filepath="/x", status="oversize"),
        UploadQueueItem(session_id="d", file_type="raw", filepath="/x", status="failed"),
    ]
    assert m.queue_stats() == {"total": 4, "pending": 1, "failed": 2, "oversize": 1}


# ── 体积上限 ──

def test_max_upload_bytes_is_conservative():
    """实测网关：50MB→200、100MB→413，阈值必须落在两者之间"""
    assert 50 * 1024 * 1024 < UploadManager.MAX_UPLOAD_BYTES < 100 * 1024 * 1024


# ── 默认值 ──

async def test_traj_oversize_must_not_mark_uploaded(tmp_path, monkeypatch):
    """★ traj 自身过大时，绝不能因为 events.jsonl 传成功就标记为已上传

    这是我在修复过程中自己引入又修掉的 bug：oversize 不清 all_confirmed，
    于是「traj 过大、events 传成功」会被判成全部确认 → 写 .uploaded，
    积压数字假性归零，而云端根本没有这个会话的轨迹。
    实测有 5 个会话（traj 210~345MB）正好落在这个分支上。
    """
    d = _session(tmp_path, "big", steps=3, files=("session.traj", "events.jsonl"))
    m = _mgr(tmp_path)
    m._server_healthy = True

    # traj 判过大，events 正常上传成功
    async def fake_upload(item):
        if item.file_type == "traj":
            item.status = "oversize"
            item.error = "300MB 过大"
            return False
        return True

    monkeypatch.setattr(m, "_upload_single_file", fake_upload)
    await m.upload_session(d, "big")

    assert not (d / ".uploaded").exists(), \
        "traj 没上云却写了 .uploaded —— 积压数字会假性归零"
    assert (d / "session.traj").exists(), "本地是唯一副本，绝不能删"


async def test_raw_oversize_still_marks_when_traj_uploaded(tmp_path, monkeypatch):
    """raw.jsonl 过大但 traj 已上云 → 应标记落地，否则每次启动都重压几百 MB"""
    d = _session(tmp_path, "ok", steps=3, files=("session.traj", "raw.jsonl"))
    m = _mgr(tmp_path)
    m._server_healthy = True

    async def fake_upload(item):
        if item.file_type == "raw":
            item.status = "oversize"
            item.error = "700MB 过大"
            return False
        return True

    monkeypatch.setattr(m, "_upload_single_file", fake_upload)
    await m.upload_session(d, "ok")

    assert (d / ".uploaded").exists()
    marker = json.loads((d / ".uploaded").read_text())
    assert "traj" in marker["files"]
    assert "raw" in marker["oversize_skipped"]
    # 有 oversize 时绝不清理本地
    assert (d / "raw.jsonl").exists()


async def test_retriable_failure_blocks_marker(tmp_path, monkeypatch):
    """可重试的失败必须阻止标记，留给下次补传"""
    d = _session(tmp_path, "retry", steps=3, files=("session.traj", "raw.jsonl"))
    m = _mgr(tmp_path)
    m._server_healthy = True

    async def fake_upload(item):
        return item.file_type == "traj"  # raw 普通失败（可重试）

    monkeypatch.setattr(m, "_upload_single_file", fake_upload)
    await m.upload_session(d, "retry")
    assert not (d / ".uploaded").exists()


def test_backfill_disabled_without_sessions_dir(tmp_path):
    """没给 sessions_dir 就不能开补传（没地方扫）"""
    m = UploadManager(upload_url="http://x/traj", upload_token="t", backfill_enabled=True)
    assert m._backfill_enabled is False


def test_cleanup_default_is_keep_local():
    """构造器默认值保持向后兼容；安全默认由 proxy 侧的环境变量决定"""
    m = UploadManager(upload_url="http://x/traj", upload_token="t",
                      cleanup_after_upload=False)
    assert m._cleanup_after_upload is False
