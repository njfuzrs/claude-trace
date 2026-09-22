"""Codex sqlite 兜底：缺表不能把整次导出打失败。

Codex 2026-08 把 logs 从表 state_5.sqlite 拆走（迁移名 drop logs）。
旧查询仍打 state_5 会抛 no such table: logs，watcher 当成导出失败反复重试。
"""

import sqlite3

from import_codex import _load_sqlite_rows


def test_load_sqlite_rows_missing_file(tmp_path):
    assert _load_sqlite_rows(tmp_path / "nope.sqlite", "select 1") == []


def test_load_sqlite_rows_missing_table_returns_empty(tmp_path):
    db = tmp_path / "state_5.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("create table threads (id text)")
    conn.commit()
    conn.close()
    rows = _load_sqlite_rows(
        db,
        "select * from logs where thread_id = ? order by ts, ts_nanos, id",
        ("abc",),
    )
    assert rows == []


def test_load_sqlite_rows_existing_table(tmp_path):
    db = tmp_path / "logs_1.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "create table logs (id integer, thread_id text, ts integer, ts_nanos integer)"
    )
    conn.execute("insert into logs values (1, 'abc', 1, 0)")
    conn.commit()
    conn.close()
    rows = _load_sqlite_rows(
        db,
        "select * from logs where thread_id = ? order by ts, ts_nanos, id",
        ("abc",),
    )
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "abc"
