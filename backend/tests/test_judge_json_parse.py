# -*- coding: utf-8 -*-
"""判官输出解析（_parse_judge_json）的回归锁。

为什么单独写这一份：
  vulture 在 judge_worker.py 的 `except` 块里翻出一段
  "剥 ``` 包裹 + 找 {} 块 + json.loads" 的死代码 —— 它在 `return None`
  之后，永不执行。读到第一眼会以为"清洗逻辑被 return 挡住了，markdown 包裹
  会静默失败"。

  核对后发现不是：真正的清洗在函数开头（v3 之前那几行）已经做过一遍，
  死的那段是**旧实现的残留**（它还少补了 8 个兜底字段）。功能没丢，代码脏了。
  所以这里既删残留，也把"包裹能被解析"这件事锁成测试 —— 免得以后有人
  把开头的清洗误当作死代码再删一次，那时就真的会丢结果。

覆盖的形态都来自真实 LLM 输出：
  纯 JSON / ```json 包裹 / ``` 无语言标记 / 带 <think> 思考块 / 前后夹解释文字。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from judge_worker import _parse_judge_json  # noqa: E402

PAYLOAD = (
    '{"consistency": "ok", "consistency_reason": "方向与证据一致",'
    ' "asset_nature_correct": true, "key_logic_chain_broken": false,'
    ' "self_contradiction": "无", "skip_trade_rationale": "",'
    ' "severity": 2, "needs_human_review": true}'
)


def test_plain_json():
    out = _parse_judge_json(PAYLOAD, source="t-plain")
    assert out is not None
    assert out["severity"] == 2
    assert out["needs_human_review"] is True
    assert out["_source"] == "t-plain"


def test_fenced_json_block():
    """```json 包裹必须能被解析 —— 这是模型最常见的输出形态。"""
    out = _parse_judge_json("```json\n" + PAYLOAD + "\n```", source="t-fence")
    assert out is not None
    assert out["consistency"] == "ok"


def test_fenced_without_language():
    out = _parse_judge_json("```\n" + PAYLOAD + "\n```", source="t-fence2")
    assert out is not None
    assert out["severity"] == 2


def test_think_block_stripped():
    out = _parse_judge_json("好的，我先想一下。<think>内部推理</think>\n" + PAYLOAD, source="t-think")
    assert out is not None
    assert out["self_contradiction"] == "无"


def test_prose_around_object():
    """前后夹中文解释时，取第一个 { 到最后一个 }。"""
    out = _parse_judge_json("判定如下：\n" + PAYLOAD + "\n以上。", source="t-prose")
    assert out is not None
    assert out["key_logic_chain_broken"] is False


def test_missing_fields_get_defaults():
    """缺字段时补兜底值，而不是返回 None —— 判官少写一个键不该整条丢弃。"""
    out = _parse_judge_json('{"severity": 1}', source="t-defaults")
    assert out is not None
    assert out["consistency"] == "ok"
    assert out["asset_nature_correct"] is True
    assert out["self_contradiction"] == "无"


def test_garbage_returns_none():
    assert _parse_judge_json("完全不是 JSON", source="t-garbage") is None
    assert _parse_judge_json("", source="t-empty") is None


def test_non_dict_json_returns_none():
    """顶层是数组/字符串时返回 None，避免下游按字典取键炸掉。"""
    assert _parse_judge_json("[1, 2, 3]", source="t-list") is None
    assert _parse_judge_json('"just a string"', source="t-str") is None
