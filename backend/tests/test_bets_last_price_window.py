# -*- coding: utf-8 -*-
"""bets._last_price 的时间窗测试。

背景：tol_days 参数以前没被使用，只取最新一笔 tick、不看它多旧 —— 一只票
停更两个月，浮亏会用两个月前的价格算成"没亏"。这里锁住"太旧就当作没行情"。
"""
import sqlite3
import time

import bets

DAY = 86400


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE market_ticks (symbol TEXT, price REAL, ts INTEGER)")
    return c


def _tick(c, symbol, price, age_days):
    c.execute("INSERT INTO market_ticks VALUES (?,?,?)",
              (symbol, price, int(time.time()) - int(age_days * DAY)))
    c.commit()


def test_stale_price_is_treated_as_missing():
    c = _conn()
    _tick(c, "BTC", 100.0, age_days=30)      # 一个月前的价
    assert bets._last_price(c, "BTC", tol_days=7) is None


def test_fresh_price_is_used():
    c = _conn()
    _tick(c, "BTC", 100.0, age_days=30)      # 旧价（更早）
    _tick(c, "BTC", 120.0, age_days=0.01)    # 刚更新（更近）
    assert bets._last_price(c, "BTC", tol_days=7) == 120.0


def test_tol_days_widens_the_window():
    c = _conn()
    _tick(c, "BTC", 88.0, age_days=30)
    assert bets._last_price(c, "BTC", tol_days=40) == 88.0
    assert bets._last_price(c, "BTC", tol_days=7) is None
