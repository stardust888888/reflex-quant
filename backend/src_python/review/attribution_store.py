"""复盘归因（attribution）的落库、统计与反馈。

为什么从 review_agent.py 搬出来：归因这一组（写明细 / 统计 / 重置 / 回灌到洞察）
10 个函数 461 行，全部围绕 attribution_reviews 与一个进程内统计缓存转，不碰 LLM
调用，也不碰复盘主流程。review_agent 已经 4,846 行，再在里面改归因口径很容易
踩到洞察生成与 prompt 拼装；搬到独立模块后，归因口径只在一个文件里。

顺带把三个被这组依赖的同文件函数一起下沉（_now / _primary_code / save_insights），
否则新模块要反向 import review_agent，会形成循环导入。review_agent 侧 re-export，
既有调用方不需要改。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import db

# 自动调 kg_improvements.add_proposal 推一条 status=pending 候选，
# 供归因复核页 (段 4) 人工审批 → reconcile worker 异步应用到 causal_graph。
try:
    import kg_improvements
    _KG_IMP_OK = True
except Exception:  # pragma: no cover
    kg_improvements = None
    _KG_IMP_OK = False

_REASON_NAMES = {
    1: "数据缺失", 2: "非活跃时段", 3: "忽略关键因素", 4: "游戏规则变化",
    5: "不可监控信息", 6: "模型判断错误", 7: "信号质量差",
    8: "时间窗口不匹配", 9: "数据滞后", 10: "策略参数不匹配",
    11: "市场状态误判", 12: "新闻重复/旧闻", 13: "联动传导忽略",
    14: "关键价位/技术位", 15: "仓位/止损设置不当", 16: "入场时机不当",
    17: "模型推理降级",
    18: "数据错误", 19: "联动数据缺失/错误", 20: "成交滑点/成本",
    21: "系统通道故障", 22: "新闻已定价/抢跑", 23: "经验误用/过拟合",
    # v189 事件逻辑链层（最高权重；反思系统主要工作）
    24: "因果图谱推理链错位", 25: "概念错位/反共识缺位", 26: "跨资产传导方向错",
}


def _now() -> str:
    return datetime.now(config.TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _primary_code(corrected_code: Optional[int], reason_code: Optional[int],
                  reasons_json: str, *, code_min: int = 1, code_max: int = 23) -> Optional[int]:
    """取一条归因记录的【主因 code】（用于单选统计），保证 pct 加和 ≤ 100%。

    优先级：人工纠错 corrected_code > AI 多原因主因 (is_primary=true) >
    AI 多原因第一项 > reason_code。范围校验同 _validate_reason_code。
    返回 None 表示该记录「未归因」（主因提取失败，会被 stats 单列 code=0）。
    """
    if corrected_code is not None:
        v = _validate_reason_code(corrected_code, code_min, code_max)
        if v is not None:
            return v
    if reasons_json:
        try:
            arr = json.loads(reasons_json)
            if isinstance(arr, list):
                # 1) 先找 is_primary=true
                for it in arr:
                    if isinstance(it, dict) and it.get("is_primary"):
                        v = _validate_reason_code(
                            it.get("code", it.get("reason_code")), code_min, code_max
                        )
                        if v is not None:
                            return v
                # 2) 否则取第一项
                for it in arr:
                    if isinstance(it, dict):
                        v = _validate_reason_code(
                            it.get("code", it.get("reason_code")), code_min, code_max
                        )
                        if v is not None:
                            return v
        except (json.JSONDecodeError, TypeError):
            pass
    if reason_code is not None:
        return _validate_reason_code(reason_code, code_min, code_max)
    return None


def save_insights(conn: sqlite3.Connection, insights: List[str], source_samples: int = 0,
                  origin: str = "real") -> int:
    """经验规则入库（按文本 + 相似度去重，带来源样本数与提炼来源）。

    origin: 'real'=历史交易复盘 / 'quick_sim'=快速评测提炼 / 'rule'=归因规则

    v196: 解析 [XAU]/[BTC]/[WTI]/[ETH]/[*] 前缀作为 asset 字段；
          剥离前缀后写入 rule（避免与后续相似度去重互相干扰）。
          已知 asset 列表（从 _KNOWN_ASSET_CODES 取），前缀不匹配 → asset=''（通则）。
    """
    if origin not in ("real", "quick_sim", "rule"):
        origin = "real"
    added = 0
    existing = [row[0] for row in conn.execute(
        "SELECT rule FROM review_insights WHERE active = 1"
    ).fetchall()]
    for raw_rule in insights:
        if not raw_rule:
            continue
        # v196: 解析 asset 前缀
        asset_code, rule = _extract_asset_prefix(raw_rule)
        if not rule or rule in existing:
            continue
        # 相似度去重：与已有经验高度相似视为冗余，跳过
        if any(_similar(rule, old) > 0.62 for old in existing):
            continue
        cur = conn.execute(
            "INSERT INTO review_insights"
            " (rule, asset, scope, active, source_samples, origin)"
            " VALUES (?, ?, 'review', 1, ?, ?)",
            (rule, asset_code, source_samples, origin),
        )
        # v409: 生成即判定 —— 参数键/方向/窗口当场落库，不等 worker 巡检。
        try:
            import insight_hypothesis as _ih
            if cur.lastrowid:
                _ih.classify_one(conn, int(cur.lastrowid))
        except Exception as _ih_e:
            print(f"[review_agent] insight classify skipped: "
                  f"{type(_ih_e).__name__}: {_ih_e}", flush=True)
        existing.append(rule)
        added += 1
        # v189-7: per-row commit, 避免 N 条 insight 同一事务持锁 ~N×5ms.
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            # 单条 insight 入库失败 (lock) 不影响其他 insight.
            print(f"[review_agent.save_insights] skip (lock): {type(exc).__name__}: {str(exc)[:80]}")
    return added


def build_attribution_rows(trades: List[Dict[str, Any]], results: List[Dict[str, Any]],
                           match_id_key: str = "id", stored_id_key: str = "id",
                           result_id_key: str = "decision_id") -> List[Dict[str, Any]]:
    """把归因结果与原始交易对齐成落库行，供 save_attribution_details 使用。

    match_id_key：原始交易里用于匹配归因键的字段；stored_id_key：落库到 trade_id 的字段。
    （review_agent 复盘用 trade.id；history_review 用 decision_id 匹配、trade.id 落库。）
    """
    by = {r.get(result_id_key): r for r in results
          if isinstance(r, dict) and r.get(result_id_key) is not None}
    rows: List[Dict[str, Any]] = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        r = by.get(t.get(match_id_key))
        if not r:
            continue
        rows.append({
            "trade_id": t.get(stored_id_key),
            "asset": t.get("asset") or "",
            "action": t.get("action") or "",
            "verdict": t.get("is_correct") or t.get("verdict") or "",
            "core_impact": t.get("gate_core_impact") or "",
            "pnl_pct": t.get("forward_pnl") if t.get("forward_pnl") is not None else t.get("pnl_pct"),
            "reason_code": r.get("reason_code"),
            "reason_text": r.get("reason_text") or "",
            "confidence": r.get("confidence"),
            "reasons_json": json.dumps(r.get("reasons") or [], ensure_ascii=False),
            "failing_sources": r.get("failing_sources") or [],  # v222：失败源清单
        })
    return rows


def save_attribution_details(conn: sqlite3.Connection, source: str, ref_id: int,
                             rows: List[Dict[str, Any]]) -> int:
    """落库归因明细（同 source+ref_id 先清后写，幂等重跑不断增）。

    review 归因额外按 trade_id 去重：复盘 scope（week）的窗口天然重叠，同一笔
    失败交易会在多期 report 里重复出现，若不去重会随复盘次数累计漂移，导致
    FAILURE STATS 面板「复盘归因/失败总数」与库不一致。去重时把旧行的人工
    纠错字段继承到新行，避免清洗掉历史反馈（最终归因以人工纠错为准）。
    """
    conn.execute("DELETE FROM attribution_reviews WHERE source=? AND ref_id=?", (source, ref_id))
    n = 0
    # v176.7 P1-4: 在落库归因后，自动给「错」的归因推 kg_improvement 候选。
    # verdict 约定: 'correct' / 'wrong' / 'partial'（来自 LLM 总结的归因）
    # reason_code 是失败框架编号（1-24，含因果链错位 24）。
    # 这里只对 verdict=wrong 的行调用 add_proposal，避免 noise。
    kg_proposals_pushed = 0
    for r in rows:
        tid = r.get("trade_id")
        carried: Dict[str, Any] = {}
        if source == "review" and tid:
            old = conn.execute(
                "SELECT human_status, corrected_code, corrected_text, note, reviewed_at "
                "FROM attribution_reviews WHERE source='review' AND trade_id=? "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if old:
                carried = {
                    "human_status": old[0] if old[0] is not None else "pending",
                    "corrected_code": old[1],
                    "corrected_text": old[2] if old[2] is not None else "",
                    "note": old[3] if old[3] is not None else "",
                    "reviewed_at": old[4],
                }
                conn.execute(
                    "DELETE FROM attribution_reviews WHERE source='review' AND trade_id=?",
                    (tid,),
                )
        conn.execute(
            """INSERT INTO attribution_reviews
               (source, ref_id, trade_id, asset, action, verdict, core_impact, pnl_pct,
                reason_code, reason_text, confidence, reasons_json, failing_sources,
                human_status, corrected_code, corrected_text, note, reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, ref_id, tid, r.get("asset"), r.get("action"),
             str(r.get("verdict") or ""), r.get("core_impact") or "",
             r.get("pnl_pct"), r.get("reason_code"),
             r.get("reason_text"), r.get("confidence"),
             r.get("reasons_json") or "",
             json.dumps(r.get("failing_sources") or [], ensure_ascii=False),
             carried.get("human_status") or "pending",
             carried.get("corrected_code"),
             carried.get("corrected_text") or "",
             carried.get("note") or "",
             carried.get("reviewed_at")),
        )
        n += 1

        # v176.7 P1-4: 错归因自动推 kg_improvement 候选（去重：同 trade+reason_code 同 ref_id 不重复）。
        # verdict 字段实际取值: 'WIN'/'LOSS'/'HOLD'（review_agent._verdict_by_pnl）。
        # 仅 LOSS 触发；reason_code=24 因果链错位最优先，其它次之。
        if _KG_IMP_OK and str(r.get("verdict") or "").upper() == "LOSS":
            try:
                asset_v = (r.get("asset") or "").strip().upper()
                rc = str(r.get("reason_code") or "")
                target = f"trade:{tid} asset:{asset_v}" if tid else f"asset:{asset_v}"
                # 去重: 同 trade+reason_code 已 pending/approved 才不再推
                dup = conn.execute(
                    "SELECT id FROM kg_improvements WHERE target=? AND status IN ('pending','approved') LIMIT 1",
                    (target[:200],),
                ).fetchone()
                if not dup:
                    kind = "reweight" if rc in ("6", "24") else "add_edge"
                    payload = {
                        "trade_id": tid,
                        "asset": asset_v,
                        "action": r.get("action") or "",
                        "reason_code": rc,
                        "reason_text": (r.get("reason_text") or "")[:500],
                        "pnl_pct": r.get("pnl_pct"),
                        "source_attribution_ref": ref_id,
                    }
                    rs = kg_improvements.add_proposal(
                        kind=kind,
                        target=target[:200],
                        reason=f"归因错(ref={ref_id}, {rc}): {(r.get('reason_text') or '')[:300]}",
                        confidence=float(r.get("confidence") or 0.5),
                        sample_size=1,
                        impact=f"trade={tid} asset={asset_v}",
                        payload=payload,
                        source=f"review_agent:{source}",
                        conn=conn,
                    )
                    if rs.get("ok"):
                        kg_proposals_pushed += 1
            except Exception:
                pass  # 候选失败不影响归因主链路
    conn.commit()
    # 主链路完成后打印一条统计（方便观察推到多少）
    if kg_proposals_pushed:
        print(f"[KG-IMPROVE] pushed {kg_proposals_pushed} pending proposals from {source} ref={ref_id}", flush=True)
    return n


def clear_review_attributions(conn: sqlite3.Connection, scope: str,
                              except_ref_id: Optional[int] = None) -> int:
    """清理某一 scope 的历史复盘归因，只保留最新一次复盘（FAILURE STATS 数据同步）。

    ``attribution_reviews`` 对同 source+ref_id 幂等，但每次复盘 ref_id(=report_id)
    都不同，旧复盘归因不会自动清除，导致 review 归因随复盘次数累计漂移（
    前端显示与库不一致）。复盘前调用本函数清掉同 scope 旧 report 的归因。
    """
    sql = (
        "DELETE FROM attribution_reviews WHERE source='review' AND ref_id IN "
        "(SELECT id FROM review_reports WHERE scope=?)"
    )
    args: List[Any] = [scope]
    if except_ref_id is not None:
        sql += " AND ref_id != ?"
        args.append(int(except_ref_id))
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount


def list_attributions(status: str = "pending", limit: int = 200) -> List[Dict[str, Any]]:
    """按复核状态列归因明细（pending/agree/corrected/all）。

    v222：返回 dict 时自动解析 reasons_json / failing_sources 为 Python 对象，
    让复核面板同时展示主因 + 全部次因 + 失败源清单（不再吞掉）。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM attribution_reviews ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM attribution_reviews WHERE human_status=? ORDER BY id DESC LIMIT ?",
                (status, min(max(limit, 1), 500)),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            rj = d.get("reasons_json") or ""
            if rj:
                try:
                    d["reasons"] = json.loads(rj)
                except Exception:
                    d["reasons"] = []
            else:
                d["reasons"] = []
            fs = d.get("failing_sources") or ""
            if fs:
                try:
                    d["failing_sources"] = json.loads(fs)
                except Exception:
                    d["failing_sources"] = []
            else:
                d["failing_sources"] = []
            out.append(d)
        return out
    finally:
        conn.close()


# ── 归因统计缓存（前端 60s 轮询，避免每次全表扫描） ──
_STATS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


_STATS_CACHE_TTL_SEC = 30.0


_STATS_CACHE_KEY = "main"


def _invalidate_attribution_stats_cache() -> None:
    """写路径（reset/feedback/unreset）后调用，让下一次 stats 重算。"""
    _STATS_CACHE.pop(_STATS_CACHE_KEY, None)


def attribution_stats() -> Dict[str, Any]:
    """跨来源统一汇总失败归因（news 回溯 + review 复盘），展示哪类失败最多。

    最终归因码以人工纠错为准（corrected_code 优先，否则用 AI 初判 reason_code）。
    reset 状态 = 用户手动重置的"已归档"记录，不计入汇总（避免"怎么还显示 87"）。

    主因聚合（单选）：每条记录只取 1 个主因 ——
    人工纠错 > AI 多原因主因 (is_primary=true) > AI 多原因第一项 > reason_code，
    保证 pct 加和 ≤ 100%。多原因明细请见 per-trade 明细面板。

    未归因：主因提取失败的记录（脏数据 / 老库兼容），独立列 code=0 让用户看见缺口。
    """
    now_ts = time.time()
    cached = _STATS_CACHE.get(_STATS_CACHE_KEY)
    if cached and now_ts - cached[0] < _STATS_CACHE_TTL_SEC:
        return cached[1]

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM attribution_reviews WHERE human_status != 'reset'"
        ).fetchone()[0]
        reset_count = conn.execute(
            "SELECT COUNT(*) FROM attribution_reviews WHERE human_status = 'reset'"
        ).fetchone()[0]
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM attribution_reviews "
            "WHERE human_status != 'reset' GROUP BY source"
        ).fetchall()
        status_rows = conn.execute(
            "SELECT human_status, COUNT(*) AS n FROM attribution_reviews "
            "WHERE human_status != 'reset' GROUP BY human_status"
        ).fetchall()
        rows = conn.execute(
            "SELECT corrected_code, reason_code, reasons_json FROM attribution_reviews "
            "WHERE human_status != 'reset'"
        ).fetchall()
        counter: Dict[int, int] = {}
        unattr = 0
        for r in rows:
            code = _primary_code(r["corrected_code"], r["reason_code"], r["reasons_json"])
            if code is None:
                unattr += 1
                continue
            counter[code] = counter.get(code, 0) + 1
        reasons: List[Dict[str, Any]] = []
        for code, n in sorted(counter.items(), key=lambda kv: -kv[1]):
            reasons.append({
                "code": code,
                "name": _REASON_NAMES.get(code, f"未知({code})"),
                "count": n,
                "pct": round(n / total * 100, 1) if total else 0.0,
            })
        if unattr > 0:
            reasons.append({
                "code": 0,
                "name": "未归因/数据缺失",
                "count": unattr,
                "pct": round(unattr / total * 100, 1) if total else 0.0,
            })
            reasons.sort(key=lambda x: -x["count"])
    finally:
        conn.close()
    out = {
        "total": total,
        "sources": {str(r["source"]): int(r["n"]) for r in source_rows},
        "statuses": {str(r["human_status"]): int(r["n"]) for r in status_rows},
        "reasons": reasons,
        # 让前端展示更直观（sources/news + sources/review 拼起来 = total）
        "news_count": int(next((r["n"] for r in source_rows if r["source"] == "news"), 0)),
        "review_count": int(next((r["n"] for r in source_rows if r["source"] == "review"), 0)),
        "unattributed": unattr,
        "reset_count": int(reset_count),
        "cache_age_sec": 0.0,
    }
    _STATS_CACHE[_STATS_CACHE_KEY] = (now_ts, out)
    return out


def reset_attributions(scope: str = "all", days: int = 0) -> Dict[str, Any]:
    """重置失败归因（软删除 = 把 human_status 改为 'reset'，数据可恢复）。

    防止用户看到大量 pending 归因而焦虑；保留数据避免破坏归因上下文。
    scope:
      - all: 全部 source 的 pending 归因
      - news: 仅新闻回溯
      - review: 仅复盘归因
    days: 仅重置 N 天前的记录（0=全部）
    """
    if scope not in ("all", "news", "review"):
        return {"ok": False, "error": f"invalid scope: {scope}"}
    conn = db.get_connection()
    try:
        if scope == "all":
            where_src = "1=1"
        else:
            where_src = f"source=?"
            # 在 params 里动态传 source
        where = f"human_status='pending' AND {where_src}"
        params: List[Any] = []
        if scope != "all":
            params.append(scope)
        if days and days > 0:
            where += f" AND created_at < datetime('now', ?)"
            params.append(f"-{int(days)} days")
        # 先数影响行数（要可观测）
        cur = conn.execute(f"SELECT COUNT(*) FROM attribution_reviews WHERE {where}", tuple(params))
        before = cur.fetchone()[0]
        # 软删除
        update_params: List[Any] = list(params)
        cur = conn.execute(
            f"UPDATE attribution_reviews SET human_status='reset', reviewed_at=datetime('now') WHERE {where}",
            tuple(update_params),
        )
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    # 写路径：下次 stats 必须重算
    _invalidate_attribution_stats_cache()
    # 重读 stats
    return {
        "ok": True,
        "scope": scope,
        "days": days,
        "before": int(before),
        "reset": int(deleted),
        "stats": attribution_stats(),
    }


def unreset_attributions(scope: str = "all", days: int = 0) -> Dict[str, Any]:
    """撤销重置（reset → pending），重新放入复核队列。

    与 reset_attributions 反向。适用「重置后想恢复部分归因继续复核」。
    仅改 human_status，不动其他字段；reviewed_at 保留为原重置时间，便于审计。
    scope: all / news / review（与 reset_attributions 同语义）
    days: 仅撤销 N 天前的记录（0=全部）
    """
    if scope not in ("all", "news", "review"):
        return {"ok": False, "error": f"invalid scope: {scope}"}
    conn = db.get_connection()
    try:
        where = "human_status='reset'"
        params: List[Any] = []
        if scope != "all":
            where += " AND source=?"
            params.append(scope)
        if days and days > 0:
            where += " AND created_at < datetime('now', ?)"
            params.append(f"-{int(days)} days")
        # 先数影响行数
        cur = conn.execute(
            f"SELECT COUNT(*) FROM attribution_reviews WHERE {where}", tuple(params)
        )
        before = cur.fetchone()[0]
        conn.execute(
            f"UPDATE attribution_reviews SET human_status='pending' WHERE {where}",
            tuple(params),
        )
        conn.commit()
    finally:
        conn.close()
    _invalidate_attribution_stats_cache()
    return {
        "ok": True,
        "scope": scope,
        "days": days,
        "before": int(before),
        "unreset": int(before),
        "stats": attribution_stats(),
    }


def gate_attribution_stats() -> Dict[str, Any]:
    """按门卫主线（core_impact 这条筋）聚合归因明细，回看每条筋的实际兑现。

    core_impact 文本本身已含主资产×性质，聚合它即得到"哪条筋胜率高/总亏/主要败因"。
    归因码取人工纠错优先（corrected_code），否则 AI 初判 reason_code。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT core_impact, verdict, pnl_pct, "
            "COALESCE(corrected_code, reason_code) AS code "
            "FROM attribution_reviews WHERE core_impact <> ''"
        ).fetchall()
        buckets: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            line = str(r["core_impact"]).strip()
            key = line[:60]
            b = buckets.setdefault(
                key, {"main_line": line, "n": 0, "win": 0, "loss": 0, "pnl": 0.0, "codes": {}}
            )
            b["n"] += 1
            v = str(r["verdict"] or "").upper()
            if v == "WIN":
                b["win"] += 1
            elif v == "LOSS":
                b["loss"] += 1
            pnl = r["pnl_pct"]
            if isinstance(pnl, (int, float)):
                b["pnl"] += float(pnl)
            if r["code"] is not None:
                c = int(r["code"])
                b["codes"][c] = b["codes"].get(c, 0) + 1
        lines = []
        for b in sorted(buckets.values(), key=lambda x: -x["n"]):
            decided = b["win"] + b["loss"]
            top_code = max(b["codes"], key=b["codes"].get) if b["codes"] else None
            lines.append({
                "main_line": b["main_line"],
                "n": b["n"],
                "winrate": round(b["win"] / decided * 100, 1) if decided else None,
                "net_pnl": round(b["pnl"], 2),
                "top_reason_code": top_code,
                "top_reason_name": _REASON_NAMES.get(top_code, "") if top_code else "",
            })
    finally:
        conn.close()
    return {"lines": lines}


def apply_attribution_feedback(conn: sqlite3.Connection, attribution_id: int, status: str,
                               corrected_code: Optional[int] = None,
                               corrected_text: str = "", note: str = "") -> Dict[str, Any]:
    """人工复核/纠错一条归因。纠错且 code 不同则自动回灌经验规则（rule_pushed 防重复）。"""
    row = None
    prev_rf = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM attribution_reviews WHERE id=?", (int(attribution_id),)
        ).fetchone()
    finally:
        conn.row_factory = prev_rf
    if row is None:
        return {"ok": False, "error": "attribution not found"}
    row = dict(row)
    now = _now()
    conn.execute(
        """UPDATE attribution_reviews
           SET human_status=?, reviewed_at=?, corrected_code=?, corrected_text=?, note=?
           WHERE id=?""",
        (status, now, corrected_code, corrected_text or "", note or "", int(attribution_id)),
    )
    pushed = False
    rule_text = ""
    if status == "corrected" and corrected_code is not None:
        old_code = row.get("reason_code")
        try:
            code_changed = int(corrected_code) != int(old_code or -1)
        except (TypeError, ValueError):
            code_changed = False
        if code_changed and not row.get("rule_pushed"):
            old_name = _REASON_NAMES.get(int(old_code or 0), "未知")
            new_name = _REASON_NAMES.get(int(corrected_code), "未知")
            ctx = f"{row.get('asset') or ''} {row.get('action') or ''} {row.get('verdict') or ''}".strip()
            rule_text = f"归因纠错：{ctx} 原判「{old_name}」应判「{new_name}」"
            if note:
                rule_text += f"（{note}）"
            save_insights(conn, [rule_text], source_samples=1, origin="rule")
            conn.execute(
                "UPDATE attribution_reviews SET rule_pushed=1 WHERE id=?", (int(attribution_id),)
            )
            pushed = True
    conn.commit()
    # 写路径：人工纠错会改变 corrected_code，下次 stats 必须重算
    _invalidate_attribution_stats_cache()
    return {"ok": True, "pushed_rule": pushed, "rule": rule_text}


def _validate_reason_code(v: Any, code_min: int = 1, code_max: int = 23) -> Optional[int]:
    """把任意值安全收敛到合法失败原因 code（1-23），非法值返回 None。

    为什么必须：LLM 偶尔会输出 _REASON_NAMES 范围外的 code（如 24、0、-1），
    若透传会导致归因榜上出现「未知(N)」占榜首。失败原因定义见 _FAILURE_FRAMEWORK。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if code_min <= n <= code_max:
        return n
    return None


def _similar(rule: str, existing: str) -> float:
    """经验规则文本相似度（0-1），用于防堆积去重。"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, rule, existing).ratio()


def _extract_asset_prefix(text: str) -> Tuple[str, str]:
    """v196: 解析 ' [XAU] XAU 在 ...' 前缀。

    匹配：[XAU]、[BTC]、[ETH]、[WTI]、[SOL]、[CN_STOCK]、[US_STOCK]、
          [INDIVIDUAL_STOCK]、[SKHYNIX]、[*]（*=通则显式标注）。
    命中 → 返回 (asset_code, 剥离前缀后的 rule)。
    未命中 → 返回 ('', text) (向后兼容)。
    """
    if not text:
        return "", ""
    s = text.strip()
    # 检测形如 "[XXX] ..." 的前缀（XXX 是已知 asset code 或 *）
    m = re.match(r"^\[(\*|[A-Z_]+)\]\s+", s)
    if not m:
        return "", text
    code = m.group(1)
    if code == "*":
        return "", s[m.end():]
    if code in _KNOWN_ASSET_CODES:
        return code, s[m.end():]
    # [OTHERS] 这类不认识的 → 视为通则（保留原文，不剥离）
    return "", text


_KNOWN_ASSET_CODES = frozenset({"XAU", "BTC", "ETH", "WTI", "SOL",
                                "CN_STOCK", "US_STOCK", "INDIVIDUAL_STOCK",
                                "SKHYNIX"})
