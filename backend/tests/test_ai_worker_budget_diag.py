# -*- coding: utf-8 -*-
"""v536：_batch_claim 拆出的两个辅助函数的测试。

_batch_claim 原来 116 行内嵌在 ai_worker 里，整体提到模块级后复杂度 27 被门禁
拦下（新函数上限 25）。于是把「日预算闸」和「长锁诊断 dump」拆出来，这里锁住
两者的行为：预算到顶返回 True、预算为 0 表示不限、诊断只在超过 5s 时输出。
"""
import os
import sqlite3
import sys
import time
import traceback

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as aw


def _stats_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_call_stats (purpose TEXT, ts INTEGER)")
    now = int(time.time())
    conn.executemany(
        "INSERT INTO ai_call_stats (purpose, ts) VALUES ('main', ?)", [(now,)] * rows
    )
    conn.commit()
    return conn


def test_budget_exhausted_at_limit(monkeypatch):
    monkeypatch.setenv("AI_MAIN_DAILY_BUDGET", "600")
    monkeypatch.setattr(aw, "_open_db", lambda: _stats_conn(600))
    assert aw._main_budget_exhausted() is True


def test_budget_not_exhausted_below_limit(monkeypatch):
    monkeypatch.setenv("AI_MAIN_DAILY_BUDGET", "600")
    monkeypatch.setattr(aw, "_open_db", lambda: _stats_conn(599))
    assert aw._main_budget_exhausted() is False


def test_budget_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("AI_MAIN_DAILY_BUDGET", "0")
    monkeypatch.setattr(aw, "_open_db", lambda: _stats_conn(10 ** 6))
    assert aw._main_budget_exhausted() is False


def test_budget_query_error_is_fail_open(monkeypatch):
    """统计表读不出来时不能把整个认领流程卡死，按「没超预算」放行。"""
    monkeypatch.setenv("AI_MAIN_DAILY_BUDGET", "600")

    def _boom():
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(aw, "_open_db", _boom)
    assert aw._main_budget_exhausted() is False


def test_lock_diag_skips_short_block(monkeypatch):
    written = []
    monkeypatch.setattr(aw.os, "write", lambda _fd, b: written.append(b))
    aw._dump_lock_diag(traceback, 3.0)
    assert written == []


def test_lock_diag_dumps_when_block_long(monkeypatch):
    written = []
    monkeypatch.setattr(aw.os, "write", lambda _fd, b: written.append(b))
    aw._dump_lock_diag(traceback, 12.0)
    text = b"".join(written).decode()
    assert "ALL THREAD STACKS" in text
    assert "12.0" in text
    assert "active threads" in text
