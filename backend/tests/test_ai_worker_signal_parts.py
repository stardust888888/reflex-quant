# -*- coding: utf-8 -*-
"""参考信号注入段的拼装测试（v536 从 ai_worker 主循环抽出）。

这段以前是主循环里的 7 个连续 if + 一个内嵌函数。抽出来之后行为必须一字不差：
prompt 里那一行 "[Reference Signals] ..." 变一个字，模型的输入就变了。
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as aw


def test_signal_lines_prefers_summary():
    items = [{"summary": "BTC 多头胜率 62%"},
             {"label": "XAU", "total": 8, "winrate": 0.5, "total_pnl": -1.2}]
    lines = aw._signal_lines(items)
    assert lines[0] == "BTC 多头胜率 62%"
    assert lines[1] == "XAU:8笔 胜率50% 盈亏-1.20%"


def test_signal_lines_caps_at_four_and_tolerates_missing_winrate():
    items = [{"label": "A", "total": 1, "winrate": None, "total_pnl": 0.5}] * 6
    lines = aw._signal_lines(items)
    assert len(lines) == 4
    assert lines[0] == "A:1笔 胜率? 盈亏+0.50%"


def test_reference_parts_empty_when_no_groups():
    assert aw._reference_signal_parts({}) == []


def test_reference_parts_keep_order_and_labels():
    sig = {"assets": [{"summary": "BTC 强"}], "combos": [{"summary": "BTC×多"}]}
    parts = aw._reference_signal_parts(sig)
    assert parts == ["品种 BTC 强", "品种×方向 BTC×多"]
    # 只为存在的组出文本，中间缺失的组不留空段
    assert len(parts) == 2


def test_reference_parts_full_section_shape():
    sig = {
        "assets": [{"summary": "BTC 胜率 60%"}],
        "directions": [{"summary": "多头偏强"}],
        "phases": [{"summary": "趋势段"}],
        "strengths": [{"summary": "强"}],
        "intervals": [{"summary": "开盘 30min"}],
        "combos": [{"summary": "BTC×多"}],
    }
    text = aw._SIGNAL_SECTION_PREFIX + " | ".join(aw._reference_signal_parts(sig))
    assert text.startswith("[Reference Signals] 历史统计信号")
    assert text.count(" | ") == 5                     # 6 组 → 5 个分隔
    assert "品种 BTC 胜率 60%" in text
    assert "品种×方向 BTC×多" in text
