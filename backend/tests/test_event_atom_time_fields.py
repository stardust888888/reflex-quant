"""v536 — event_atom 落库 LLM 的时间结构化字段。

event_window_minutes / data_anchor_ts 以前只在 extract_and_persist 的签名里被收下，
之后一次都没写进库：ai_worker 每次都传，数据库每次都丢。下游拿不到"这条事件
影响多久、以哪个时间为锚"，只能用默认值猜。
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import event_atom
import seed_event_dim


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    seed_event_dim.seed_all(connection=conn)
    return conn


def test_columns_exist_after_migrate():
    conn = _fresh_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_atom)")}
    assert {"event_window_minutes", "data_anchor_ts"} <= cols
    conn.close()


def test_window_and_anchor_are_persisted():
    conn = _fresh_db()
    eid = event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="rate_hike", event_time=1700000000,
        asset_code="XAU", event_window_minutes=120,
        data_anchor_ts="2026-11-20T10:00:00Z", connection=conn,
    )
    assert eid > 0
    row = conn.execute(
        "SELECT event_window_minutes, data_anchor_ts FROM event_atom WHERE id=?",
        (eid,)).fetchone()
    assert row["event_window_minutes"] == 120
    assert row["data_anchor_ts"] == "2026-11-20T10:00:00Z"
    conn.close()


def test_llm_na_anchor_is_stored_as_empty():
    """LLM 回 'N/A' 表示没给锚点，不该把 'N/A' 当内容写进库。"""
    conn = _fresh_db()
    eid = event_atom.insert_event_atom(
        raw_news_id=2, event_type_code="rate_hike", event_time=1700000000,
        data_anchor_ts="N/A", connection=conn,
    )
    assert eid > 0
    row = conn.execute(
        "SELECT data_anchor_ts, event_window_minutes FROM event_atom WHERE id=?",
        (eid,)).fetchone()
    assert row["data_anchor_ts"] == ""
    assert row["event_window_minutes"] is None   # 没给就是 NULL，不写 0
    conn.close()


def test_extract_and_persist_forwards_the_fields(monkeypatch):
    """端到端：extract_and_persist 必须把这两个字段传给 insert_event_atom。

    直接断言落库也行，但那依赖规则解析某条真实文本；这里锁"传参不断链"。
    """
    captured = {}

    def _fake_insert(**kw):
        captured.update(kw)
        return 0  # 0 = 不追加 id，走正常跳过分支

    monkeypatch.setattr(event_atom, "insert_event_atom", _fake_insert)
    monkeypatch.setattr(event_atom.event_struct, "extract_events",
                        lambda text: [{"event_type": "rate_hike",
                                       "target_assets": ["XAU"]}])
    conn = _fresh_db()
    event_atom.extract_and_persist(
        "美联储加息 25bp", raw_news_id=7, default_event_time=1700000000,
        event_window_minutes=45, data_anchor_ts="2026-11-20T10:00:00Z",
        connection=conn,
    )
    assert captured.get("event_window_minutes") == 45
    assert captured.get("data_anchor_ts") == "2026-11-20T10:00:00Z"
    conn.close()
