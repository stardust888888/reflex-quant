# -*- coding: utf-8 -*-
"""v536：_do_reclaim / _do_claim 提取到模块级后的行为锁定。

这两个函数原来内嵌在 ai_worker 里，靠闭包拿 conn；提到模块级后改为显式传参。
它们直接决定「待分析」队列能不能自我恢复，所以用真实 SQLite 验一条主路径：
卡死 PROCESSING 能被回收、在办批次不被误回收、认领失败不影响连接归属。
"""
import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as aw


def _mk_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY, status TEXT,"
        " retry_count INTEGER DEFAULT 0, processing_at TEXT, ts INTEGER)"
    )
    return conn


def _add(conn, rid, status, retry, minutes_ago=None, ts=None):
    if minutes_ago is None:
        pat = None
    else:
        pat = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - minutes_ago * 60)
        )
    conn.execute(
        "INSERT INTO raw_news (id, status, retry_count, processing_at, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (rid, status, retry, pat, ts),
    )
    conn.commit()


def test_reclaim_turns_stuck_processing_back_to_pending(monkeypatch):
    conn = _mk_conn()
    _add(conn, 1, "PROCESSING", 0, minutes_ago=30)
    monkeypatch.setattr(aw, "_active_batch_live_ids", lambda: [])

    n = aw._do_reclaim(conn)

    assert n == 1
    row = conn.execute("SELECT status, retry_count FROM raw_news WHERE id=1").fetchone()
    assert row == ("PENDING", 1)


def test_reclaim_skips_live_batch_and_fresh_claims(monkeypatch):
    conn = _mk_conn()
    _add(conn, 2, "PROCESSING", 0, minutes_ago=30)   # 在办批次：租约还在
    _add(conn, 3, "PROCESSING", 0, minutes_ago=1)    # 刚认领，不算卡死
    monkeypatch.setattr(aw, "_active_batch_live_ids", lambda: [2])

    n = aw._do_reclaim(conn)

    assert n == 0
    assert conn.execute("SELECT status FROM raw_news WHERE id IN (2, 3)").fetchall() == [
        ("PROCESSING",), ("PROCESSING",),
    ]


def test_reclaim_marks_failed_after_retry_budget(monkeypatch):
    conn = _mk_conn()
    _add(conn, 4, "PROCESSING", aw._MAX_PENDING_RETRIES, minutes_ago=30)
    monkeypatch.setattr(aw, "_active_batch_live_ids", lambda: [])

    aw._do_reclaim(conn)

    assert conn.execute("SELECT status FROM raw_news WHERE id=4").fetchone()[0] == "FAILED"


def test_claim_returns_rows_and_commits(monkeypatch):
    conn = _mk_conn()
    monkeypatch.setattr(aw, "_has_fresh_tradeable", lambda _c: False)
    monkeypatch.setattr(aw, "_build_claim_sql", lambda _tb=None: ("SELECT id FROM raw_news", []))
    _add(conn, 5, "PENDING", 0)

    rows = aw._do_claim(conn)

    assert [r["id"] if not isinstance(r, tuple) else r[0] for r in rows] == [5]


def test_claim_prefilter_error_is_swallowed(monkeypatch):
    conn = _mk_conn()
    monkeypatch.setattr(aw, "_has_fresh_tradeable", lambda _c: False)
    monkeypatch.setattr(aw, "_build_claim_sql", lambda _tb=None: ("SELECT id FROM raw_news", []))
    _add(conn, 6, "PENDING", 0)

    import analysis.prefilter as pf
    monkeypatch.setattr(pf, "apply", lambda _c, rows: (_ for _ in ()).throw(RuntimeError("boom")))

    rows = aw._do_claim(conn)   # 不应抛

    assert len(rows) == 1
