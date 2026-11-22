# -*- coding: utf-8 -*-
"""meta_reflection_updater 的版本号递增测试。

背景：_next_version 以前完全不看 current_version，只按 ISO 周返回 v202+.wNN。
同一周自动跑两次（或手动 --apply 补跑），会往 META_REFLECTION.md 追加两个
标题完全一样的块，事后分不清哪块是哪一次。
"""
import datetime

import meta_reflection_updater as mru


def _base() -> str:
    week = datetime.datetime.now().isocalendar().week
    return "{}.w{}".format(mru.WEEKLY_VERSION_PREFIX, week)


def test_first_append_in_a_week_uses_plain_week():
    assert mru._next_version("") == _base()


def test_second_append_in_same_week_adds_suffix():
    assert mru._next_version(_base()) == _base() + ".2"


def test_third_append_keeps_counting():
    assert mru._next_version(_base() + ".2") == _base() + ".3"


def test_version_from_another_week_resets():
    other_week = (datetime.datetime.now().isocalendar().week % 52) + 1
    assert mru._next_version("{}.w{}".format(mru.WEEKLY_VERSION_PREFIX, other_week)) == _base()


def test_latest_version_reads_the_last_appended_block():
    content = (
        "## 当前盲区\n\n"
        "## 自动追加: v202+.w47 (2026-11-15)\n"
        "## 自动追加: v202+.w48 (2026-11-22)\n"
    )
    assert mru._latest_version(content) == "v202+.w48"
    assert mru._latest_version(None) == ""


def test_block_builder_signature_has_no_unused_old_summary():
    summary = {
        "week_start": "2026-11-16", "e020_total_week": 3,
        "new_kg_proposals": [], "unresolved_alerts": 0,
        "asset_action_hotspots": {},
    }
    block = mru._build_new_block([], _base(), summary)
    assert "## 自动追加: " in block
    assert "### 本周盲区扫描" in block
