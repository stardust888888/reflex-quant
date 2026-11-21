"""Prompt 上下文块 —— 从 ai_worker.py 抽出的「读库 → 拼 prompt 一节」纯函数。

为什么单独成文件：
    ai_worker.py 是 11.7k 行的单体，这些函数彼此独立，也不碰 worker 的运行状态
    （熔断、信号量、连接池、批处理租约），却被埋在 3800–7200 行深处。挪出来之后：
      * 改 prompt 措辞的人只读这一个文件，不用在 1 万行里跳；
      * 每个块都能在测试里单独调用，不需要起 worker、不需要造批；
      * 运行状态与展示内容彻底分开 —— 前者会随周期变，后者只随话术变。

约定：
    * 本模块**不 import ai_worker**（否则循环依赖），只依赖 utils / worker_config / 标准库；
    * 每个函数一律返回 str，空串表示「这一节不放」，调用方不需要 try；
    * 库里读不到数据是一种正常情况，不是异常，日志用 print 带前缀，不抛。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .utils import _open_db
from .worker_config import (
    _PERF_ACTIONS,
    _PERF_ASSETS,
    _PERF_LOOKBACK_DAYS,
    _PERF_MIN_SAMPLE,
    _PERF_PREDICTION_TYPES,
)

def _build_microstructure_block(
    target_assets: Optional[List[str]] = None,
    max_chars: int = 800,
) -> str:
    """v200+ P0-1: 拼 microstructure block 给 LLM 看（注入 user_text 头部）。

    数据：coinglass_lsr.get_latest_samples_by_symbols + funding_rate_feed.fetch_funding_rates。
    在 _build_baseline_block 之后注入,与历史胜率并列 → 决策前先看：
      - 持仓量/资金费率(最大);
      - [market_structure_block] 中已有的市场风格(regime)、价格等"宏观背景"提示
      - 本块 = "微观结构"(散户/大户持仓 + 永续费率),从概率上校准本次方向
      - 不直接采纳其方向,但要求在 [逻辑自检] 和 [总结] 中显式引用 ≥ 1 条 microstructure 字段
      - 标记 stale=True 的数据视为「过期」, 引用时须注明「数据已滞后」

    失败/无样本 → 返回 "" (静默,主流程不阻塞)
    """
    if not target_assets:
        # 退化:用默认四件套
        target_assets = ["BTC", "ETH", "SOL", "XAU"]
    try:
        # 延迟 import 避免 engine 启动循环依赖
        import structured_input as _si
        micro = _si._microstructure_block(target_assets=target_assets)
    except Exception:
        return ""
    if not isinstance(micro, dict):
        return ""
    lsr = micro.get("lsr") or {}
    funding = micro.get("funding") or {}
    stale_syms = set(micro.get("stale_symbols") or [])
    if not lsr and not funding:
        return ""
    fresh_ts = int(micro.get("fresh_ts") or 0)
    fresh_str = ""
    if fresh_ts:
        try:
            from datetime import datetime as _dt
            fresh_str = _dt.utcfromtimestamp(fresh_ts).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            fresh_str = str(fresh_ts)
    header = ("═══ 微观结构（持仓量/资金费率,最新样本）═══\n"
              "以下数据用于校准本次方向的概率,不是直接采纳的信号源。\n"
              f"采样时间:{fresh_str or 'N/A'} | 数据源:{micro.get('source') or 'empty'}\n")
    lines: List[str] = []

    # ── LSR 部分(持仓量/多空比/大户持仓) ──
    for sym in sorted(lsr.keys()):
        s = lsr[sym]
        if not isinstance(s, dict):
            continue
        ratio_pct = str(s.get("ratio_pct") or "")
        d4 = str(s.get("delta_4h_pct") or "")
        d30 = str(s.get("delta_30m_pct") or "")
        long_q = float(s.get("ov_long_qty") or 0.0)
        short_q = float(s.get("ov_short_qty") or 0.0)
        whale_long = float(s.get("whale_long_qty") or 0.0)
        whale_short = float(s.get("whale_short_qty") or 0.0)
        oi_mcap = float(s.get("oi_mcap") or 0.0)
        cache_age = int(s.get("cache_age_s") or 0)
        stale_flag = " [数据已滞后]" if sym in stale_syms else ""
        # ratio_pct 通常形如 "55.20%" / "-3.14%" / "0" (无数据)
        line = (f"  - {sym}: 持仓比={ratio_pct or 'N/A'}, "
                f"4h 变化={d4 or 'N/A'}, 30m 变化={d30 or 'N/A'}, "
                f"多仓={long_q:.0f}/空仓={short_q:.0f}, "
                f"大户多={whale_long:.0f}/大户空={whale_short:.0f}, "
                f"OI/市值={oi_mcap:.3f}"
                f"{stale_flag} (cache_age={cache_age}s)")
        lines.append(line)
    # ── Funding 部分(永续费率) ──
    for sym in sorted(funding.keys()):
        f = funding[sym]
        if not isinstance(f, dict):
            continue
        rate_pct = float(f.get("funding_rate_pct") or 0.0)
        is_extreme = bool(f.get("is_extreme"))
        nft = f.get("next_funding_time")
        ext_flag = " ⚠极端" if is_extreme else ""
        nft_str = f" (下次结算:{nft})" if nft else ""
        sign = "+" if rate_pct >= 0 else ""
        lines.append(f"  - {sym}: 资金费率={sign}{rate_pct:.4f}%/8h{ext_flag}{nft_str}")
    # ── Funding Extreme 全局提醒 ──
    fext = micro.get("funding_extreme") or []
    if fext:
        for e in fext[:3]:
            if isinstance(e, dict):
                lines.append(
                    f"  ⚠ 极端费率提醒: {e.get('inst', 'N/A')} = "
                    f"{e.get('rate_pct', 0):+.4f}%/8h"
                )
    if not lines:
        return ""
    body = "\n".join(lines)
    footer = ("\n要求:在 [逻辑自检] 和 [总结] 中显式引用至少 1 条 microstructure 字段；\n"
              "  - 标记 [数据已滞后] 的字段不可作决策唯一依据,只能作风险提示；\n"
              "  - 资金费率 ≥ 0.2%/8h 即视为极端,市场情绪单边过热,慎追单边；\n"
              "  - 大户持仓 / 散户持仓背离超过 20% → 关注大户动作。\n")
    text = header + body + footer
    if len(text) > max_chars:
        text = text[: max_chars - 60] + "...\n(以上 microstructure 数据已截断,以原文表格为准)"
    return text


def _build_performance_context() -> str:
    """Build [Historical Performance] block for prompt injection.

    Queries settled ai_decisions for each asset × action × prediction_type
    combo.  Returns a concise multi-line string suitable for prepending to
    the LLM user prompt.  Runs once per batch (O(1) near-instant SQL).
    """
    try:
        conn = _open_db()
    except Exception:
        return ""

    try:
        lines: List[str] = []
        lines.append("[Historical Performance]")
        lines.append("Similar signals:")

        # Query: for each (asset, action, prediction_type) combo
        for asset in _PERF_ASSETS:
            for action in _PERF_ACTIONS:
                # ── Overall (all prediction_types) ──
                overall = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "  SUM(CASE WHEN is_correct = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                    "  SUM(CASE WHEN is_correct = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                    "  AVG(CASE WHEN forward_pnl IS NOT NULL THEN forward_pnl ELSE NULL END) AS avg_pnl "
                    "FROM ai_decisions "
                    "WHERE settled = 1 "
                    "  AND is_correct IN ('WIN', 'LOSS') "
                    "  AND target_asset = ? "
                    "  AND suggested_action = ? "
                    "  AND strftime('%s', created_at) >= strftime('%s', 'now', ?)",
                    (asset, action, f"-{_PERF_LOOKBACK_DAYS} days"),
                ).fetchone()

                for pt in _PERF_PREDICTION_TYPES:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, "
                        "  SUM(CASE WHEN is_correct = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                        "  SUM(CASE WHEN is_correct = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                        "  AVG(CASE WHEN forward_pnl IS NOT NULL THEN forward_pnl ELSE NULL END) AS avg_pnl "
                        "FROM ai_decisions "
                        "WHERE settled = 1 "
                        "  AND is_correct IN ('WIN', 'LOSS') "
                        "  AND target_asset = ? "
                        "  AND suggested_action = ? "
                        "  AND prediction_type = ? "
                        "  AND strftime('%s', created_at) >= strftime('%s', 'now', ?)",
                        (asset, action, pt, f"-{_PERF_LOOKBACK_DAYS} days"),
                    ).fetchone()

                    n = row["n"] or 0
                    wins = row["wins"] or 0
                    losses = row["losses"] or 0
                    decided = wins + losses
                    avg_pnl = row["avg_pnl"]

                    if n < _PERF_MIN_SAMPLE or decided == 0:
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"Insufficient sample"
                        )
                    else:
                        wr = wins / decided
                        pnl_str = (
                            f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A"
                        )
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"sample: {decided} "
                            f"win rate: {wr:.0%} "
                            f"avg pnl: {pnl_str}"
                        )

                # ── Also add the overall (all prediction_types) line ──
                n_all = overall["n"] or 0
                wins_all = overall["wins"] or 0
                losses_all = overall["losses"] or 0
                decided_all = wins_all + losses_all
                if decided_all >= _PERF_MIN_SAMPLE:
                    wr_all = wins_all / decided_all
                    pnl_all = overall["avg_pnl"]
                    pnl_str = (
                        f"{pnl_all:+.2f}%" if pnl_all is not None else "N/A"
                    )
                    lines.append(
                        f"{asset} * {action}: "
                        f"sample: {decided_all} "
                        f"win rate: {wr_all:.0%} "
                        f"avg pnl: {pnl_str}"
                    )

        return "\n".join(lines)

    except Exception as e:
        print(f"[PERF] 查询历史绩效失败: {type(e).__name__}: {str(e)[:80]}")
        return ""
    finally:
        conn.close()


def _load_attribution_block(asset: str, days: int = 30, top_n: int = 3) -> str:
    """v309 P0-3: 注入「该品种近期异动的历史归因」块。

    为什么需要 (这是审计里 P0-3 的核心缺口):
      归因系统 (event_move_attribution) 一直在算"哪条新闻解释了这波异动",
      但结论**从未回到 ai_worker 的决策上下文**。AI 每次分析同一条新闻都在
      凭印象重来, 完全不知道"上次这个品种这样异动时, 归因是什么"。

    注入的是**已经发生过的**历史归因 (move_ts 在过去), 用于给 AI 一个
    "这类事件在这个品种上历史通常怎么走" 的参照, 属于研究参考,
    不强制采纳 (与 baseline / microstructure 块的定位一致)。

    未来函数纪律:
      · 只取 move_ts < now 的行 (历史事实);
      · 剔除量纲污染行 (|move_pct| > 90%), 否则 +9867% 的假异动会进 prompt;
      · 只取有新闻来源的归因行 (ev_news_id / ev_source 非空), 否则是无信息行;
      · 表不存在或无数据 → 返回 "" (零开销, 不阻塞主流程)。
    """
    asset_u = str(asset or "").strip().upper()
    if not asset_u or asset_u in ("NONE", "OTHER", "N/A"):
        return ""
    conn = None
    try:
        import db as _db_attr
        conn = _db_attr.get_connection()
        # v308 P1-fix3 同款量纲过滤 —— 污染行绝不能进 prompt。
        #
        # v309 P0-3 修复: 之前 days 参数**形同虚设** —— 签名里有 days=30,
        # 但 SQL 只过滤 move_ts < now, 没有任何"最近 N 天"下界 →
        # 半年前的历史归因也会被注入 prompt (且 LIMIT 会取到最老的那些,
        # 因为 ORDER BY move_ts DESC + LIMIT 取的是最新的, 但 top_n*3 的窗口
        # 之外数据仍会挤占配额, 且 2024 年的旧新闻对当前毫无参考价值)。
        # 现在补上 move_ts >= now - days*86400 的下界, 让 days 真正生效。
        _now = int(time.time())
        _since = _now - int(max(1, days)) * 86400
        rows = conn.execute(
            "SELECT move_ts, move_pct, move_kind, algo_score, ev_source, "
            "       ev_action, ev_news_id "
            "FROM event_move_attribution "
            "WHERE asset = ? AND move_ts < ? AND move_ts >= ? "
            "  AND ABS(COALESCE(move_pct,0)) <= 90 "
            "  AND (ev_news_id IS NOT NULL OR COALESCE(ev_source,'') != '') "
            "ORDER BY move_ts DESC LIMIT ?",
            (asset_u, _now, _since, int(max(1, top_n) * 3)),
        ).fetchall()
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not rows:
        return ""

    # 按 move_ts 去重: 同一波异动可能有多条新闻归因, 只保留归因分最高的那条,
    # 否则一波行情会占满整个块。key=ts, value=(score, ts, pct, kind, src, act)
    best_by_move: Dict[int, Any] = {}
    for r in rows:
        try:
            ts = int(r[0]); pct = float(r[1] or 0.0); kind = r[2]
            score = float(r[3] or 0.0); src = r[4]; act = r[5]
        except (TypeError, ValueError, IndexError):
            continue
        if abs(pct) < 1.0:
            continue  # 幅度过小的"异动"没有参考价值
        prev = best_by_move.get(ts)
        if prev is None or score > prev[0]:
            best_by_move[ts] = (score, ts, pct, kind, src, act)

    items = sorted(best_by_move.values(), key=lambda x: x[0], reverse=True)[:int(max(1, top_n))]
    if not items:
        return ""

    lines = []
    for score, ts, pct, kind, src, act in items:
        src_s = str(src or "unknown")[:24]
        act_s = str(act or "?").upper()[:6]
        kind_s = str(kind or "?")[:16]
        try:
            ts_s = datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            ts_s = "?"
        lines.append(
            f"  - {ts_s} {pct:+.2f}% ({kind_s}) | 归因新闻方向 {act_s} | 来源 {src_s} | 归因分 {score:.2f}"
        )
    if not lines:
        return ""
    return (
        "═══ 历史归因参照（该品种近期异动由哪条新闻解释, 来自 event_move_attribution）═══\n"
        + "\n".join(lines)
        + "\n要求：以上是**历史已发生**的归因事实，用于校准「这类事件在这个品种上"
          "通常怎么传导」的参照；不得把它当作本次新闻的直接证据，也不得据此"
          "臆造未发生的新闻。\n"
    )


def _build_baseline_block(event_type_code: str, target_asset: str) -> str:
    """构造「历史绩效（baseline）」块：从 backtest_event_outcomes 表读历史 BUY/SELL/HOLD 样本。

    输入：event_type_code (例 'geopolitical_conflict'), target_asset (例 'XAU')
    输出：prompt 段文本。

    设计：
      - 优先取 (type_code, asset) 精确匹配的样本
      - 否则取同 type_code 不限 asset 的样本
      - 否则取全局样本
      - 样本 < 3 时显式标 "(样本不足,仅供参考)"
      - 表为空 / 数据库错 → 返回 "" (静默,主流程不阻塞)
    """
    if not event_type_code:
        return ""
    conn = None
    try:
        import db as _db
        conn = _db.get_connection() if hasattr(_db, "get_connection") else None
        if conn is None:
            return ""
        cur = conn.cursor()

        # 1) (type, asset) 精确匹配
        cur.execute(
            "SELECT sample_n, win_n, loss_n, avg_pnl_4h FROM backtest_event_outcomes "
            "WHERE type_code=? AND asset=? LIMIT 1",
            (event_type_code, target_asset or ""),
        )
        exact = cur.fetchone()

        # 2) 同 type 跨 asset (取样本数最大的非精确匹配组合)
        cur.execute(
            "SELECT asset, sample_n, win_n, loss_n, avg_pnl_4h FROM backtest_event_outcomes "
            "WHERE type_code=? AND asset != ? ORDER BY sample_n DESC LIMIT 1",
            (event_type_code, target_asset or ""),
        )
        same_type_other_asset = cur.fetchone()

        # 3) 全局汇总
        cur.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN win_n>0 THEN 1 ELSE 0 END) AS any_win, "
            "       SUM(loss_n) AS total_loss, SUM(sample_n) AS total_sample, "
            "       AVG(avg_pnl_4h) AS global_avg "
            "FROM backtest_event_outcomes"
        )
        global_row = cur.fetchone()
    except Exception as _e:
        print(f"[BASELINE] 查询失败(静默): {type(_e).__name__}: {_e}", flush=True)
        return ""
    finally:
        # v189-8: 原函数 2 条返回路径都不关连接, 每次调用泄漏 1 个 fd
        if conn is not None:
            conn.close()

    def _fmt(sample_n, win_n, loss_n, avg_pnl, label, asset=""):
        if sample_n <= 0:
            return None
        win_rate = (win_n / sample_n * 100.0) if sample_n > 0 else 0.0
        sample_flag = " [样本不足,仅供参考]" if sample_n < 3 else ""
        asset_part = f" / {asset}" if asset else ""
        return (f"  - {label}{asset_part}: n={sample_n}, 胜率={win_rate:.0f}%, "
                f"平均24h收益={avg_pnl:+.2f}%{sample_flag}")

    lines = []
    if exact:
        s, w, l, p = exact[0], exact[1], exact[2], exact[3] or 0.0
        line = _fmt(s, w, l, p, f"同事件同品种历史 (type={event_type_code}, asset={target_asset or 'N/A'})")
        if line:
            lines.append(line)

    if same_type_other_asset:
        s, w, l, p = same_type_other_asset[1], same_type_other_asset[2], same_type_other_asset[3], same_type_other_asset[4] or 0.0
        a = same_type_other_asset[0] or "(不限品种)"
        line = _fmt(s, w, l, p, f"同事件类型历史 (type={event_type_code})", asset=a)
        if line:
            lines.append(line)

    if global_row and global_row[3] and global_row[3] > 0:
        total_sample = global_row[3]
        total_loss = global_row[1] or 0  # any_win
        total_loss_n = global_row[2] or 0
        global_avg = global_row[4] or 0.0
        line = _fmt(total_sample, total_loss, total_loss_n, global_avg, "全事件类型历史汇总")
        if line:
            lines.append(line)

    if not lines:
        return ""

    body = "\n".join(lines)
    header = ("═══ 历史绩效（baseline.backtest_event_outcomes）═══\n"
              "以下数据来自历史 ai_decisions 在做出去向后的真实 PnL 回测（24h 窗口），"
              "用于校准本次分析的「同类事件过去是否赚钱」。\n")
    footer = ("\n要求：在 [因素排行] 和 [总结] 中显式引用上面至少 1 条历史数据，作为本次方向的"
              "概率校准依据（不要忽略样本不足警告）。\n")
    return header + body + footer


def _build_causal_block(news_content: str) -> str:
    """[v176.3 已停用] 构造 [因果链分析] 段要求（追加到 system prompt）。

    分析流程已删除 [因果链分析] 段，本函数不再被调用（因果链库数据/管理页仍保留）。
    无论是否命中因果链都要求 AI 在八段式之后输出 [因果链分析] 段，保证前端
    八段+因果链展示结构完整：
      - 命中启用因果链（触发词匹配）→ 注入链内容，AI 结合新闻解读因果
      - 未命中 → 要求输出"因：暂无匹配；果：暂无匹配结果"，仍保留该段结构
    无因果链库可用（异常）→ 返回 ""（不强制，不改变输出格式）。
    """
    try:
        from causal_chains import match_chains
        chains = match_chains(news_content, max_chains=3)
    except Exception:
        return ""
    if chains:
        lines = []
        for ch in chains:
            name = str(ch.get("name") or "").strip()
            cause = str(ch.get("cause") or "").strip()
            effect = str(ch.get("effect") or "").strip()
            note = str(ch.get("note") or "").strip()
            try:
                weight = float(ch.get("weight") or 1.0)
            except (TypeError, ValueError):
                weight = 1.0
            mechanism = str(ch.get("mechanism") or "").strip()
            precondition = str(ch.get("precondition") or "").strip()
            invalidation = str(ch.get("invalidation") or "").strip()
            head = f"[因果链 {name}]" if name else "[因果链]"
            line = f"{head} 因：{cause}；果：{effect}"
            if mechanism:
                line += f"（机理：{mechanism[:200]}）"
            if precondition:
                line += f"（前提：{precondition[:120]}）"
            if invalidation:
                line += f"（失效边界：{invalidation[:120]}）"
            if weight != 1.0:
                line += f"（权重 {weight:g}）"
            if note:
                line += f"（备注：{note}）"
            lines.append(line)
        return (
            "═══ 因果链分析（命中以下宏观因果链，必须在 reasoning_path 八段之后"
            "追加输出 [因果链分析] 段）═══\n"
            + "\n".join(lines)
            + "\n\n要求：\n"
            + "  1. 在八段式（[驱动力]…[逻辑自检]）之后，追加一段 [因果链分析]：结合本条"
              "新闻，用『因：…』说明当前事件对应的宏观/因果起点，用『果：…』推演其对"
              "黄金/原油/加密/美债等的传导方向与幅度。命中多条时按标注的（权重 N）"
              "权衡侧重——权重越高该链越是本条新闻的主导因果框架，分析要更侧重它。\n"
              "    ★多品种时因果链也要分品种：对主品种 + 每个 sibling 品种分别写"
              "『品种X：因…果…』（各自传导方向/幅度），禁止只写主品种。\n"
            + "  2. 对每条因→果，必须强制拆解三层并在 [因果链分析] 里写明："
              "①传导机理——A 为什么会造成 B，中间经过哪一层金融/物理机制；"
              "②生效前提——什么条件下这条链成立；③失效边界——什么场景下失效/反转"
              "（如金融危机时 TIPS 上行黄金未必跌）。命中的链未标注这三层时，"
              "AI 须按金融常识补出；说不出机理的关联一律不得当因果采信。\n"
            + "  3. 因果链仅作分析框架：若新闻与因果链方向矛盾或证据不足，明说并降级，"
              "不得机械套用。\n"
            + "  4. 若输出了 [因果链分析]，reasoning 结尾必须补一句『总结：』，"
              "多品种时总结也按品种分行——每个品种：因果链结论与八段式结论"
              "相互印证/存在分歧（说明采信哪边及原因）——两者互相认证。\n"
        )
    # 未命中任何因果链：仍要求输出该段（显示"暂无匹配"），保证结构完整
    return (
        "═══ 因果链分析（本条新闻未命中因果链库，仍需输出 [因果链分析] 段）═══\n\n"
        "要求：在八段式（[驱动力]…[逻辑自检]）之后，追加一段 [因果链分析]，"
        "内容写：『因：暂无匹配；果：暂无匹配结果』。若你能从宏观常识判断本条新闻"
        "有明确因果传导，也可简述（因：…；果：…），但不要编造复杂链条。\n"
        "多品种时若对各品种有不同因果判断，按品种分行简述（品种X：因…果…）。\n"
    )


def _build_gate_context(row) -> str:
    """从 raw_news.gate_* 列拼出 [门卫四步] 分析先验；缺列/无核心影响返回空串。"""
    def _g(col: str) -> str:
        try:
            return str(row[col] or "").strip()
        except Exception:
            return ""

    core = _g("gate_core_impact")
    verdict = _g("gate_verdict")
    if not core and not verdict:
        return ""
    main_asset = _g("gate_main_asset")
    kind = _g("gate_kind")
    timing = _g("gate_timing")
    strength = _g("gate_strength")
    if not core:
        core = "（门卫未给出核心影响，自行判断）"
    parts = [f"核心影响: {core}"]
    if main_asset:
        parts.append(f"主品种: {main_asset}")
    if kind:
        parts.append(f"性质: {kind}")
    if timing:
        parts.append(f"时效: {timing}")
    if strength:
        parts.append(f"强度: {strength}")
    if verdict:
        parts.append(f"判词: {verdict}")
    if main_asset and main_asset.upper() in ("BTC", "XAU", "WTI", "ETH", "SOL"):
        # v451：门卫已锁定池内主品种时，主分析不得再判 NONE/OTHER。
        # 实测 21:30 一批 8 条里"美、布两油走低""沃勒称可能加息50bp"全被判
        # 非核心 → routing_miss，LLM 明明拿到了 [门卫四步] 却没照用。
        parts.append(f"硬约束: target_asset 必须填 {main_asset}，禁止 NONE/OTHER")
    return "[门卫四步] " + "｜".join(parts)


def _load_insights_section() -> str:
    """复盘经验注入分区（结构化标题 + 逐条内容）。

    独立成区而非挤成一行 " | "：模型能逐条对照，且带样本数（可靠性依据）。
    硬铁律(H#)/软启发(EX#) 分两区，由 build_active_insights_section 输出。
    条数由 config.REVIEW_INSIGHTS_LIMIT 控制（默认不限 = 启用多少注入多少）。
    """
    try:
        from review.review_agent import build_active_insights_section, seed_analysis_experience
        try:
            seed_analysis_experience()  # 幂等播种硬铁律/软启发种子（首次分析写入）
        except Exception:
            pass
        # v415: 回流 A/B（批次级集群随机，默认 30% 对照）。
        # 对照组只撤软启发(kind=1)，硬铁律(kind=0)任何组都保留 —— 那是人工约束，不能拿去做实验。
        # 分组失败一律按 treatment（宁可多给经验，也不静默丢规则）。
        grp, key, bstart = "treatment", "", 0
        try:
            from reflection_ab import assign_group, log_assignment
            grp, key, bstart = assign_group()
        except Exception:
            grp, key = "treatment", ""
        text = (build_active_insights_section(include_soft=False)
                if grp == "control" else build_active_insights_section())
        if key and grp in ("treatment", "control"):
            try:
                import re as _re_ab
                ids = [int(x) for x in _re_ab.findall(r"\[EX#(\d+)", text)]
                log_assignment(key, grp, ids, bstart)
            except Exception:
                pass
        return text
    except Exception:
        return ""


def _build_event_block(news_content: str) -> str:
    """构造「事件结构化抽取」块：规则确定性抽出事件原子 + 要求 AI 交叉校验。

    事件原子（根因/量级/方向/标的）与 LLM 的七段式分析互补：
    规则负责把客观字段抽准，LLM 负责机理拆解。这里要求 AI 显式确认规则
    抽出的量级是否与新闻一致，不一致时在 [逻辑自检] 写『事件结构化校验』
    修正标记，供人按关键词复核（即"标黄"机制）。
    """
    try:
        from event_struct import (
            extract_data_points, extract_events, to_data_points_block, to_prompt_block,
        )
        events = extract_events(news_content or "")
        data_points = extract_data_points(news_content or "")
    except Exception:
        return ""
    block = to_prompt_block(events)
    dp_block = to_data_points_block(data_points)
    if not block and not dp_block:
        return ""
    body = block
    if dp_block:
        body = (body + "\n" + dp_block) if body else ("═══ 事件结构化抽取（数据三值锚点）═══\n" + dp_block)
    return (
        body
        + "\n要求：若上面规则抽出的事件/三值锚点与新闻一致，直接采纳其量级写进 [因素排行]/[驱动力]；"
        "若不一致（抽错事件/量级错误/遗漏关键事件/三值错位），必须在 [逻辑自检] 写"
        "『事件结构化校验：规则抽出X，实为Y』修正。任何方向性结论若依赖于被修正的量级，"
        "须以修正后的量级为准。\n"
    )


def _load_dim_event_type_block() -> str:
    """v174: 从 dim_event_type 表拉所有 active 的 type_code,生成「事件类型选项清单」注入 prompt。
    让 LLM 知道有这套维表,而不是自由输出 event_type 字符串。
    输出格式:
      【事件类型选项】(从 dim_event_type 选 1)
        - geopolitical_conflict (地缘冲突) → XAU/WTI 利多
        - rate_hike (加息) → BTC/ETH/WTI/XAU 利空
        ...
    """
    _conn = None
    try:
        import json as _json_det
        import db as _db_det
        _conn = _db_det.get_connection()
        # db.get_connection() 不设 row_factory, 这里用整数索引取值
        rows = _conn.execute(
            "SELECT code, name_zh, name_en, category, direction_bias FROM dim_event_type "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
        if not rows:
            return ""
        # v435 P0-2：清单从「一行一个 code + 中文名 + 英文名 + 分类 + 方向先验」压成
        # 紧凑表 —— 原格式 44 行约 2,718 字符，每条新闻都要发一遍（≈1.4k token）。
        # 实测（21:2x [PARTS] 诊断）它是 user 消息里最大的一块，比新闻正文还大；
        # 模型真正需要的只有 code + 中文名 + 方向先验，英文名/分类标签对选型没帮助。
        _compact = ["\n\n═══ 事件类型清单（必填 event_type_code，从下表严格选 1）═══",
                    "格式：code（中文名；历史方向先验，非硬规则）"]
        for r in rows:
            code_val = r[0]
            zh = str(r[1] or '').strip()
            dir_bias = r[4] or '{}'
            try:
                bias_dict = _json_det.loads(dir_bias) if str(dir_bias).strip().startswith('{') else {}
            except Exception:
                bias_dict = {}
            _tokens = []
            for k, v in list(bias_dict.items())[:4]:
                if isinstance(v, str):
                    _v = v.strip().lower()
                    if _v.startswith("bull") or _v in ("+", "up", "long"):
                        _tokens.append(f"{k}+")
                    elif _v.startswith("bear") or _v in ("-", "down", "short"):
                        _tokens.append(f"{k}-")
                elif isinstance(v, (int, float)):
                    _tokens.append(f"{k}{'+' if float(v) > 0 else '-'}")
            _inner = []
            if zh and zh != code_val:
                _inner.append(zh)
            if _tokens:
                _inner.append(",".join(_tokens))
            _compact.append(code_val + ("（" + "；".join(_inner) + "）" if _inner else ""))
        _compact.append('字段："event_type_code": "<上表 code 之一>"（都不贴合就选最近的并在 reasoning 说明）')
        return "\n".join(_compact)
    except Exception as _exc:
        # 失败兜底:返回最简清单让 LLM 知道有这个字段
        return ("\n\n═══ 事件类型选项清单(v174 必填)═══\n"
                "[ERROR] 加载 dim_event_type 失败, 请回退默认类型 (geopolitical_conflict/rate_cut/rate_hike/cpi/earnings_release 等)。\n"
                'JSON 必填新增字段: "event_type_code": "<code>"')
    finally:
        # v189-8: 该函数 3 条返回路径 (空表 / 正常 / 异常兜底) 原先都不关连接
        if _conn is not None:
            _conn.close()
