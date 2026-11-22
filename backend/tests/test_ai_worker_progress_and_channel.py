# -*- coding: utf-8 -*-
"""v536：ai_worker 里两个内嵌小函数的 characterization 测试。

_log_progress / _is_channel_failure 原来定义在 ai_worker 函数体里，没法单测。
提到模块级后行为必须一字不差 —— 前者是看门狗判定"引擎是否卡死"的唯一输入，
后者决定"通道故障的新闻要不要计重试次数"（计了就会被永久标 FAILED）。
"""
import json
import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as aw


@pytest.fixture(autouse=True)
def _restore_progress_state():
    """_PROGRESS_STATE 是模块级可变状态，测试后还原，避免污染其他用例。"""
    saved = dict(aw._PROGRESS_STATE)
    yield
    aw._PROGRESS_STATE.clear()
    aw._PROGRESS_STATE.update(saved)


def _progress_file(tmp_path):
    return tmp_path / "runtime" / "ai_worker_progress.json"


def test_is_channel_failure_is_case_insensitive():
    assert aw._is_channel_failure("HTTP 429 Too Many Requests")
    assert aw._is_channel_failure("所有通道熔断")
    assert aw._is_channel_failure("QUOTA exhausted")


def test_is_channel_failure_ignores_other_errors():
    assert not aw._is_channel_failure("database is locked")
    assert not aw._is_channel_failure(None)
    assert not aw._is_channel_failure("")


def test_log_progress_writes_step_and_extra(tmp_path, monkeypatch):
    monkeypatch.setattr(aw.config, "BASE_DIR", str(tmp_path))
    (tmp_path / "runtime").mkdir()

    aw._log_progress("signals", waited_sec=3)

    data = json.loads(_progress_file(tmp_path).read_text(encoding="utf-8"))
    assert data["step"] == "signals"
    assert data["waited_sec"] == 3
    assert data["step_ts"] > 0


def test_loop_step_refreshes_step_ts_every_round(tmp_path, monkeypatch):
    """空转（loop）必须每轮刷新 step_ts，否则看门狗会杀掉"闲着的引擎"。"""
    monkeypatch.setattr(aw.config, "BASE_DIR", str(tmp_path))
    (tmp_path / "runtime").mkdir()

    aw._log_progress("loop")
    first = json.loads(_progress_file(tmp_path).read_text(encoding="utf-8"))["step_ts"]

    aw._PROGRESS_STATE["step_ts"] = first - 3600   # 模拟一小时没刷新
    aw._log_progress("loop")
    again = json.loads(_progress_file(tmp_path).read_text(encoding="utf-8"))["step_ts"]
    assert again != first - 3600


def test_same_non_loop_step_keeps_step_ts(tmp_path, monkeypatch):
    """同一个非 loop step 连续写，不该反复刷新 step_ts（否则"卡住"永远看不出来）。"""
    monkeypatch.setattr(aw.config, "BASE_DIR", str(tmp_path))
    (tmp_path / "runtime").mkdir()

    aw._log_progress("persist")
    aw._PROGRESS_STATE["step_ts"] = 12345
    aw._log_progress("persist")
    data = json.loads(_progress_file(tmp_path).read_text(encoding="utf-8"))
    assert data["step_ts"] == 12345
