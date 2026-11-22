# -*- coding: utf-8 -*-
"""v536：_BatchPersistCtx 契约测试。

_batch_persist 原本靠 14 个闭包变量从 ai_worker 里取状态，无法单独调用。
显式打包成 dataclass 之后，字段完整性就是硬契约：以后谁在 _batch_persist 里
新增一个外层变量却忘了加字段，这里必须红，而不是等运行时 AttributeError。
"""
import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as aw

EXPECTED = {
    "successes", "batch", "content_map", "published_map", "timestamp_map",
    "model_pool", "gate_verdict_map", "gate_strength_map", "gate_impact_map",
    "market_context", "decision_context", "batch_end", "loop", "failures",
}


def test_ctx_has_exactly_the_expected_fields():
    assert set(aw._BatchPersistCtx.__dataclass_fields__) == EXPECTED


def test_ctx_keeps_every_value_by_identity():
    """必须是原对象透传，不能拷贝 —— 上层还在拿同一批 dict/list 做别的判断。"""
    kw = {k: object() for k in EXPECTED}
    ctx = aw._BatchPersistCtx(**kw)
    for k in EXPECTED:
        assert getattr(ctx, k) is kw[k]
