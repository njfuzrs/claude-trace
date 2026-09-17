#!/usr/bin/env python3
"""save_trajectory 的收缩闸门测试

对应 P0-A：会话超时被清理后用户又提问，代理新建 pairs 为空的 Session，
每轮都 save_trajectory 覆盖写 → 几小时的完整轨迹被只含最后几步的短轨迹冲掉。
实测 136 个会话、13313 次 API 调用因此从 traj 里消失。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from builder import _traj_step_count, save_trajectory  # noqa: E402


def _traj(steps: int) -> dict:
    return {"trajectory": [{"i": i} for i in range(steps)], "metadata": {}}


def test_shrink_blocked(tmp_path):
    """★核心：allow_shrink=False 时，短轨迹不得覆盖长轨迹"""
    p = tmp_path / "session.traj"
    save_trajectory(p, _traj(240))
    # 模拟会话复活后只剩 11 步的导出
    save_trajectory(p, _traj(11), allow_shrink=False)
    assert len(json.loads(p.read_text())["trajectory"]) == 240


def test_grow_allowed(tmp_path):
    """正常增长必须放行，否则会话越写越停滞"""
    p = tmp_path / "session.traj"
    save_trajectory(p, _traj(10))
    save_trajectory(p, _traj(11), allow_shrink=False)
    assert len(json.loads(p.read_text())["trajectory"]) == 11


def test_equal_allowed(tmp_path):
    """步数相同放行：同一轮可能因 partial→complete 重写内容"""
    p = tmp_path / "session.traj"
    save_trajectory(p, _traj(5))
    t = _traj(5)
    t["metadata"]["updated"] = True
    save_trajectory(p, t, allow_shrink=False)
    assert json.loads(p.read_text())["metadata"]["updated"] is True


def test_default_allows_shrink(tmp_path):
    """默认 allow_shrink=True 保持旧行为（每轮增量写不受影响）"""
    p = tmp_path / "session.traj"
    save_trajectory(p, _traj(9))
    save_trajectory(p, _traj(2))
    assert len(json.loads(p.read_text())["trajectory"]) == 2


def test_corrupt_old_file_is_overwritten(tmp_path):
    """旧文件损坏（读不出步数）时必须放行，否则坏文件永远修不掉"""
    p = tmp_path / "session.traj"
    p.write_text("{ this is not json")
    save_trajectory(p, _traj(3), allow_shrink=False)
    assert len(json.loads(p.read_text())["trajectory"]) == 3


def test_atomic_write_leaves_no_tmp(tmp_path):
    """原子写：成功后不留 .tmp 残留"""
    p = tmp_path / "session.traj"
    save_trajectory(p, _traj(4))
    assert list(tmp_path.glob("*.tmp")) == []


def test_step_count_large_file_empty_trajectory(tmp_path):
    """大文件走头部快路径：空 trajectory 要能判成 0，不能误判成未知"""
    p = tmp_path / "session.traj"
    big = {"trajectory": [], "metadata": {"pad": "x" * 500_000}}
    p.write_text(json.dumps(big))
    assert p.stat().st_size > 400 * 1024
    assert _traj_step_count(p) == 0
    # 空轨迹不该阻挡新数据写入
    save_trajectory(p, _traj(7), allow_shrink=False)
    assert len(json.loads(p.read_text())["trajectory"]) == 7
