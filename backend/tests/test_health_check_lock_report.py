# -*- coding: utf-8 -*-
"""health_check.lock_report 的读取窗口测试。

背景：lock_report 的 tail_lines 参数以前根本没用到，实现写死 400 行
（vulture 报"调用方传了、函数没用"）。这里锁住"参数真的决定读多少"——
写死的话下面第一条用例会失败。
"""
import os

import pytest

import health_check as hc

LOCKED = "2026-11-20 10:00:00 [ERROR] database is locked\n"
NORMAL = "2026-11-20 10:00:01 [INFO] market tick ok\n"


def _make_log(dirpath, head_lines, tail_lines):
    path = os.path.join(str(dirpath), "trident.log")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(LOCKED * head_lines)
        fh.write(NORMAL * tail_lines)
    return path


def test_tail_lines_limits_the_window(tmp_path, monkeypatch):
    """窗口只覆盖末尾时，前面的 locked 不该被算进来。"""
    monkeypatch.setattr(hc, "LOG_DIR", str(tmp_path))
    # 前面 5000 行全是报错，末尾 200 行是正常日志（约 8KB）
    _make_log(tmp_path, head_lines=5000, tail_lines=200)

    small = hc.lock_report(minutes=60, tail_lines=50)      # 窗口 6KB → 只到末尾
    assert small["locked_hits"] == 0

    full = hc.lock_report(minutes=60, tail_lines=10000)    # 窗口超过文件大小 → 全读
    assert full["locked_hits"] == 5000


def test_default_window_still_counts_recent_errors(tmp_path, monkeypatch):
    """默认 400 行的窗口行为不变：末尾的报错照常计入。"""
    monkeypatch.setattr(hc, "LOG_DIR", str(tmp_path))
    _make_log(tmp_path, head_lines=1000, tail_lines=0)  # 47KB，默认窗口 48KB 可全读

    res = hc.lock_report()  # 默认 tail_lines=400 → 48KB 窗口
    assert res["log_files"] == 1
    assert res["locked_hits"] == 1000
