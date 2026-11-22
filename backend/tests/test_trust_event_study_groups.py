# -*- coding: utf-8 -*-
"""F60 事件研究的分组样本下限测试。

groups_min 以前只出现在签名里、从没参与筛选：1～2 个样本的分组也会算 t 检验，
"显著"只是噪声。这里锁住"样本不足的分组不进统计"。
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from trident2.features import trust


class _Ctx:
    def __init__(self, events):
        self._events = events

    def news(self, hours=24 * 7, **_kw):
        return self._events

    def kline_symbols(self):
        return {"XAU"}

    def q(self, sql, params, note=""):
        if "kline_bars" not in sql:
            return []
        start_ms = int(params[1])
        # 事件后 26 根 1h K 线，价格单调上行（收益与方向无关，只看是否被分组）
        return [(start_ms + i * 3_600_000, 100.0 + i) for i in range(26)]

    def evidence(self, **_kw):
        return {}


def _events(n_buy, n_sell):
    base_ts = 1_760_000_000
    ev = [{"asset": "XAU", "action": "BUY", "ts": base_ts + i * 100} for i in range(n_buy)]
    ev += [{"asset": "XAU", "action": "SELL", "ts": base_ts + 1000 + i * 100}
           for i in range(n_sell)]
    return ev


def test_groups_below_min_are_excluded():
    res = trust.event_study(_Ctx(_events(5, 1)), groups_min=3)
    assert res.status == "ok"
    actions = [g["action"] for g in res.value["groups"]]
    assert actions == ["BUY"]           # 只有 1 条样本的 SELL 组被挡在统计之外


def test_groups_min_one_keeps_every_group():
    res = trust.event_study(_Ctx(_events(5, 1)), groups_min=1)
    assert sorted(g["action"] for g in res.value["groups"]) == ["BUY", "SELL"]


def test_all_groups_too_small_returns_no_data():
    res = trust.event_study(_Ctx(_events(2, 1)), groups_min=5)
    assert res.status == "no_data"
    assert "分组样本不足" in res.headline
