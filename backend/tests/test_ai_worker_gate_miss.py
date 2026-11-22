# -*- coding: utf-8 -*-
"""v536：门卫判定缺失时的丢弃逻辑（_bp_drop_gate_miss）。

这段原来埋在 ai_worker 的逐条循环里（36 行），抽成模块级后才能单测。
它决定两类行的归宿，错一次就是「决策链静默停摆」或「伪造观望卡片」：
- 门卫看不清（gate_unavailable）→ 退回 PENDING + retry_count+1 + 冷却
- 门卫说没信号 → FILTERED（主动不分析，不是失败）
"""
import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as aw


class _FakeGr:
    def __init__(self):
        self.blocks = []

    def apply_block(self, gate, reason):
        self.blocks.append((gate, reason, True))


def _conn(retry=0):
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY, retry_count INTEGER,"
        " status TEXT, processing_at TEXT)"
    )
    c.execute("INSERT INTO raw_news VALUES (7, ?, 'PROCESSING', NULL)", (retry,))
    c.commit()
    return c


def _row(**kw):
    base = {"gate_strength": 0, "gate_verdict": "无信号", "is_noise": 0,
            "gate_unavailable": 0, "retry_count": 0}
    base.update(kw)
    return base


def test_no_gate_status_keeps_row():
    conn, gr = _conn(), _FakeGr()
    assert aw._bp_drop_gate_miss(conn, 7, None, 0, "", _row(), gr) is False
    assert gr.blocks == []


def test_unavailable_goes_back_to_pending_with_cooldown():
    conn, gr = _conn(), _FakeGr()
    ok = aw._bp_drop_gate_miss(
        conn, 7, "PENDING", 1, "门卫不可用",
        _row(gate_strength=None, gate_verdict=None, gate_unavailable=1), gr,
    )
    assert ok is True
    row = conn.execute("SELECT retry_count, status, processing_at FROM raw_news WHERE id=7").fetchone()
    assert row[0] == 1 and row[1] == "PENDING"
    assert row[2] is not None, "必须写冷却时间，否则立刻被重新认领形成热循环"
    assert gr.blocks and gr.blocks[0][0] == "GATE-MISS"


def test_true_no_signal_is_filtered_not_failed():
    conn, gr = _conn(), _FakeGr()
    ok = aw._bp_drop_gate_miss(
        conn, 7, "FILTERED", 0, "门卫判无信号", _row(gate_strength=0, gate_verdict="无信号"), gr,
    )
    assert ok is True
    row = conn.execute("SELECT status, processing_at FROM raw_news WHERE id=7").fetchone()
    assert row == ("FILTERED", None)


def test_update_failure_still_returns_true(monkeypatch):
    """标记失败只能打印（该行靠看门狗回收），不能让整批落库因此中断。"""
    conn, gr = _conn(), _FakeGr()

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(conn, "execute", _boom)
    assert aw._bp_drop_gate_miss(
        conn, 7, "FILTERED", 0, "", _row(), gr,
    ) is True
