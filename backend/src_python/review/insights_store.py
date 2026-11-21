"""复盘洞察（insights）的存取、筛选与 prompt 拼装。

为什么从 review_agent.py 搬出来：review_agent.py 5,318 行、100 个顶层函数，
其中"洞察的增删改查 + 排序 + 拼成 prompt 一节"这 13 个函数彼此闭环、只依赖
db 与几个常量，不碰 LLM 调用与复盘主流程，是这份文件里最干净的一块。
搬出来后改洞察规则只看一个 500 行的文件，也不必在五千行里翻。

依赖方向：本模块只读 db / config，不回指 review_agent —— 因此不会循环导入。
review_agent 侧用 re-export 保持既有取用路径不变。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import json
import re
import sqlite3

import config
import db


# ── 分析经验种子：硬铁律（kind=0，不可违背、不参与胜率停用）+ 软启发（kind=1，可证伪） ──
ANALYSIS_EXPERIENCE_SEEDS: List[Dict[str, Any]] = [
    # 硬铁律 —— 边界公理，管稳定
    {"kind": 0, "scope": "all", "rule": "只依据真实、可追溯的数据源；数据缺失标 N/A，绝不臆造/编造数据。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "官员/机构「发言表态」是即时事件催化，不是媒体「事后统计」；不得把表态误判为事后统计性消息。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "事后统计/评估性消息大概率已定价（新闻已抢跑），不据此直接给方向。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "八段推演与 20 字段结构不可省略；每个结论必须 score 锚定，asset_impact 打分必须带事实依据。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "方向不确定时给 HOLD 并说明理由，绝不因「要出结论」而强行买卖。", "counter_factual": ""},
    # 软启发 —— 可证伪假说（人工维护种子，结算后按真实胜率升/降权或停用）
    {"kind": 1, "scope": "review", "rule": "避险叙事下黄金走强概率高，但资金坐实前不追多。", "counter_factual": "若未见真实买盘/持仓增持续证明资金流入，则不追多"},
    {"kind": 1, "scope": "review", "rule": "突发供应中断（炼厂爆炸/停产）通常短线利多油价，但价格已抢跑时追多赔率差。", "counter_factual": "若价格已因该事件大幅拉升，追多赔率差，反而防回落"},
    {"kind": 1, "scope": "review", "rule": "官员表态引发的短期波动，需匹配真实资金流才可跟随。", "counter_factual": "若仅表态、无资金流/持仓配合，则不作方向依据"},
]


def seed_analysis_experience(conn: Optional[sqlite3.Connection] = None) -> int:
    """幂等播种硬铁律/软启发种子：相同 rule 已存在则不重复插入（不删除/重置用户已有经验）。

    硬铁律 kind=0 不参与胜率停用；软启发 origin='seeded'（人工维护，结算后验证）。
    """
    own = conn is None
    conn = conn or db.get_connection()
    added = 0
    try:
        for seed in ANALYSIS_EXPERIENCE_SEEDS:
            rule = seed["rule"]
            if conn.execute("SELECT 1 FROM review_insights WHERE rule=?", (rule,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO review_insights"
                " (rule, asset, scope, active, source_samples, origin, kind, counter_factual)"
                " VALUES (?, '', ?, 1, 0, 'seeded', ?, ?)",
                (rule, seed["scope"], seed["kind"], seed["counter_factual"]),
            )
            added += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return added


def build_active_insights_section(
    conn: Optional[sqlite3.Connection] = None,
    include_soft: bool = True,
) -> str:
    """生效经验的注入分区（结构化标题 + 逐条内容），供拼进 AI 分析的 prompt。

    为什么单独成区：此前是把经验用 " | " 挤成一行塞进 user prompt，条数一多就成
    一坨、模型难以逐条对照，且样本数（判断可靠性的唯一依据）在拼接时丢失。
    这里改为与「品种专项规则」同风格的独立分区：标题 + 使用方式 + 逐条编号。

    优先级：本分区是「经验层」，低于通用规则与品种专项规则 —— 分区内已写明，
    避免经验与用户设定的规则互相打架。使用方式里也强调不得因引用经验而省略
    八段结构 / 20 字段 / score 锚定（防结果漂移）。

    条数由 config.REVIEW_INSIGHTS_LIMIT 控制（默认 0 = 不限，启用多少注入多少）；
    无生效经验 → 返回 ""（不注入、零开销）。
    """
    limit = max(0, int(getattr(config, "REVIEW_INSIGHTS_LIMIT", 0) or 0))
    min_samples = max(1, int(getattr(config, "REVIEW_INSIGHTS_MIN_SAMPLES", 5) or 5))
    # limit=0 表示不限 —— SQL 的 LIMIT 0 会变成"什么都不取"，故给个足够大的值
    sql_limit = limit if limit > 0 else 100000
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        # 硬铁律：kind=0，强制约束，不受样本门槛，不参与胜率停用
        hard_rows = conn.execute(
            "SELECT id, rule FROM review_insights"
            " WHERE active = 1 AND kind = 0 ORDER BY id"
        ).fetchall()
        # 软启发：人工种子(origin='seeded')始终注入；系统自动沉淀需满足样本门槛
        # v413: 不可测经验（observe_only=1）不注入 —— 它们被 insight_hypothesis
        # 判定为"解不成可测假设"，无法验证也无法自我纠错。硬铁律(kind=0)不受影响：
        # 那是人写下的约束，本来就不是"待验证的假设"。
        # 环境变量 INSIGHTS_INJECT_UNMEASURABLE=1 可恢复旧行为。
        # v415: include_soft=False 供回流 A/B 的对照组使用 —— 只保留硬铁律，
        # 软启发整段撤掉。硬铁律是人写的强制约束，任何组都不允许被实验关掉。
        _allow_obs = __import__("os").environ.get("INSIGHTS_INJECT_UNMEASURABLE", "0") == "1"
        soft_rows = []
        if include_soft:
            soft_rows = conn.execute(
            "SELECT id, rule, COALESCE(origin, 'real') AS origin, source_samples,"
            " COALESCE(counter_factual, '') AS counter_factual,"
            " COALESCE(asset, '') AS asset,"
            " verification_verdict, verification_sample_n, insight_param_key, insight_direction,"
            " COALESCE(insight_param_bucket, '') AS insight_param_bucket"
            " FROM review_insights"
            " WHERE active = 1 AND kind = 1 AND (origin = 'seeded'"
            "   OR verification_verdict IN ('verified', 'partial')"
            "   OR (CASE WHEN COALESCE(source_samples, 0) >= COALESCE(verification_sample_n, 0)"
            "           THEN COALESCE(source_samples, 0) ELSE COALESCE(verification_sample_n, 0) END) >= ?)"
            "   AND (? = 1 OR COALESCE(observe_only, 0) = 0)"
            " ORDER BY source_samples DESC, id DESC LIMIT ?",
                (min_samples, 1 if _allow_obs else 0, sql_limit),
            ).fetchall()
            if getattr(config, "REVIEW_INSIGHTS_RANK", 1):
                # v453: 这条才是真正进 prompt 的路径 —— 旧排序只看 source_samples，
                # 唯一 verified 的洞察被样本数更大的挤掉，且同一参数键上"提高/降低"
                # 两条会一起进来。先按证据强度排序并做同键消解，再做标签多样性截断。
                _items = [{
                    "id": int(r["id"]),
                    "rule": str(r["rule"] or ""),
                    "origin": str(r["origin"] or "real"),
                    "source_samples": max(int(r["source_samples"] or 0),
                                          int(r["verification_sample_n"] or 0)),
                    "verdict": str(r["verification_verdict"] or ""),
                    "param_key": str(r["insight_param_key"] or ""),
                    "bucket": str(r["insight_param_bucket"] or ""),
                    "asset": str(r["asset"] or ""),
                    "direction": int(r["insight_direction"] or 0),
                    "observe_only": 0,
                    "counter_factual": str(r["counter_factual"] or ""),
                } for r in soft_rows]
                _ranked = rank_insight_candidates(_items, 0)
                soft_rows = _ranked      # dict 与 sqlite3.Row 字段同名，下游按 r["x"] 取用
        if not hard_rows and not soft_rows:
            return ""
        lines: List[str] = []
        if hard_rows:
            lines.append("═══ 硬铁律（不可违背 · 必守）═══")
            lines.append("以下为系统强制约束，任何情况下不得违背；不参与命中/胜率统计。")
            for i, r in enumerate(hard_rows, 1):
                lines.append(f"H{i}. {r['rule']}")
            lines.append("")
        if soft_rows:
            origin_label = {"real": "历史交易", "quick_sim": "快速评测", "rule": "归因规则", "seeded": "人工维护"}
            lines.append("═══ 软启发（统计倾向 · 可被反证）═══")
            lines.append("以下为可证伪的经验倾向，与本次新闻事实冲突时以新闻事实为准。")
            lines.append("真正采纳了哪条，就在输出 JSON 的 insight_hit_ids 里填它的 [EX#id]（没采纳填 []）。")
            _cf_seen: Dict[str, int] = {}
            # v442：按「标签」做多样性截断。
            # 实测 80 条生效软启发里，[sizing.position_weight] 10 条、[sizing.take_profit_pct] 10 条……
            # 同一参数反复出现，把整个分区撑到 2,900 字符，而分析模型要的是"判断这类新闻"
            # 而不是"某个参数该设多少"。同类最多留 3 条（已按样本数排序，留下的就是样本最多的），
            # 总条数与总字符都有硬上限；硬铁律不受影响。
            _SOFT_PER_TAG = int(getattr(config, "REVIEW_INSIGHTS_PER_TAG", 3) or 3)
            _SOFT_MAX_CHARS = int(getattr(config, "REVIEW_INSIGHTS_MAX_CHARS", 1400) or 1400)
            _tag_count: Dict[str, int] = {}
            _kept = []
            _chars = 0
            for _r in soft_rows:
                _t = str(_r["rule"] or "")
                _tag = _t[: _t.find("]") + 1] if "]" in _t[:24] else _t[:12]
                if _tag_count.get(_tag, 0) >= _SOFT_PER_TAG:
                    continue
                if _chars + len(_t) > _SOFT_MAX_CHARS:
                    continue
                _tag_count[_tag] = _tag_count.get(_tag, 0) + 1
                _chars += len(_t)
                _kept.append(_r)
            soft_rows = _kept
            for i, r in enumerate(soft_rows, 1):
                label = origin_label.get(str(r["origin"] or "real"), "历史交易")
                n = int(r["source_samples"] or 0)
                tag = f"{n} 样本" if n >= min_samples else "种子/待验证"
                # v453: 注入时标明适用范围 —— 经验库里有 XAU/WTI 专用条目，
                # 之前混在通用里一起喂，分析别的品种时等于噪声。标注比过滤稳：
                # 该用的时候模型自己会用，不该用的看得见也会跳过。
                _asset = str(r.get("asset") if isinstance(r, dict) else r["asset"] or "").strip().upper()
                _scope = f" · {_asset} 专用" if _asset and _asset not in ("", "NONE") else ""
                base = f"{i}. [EX#{r['id']} · {label} · {tag}{_scope}] {r['rule']}"
                cf = clean_counter_factual(r["counter_factual"])
                lines.append(base)
                if cf:
                    # 同一批经验的反例往往是同一句（都来自同一批事故码），重复注入纯浪费
                    if cf in _cf_seen:
                        lines.append(f"   反例同第 {_cf_seen[cf]} 条")
                    else:
                        _cf_seen[cf] = i
                        lines.append(f"   反例：{cf}")
        return "\n".join(lines)
    except Exception:
        return ""
    finally:
        if own:
            conn.close()


def load_active_insights(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 0,
    min_samples: int = 0,
) -> List[str]:
    """加载生效经验规则（供 AI 分析 prompt 回灌）。

    默认 limit=0（不限）/ min_samples=0（用配置门槛）—— 统一由 config 的
    REVIEW_INSIGHTS_LIMIT / REVIEW_INSIGHTS_MIN_SAMPLES 控制，避免"函数默认值"
    与"配置项"两处口径打架。显式传参可覆盖（测试用）。
    """
    eff_limit = limit if limit > 0 else max(0, int(getattr(config, "REVIEW_INSIGHTS_LIMIT", 0) or 0))
    eff_min = min_samples if min_samples > 0 else max(
        1, int(getattr(config, "REVIEW_INSIGHTS_MIN_SAMPLES", 5) or 5)
    )
    return [item["rule"] for item in load_active_insight_items(conn, eff_limit, eff_min)]


def load_active_insight_items(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 0,
    min_samples: int = 0,
) -> List[Dict[str, Any]]:
    """加载生效经验（含 id 与来源 origin），供注入 prompt 与记录命中（insight_hit_ids）。

    limit=0 = 不限（受 config.REVIEW_INSIGHTS_LIMIT 约束，默认也是不限）；
    min_samples=0 = 用 config.REVIEW_INSIGHTS_MIN_SAMPLES。
    """
    eff_limit = limit if limit > 0 else max(0, int(getattr(config, "REVIEW_INSIGHTS_LIMIT", 0) or 0))
    sql_limit = eff_limit if eff_limit > 0 else 100000
    eff_min = min_samples if min_samples > 0 else max(
        1, int(getattr(config, "REVIEW_INSIGHTS_MIN_SAMPLES", 5) or 5)
    )
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        # v453: 注入池要拉得比注入条数多 —— 排序交给 _rank_insight_candidates，
        # 否则 SQL 的 ORDER BY 直接决定谁进 prompt，证据强度根本没机会参与。
        pool = sql_limit if getattr(config, "REVIEW_INSIGHTS_RANK", 1) == 0 else max(sql_limit, 40)
        rows = conn.execute(
            "SELECT id, rule, COALESCE(origin, 'real') AS origin, source_samples,"
            " verification_verdict, verification_sample_n, insight_param_key,"
            " insight_direction, observe_only"
            " FROM review_insights"
            " WHERE active = 1 AND kind = 1 AND (origin = 'seeded'"
            "   OR verification_verdict IN ('verified', 'partial')"
            # v453: 证据量取两边较大值 —— 决策级归因的 n 记在 verification_sample_n，
            # source_samples 可能是 0，旧门槛会把唯一 verified 的那条挡在 prompt 外。
            "   OR (CASE WHEN COALESCE(source_samples, 0) >= COALESCE(verification_sample_n, 0)"
            "           THEN COALESCE(source_samples, 0) ELSE COALESCE(verification_sample_n, 0) END) >= ?)"
            " ORDER BY source_samples DESC, id DESC LIMIT ?",
            (eff_min, pool),
        ).fetchall()
        items = [{
            "id": int(r["id"]),
            "rule": str(r["rule"]),
            "origin": str(r["origin"] or "real"),
            "source_samples": max(int(r["source_samples"] or 0), int(r["verification_sample_n"] or 0)),
            "verdict": str(r["verification_verdict"] or ""),
            "param_key": str(r["insight_param_key"] or ""),
            "direction": int(r["insight_direction"] or 0),
            "observe_only": int(r["observe_only"] or 0),
        } for r in rows]
        if getattr(config, "REVIEW_INSIGHTS_RANK", 1):
            items = rank_insight_candidates(items, eff_limit)
        elif eff_limit > 0:
            items = items[:eff_limit]
        return [{"id": it["id"], "rule": it["rule"], "origin": it["origin"]} for it in items]
    finally:
        if own:
            conn.close()


# v453: 经验注入的排序权重 —— 证据强度优先，样本数只做同档内的次序
_VERDICT_RANK = {"verified": 3, "partial": 2, "param_rule": 1, "": 0, "failed": -1}


# v453: 反例文本清洗 —— verifier_feedback 曾把多个错误码说明无条件拼接（无分隔符），
# 71 条生效经验里 70 条的反例成了 "E020_CONTAMINATED: 基于 1 个…E005_CONTAMINATED:…"，
# 原样注入 prompt 是纯噪声，还占用 1400 字符预算。
_CODE_SEG = re.compile(r"(E\d{3}[_A-Z]*[:：])")


def clean_counter_factual(text: Any, max_chars: int = 90) -> str:
    """把反例字段整理成一句人话；纯错误码说明 → 压缩成带例数的结构化短句。

    旧数据里错误码段可能被截断（"E014_CONTAMINATED: 基于 2 个"），所以按「段头」
    切分而不是匹配完整句式：段头之后的整段都算错误码说明，其余算人话。

    re.split 带捕获组后 → [人话, 段头, 段体, 段头, 段体, …]，偶数位是人话。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    parts = _CODE_SEG.split(raw)
    texts: List[str] = []
    counts: Dict[str, int] = {}
    for i, piece in enumerate(parts):
        if i % 2 == 0:
            # i=0 是首个段头之前的人话；i>0 的偶数位是「上一个段头的段体」，不算人话
            if i == 0 and piece.strip():
                texts.append(piece.strip())
            continue
        code = piece.rstrip(":：").split("_")[0].upper()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        m = re.search(r"基于\s*(\d+)\s*个", body or "")
        n = int(m.group(1)) if m else 1
        counts[code] = max(counts.get(code, 0), n)
    joined = re.sub(r"；{2,}", "；", "；".join(texts)).strip(" ；，,。")
    if joined:
        return joined[:max_chars]
    if counts:
        # 结构化：每个事故码带例数，模型能看出"哪类事故削的权、削了多少"
        detail = "/".join(f"{c}×{n}" for c, n in sorted(counts.items()))
        return f"{detail}（共 {sum(counts.values())} 例事故，可信度已削弱）"[:max_chars]
    return raw[:max_chars]
    return raw[:max_chars]


def rank_insight_candidates(items: List[Dict[str, Any]], limit: int = 0) -> List[Dict[str, Any]]:
    """按"这条经验有多可信"排序，并对同一参数键做冲突消解。

    旧行为：`ORDER BY source_samples DESC` —— 样本数说了算。后果是唯一 verified
    的洞察（n=34）被样本数 7~12 的挤掉，而同一参数键上方向相反的两条会一起进
    prompt，模型只能自己猜听哪条。

    新规则（v453）：
      1. 证据强度：verified > partial > param_rule > 未验证；failed 永不注入；
      2. 同参数键只留一条（强度高者胜，同强度取样本多者）—— 避免"提高/降低"打架；
      3. 同档内再按 source_samples 排，最后 id 大的优先（新经验顶掉旧的）；
      4. 没有参数键的仍然可以进（治理类经验不带键），但排在带键的之后。
    """
    def _key(it: Dict[str, Any]) -> Tuple[int, int, int, int, int, int]:
        return (
            _VERDICT_RANK.get(it.get("verdict", ""), 0),
            1 if it.get("param_key") else 0,
            # 带资产的比全局的更具体，优先（同一键上 WTI 的经验比"通用"更有用）
            1 if str(it.get("asset") or "") not in ("", "NONE", "ALL") else 0,
            int(it.get("source_samples") or 0),
            int(it.get("id") or 0),
            -int(it.get("observe_only") or 0),
        )

    ranked = sorted(items, key=_key, reverse=True)
    out: List[Dict[str, Any]] = []
    used_keys: set = set()
    for it in ranked:
        if it.get("verdict") == "failed":
            continue
        # v420: 消解粒度从"参数键"细化到"参数键+资产+分桶"。
        # 旧粒度下 score.buy_min_score 只能留 1 条 —— WTI/XAU/强势行情三种语境
        # 互相挤掉，最终 6 条可测经验只进 3 条。同键同桶仍只留最强的一条，
        # 防止"提高/降低"打架；不同桶/不同资产可以并存。
        pk = ((it.get("param_key") or "").strip(),
              str(it.get("asset") or "").strip(),
              str(it.get("bucket") or "").strip())
        if pk[0]:
            if pk in used_keys:
                continue
            used_keys.add(pk)
        out.append(it)
        if limit > 0 and len(out) >= limit:
            break
    return out


def normalize_insight_hit_ids(raw: Any) -> List[int]:
    """把 AI 输出声称采纳的经验 id 规范化为合法 int 列表（去重/升序/过滤非法）。

    AI 可能返回 JSON 字符串、int 列表或混入非法值（字符串 id、负数、幻觉 id 等）；
    这里只做「形状与数值」归一（id 必须为正整数）。是否属于当前生效经验 id，
    由调用方用 load_active_insight_items() 结果二次过滤，避免脏数据污染命中归因。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out: List[int] = []
    for x in raw:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        if v not in out:
            out.append(v)
    out.sort()
    return out


def load_all_insight_items(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """全部经验（含生效与停用，带 id/origin），供前端按来源手动清理。

    与 load_active_insight_items 的区别：那个只给注入 prompt 的"生效且样本达标"子集；
    这个给全量视图，让用户能删除任意一条（含已停用、无法再验证的旧经验）。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, rule, COALESCE(origin, 'real') AS origin, active,"
            " source_samples, created_at"
            " FROM review_insights ORDER BY active DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "rule": str(r["rule"]),
                "origin": str(r["origin"] or "real"),
                "active": int(r["active"] or 0),
                "source_samples": int(r["source_samples"] or 0),
                "created_at": str(r["created_at"] or ""),
            }
            for r in rows
        ]
    finally:
        if own:
            conn.close()


def load_deactivated_insights(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """最近被自动停用的经验（active=0，按入库倒序），供前端展示空态原因。

    停用原因：命中该经验的交易胜率偏低（_evaluate_insights 按命中归因自动停用，
    或人工在提示库/复盘页手动停用）。展示为灰色，让用户知道经验不是丢了而是被停用。
    注：已改为双向可恢复 —— 命中胜率回升到阈值以上会自动恢复启用。

    返回含 id 与 origin：前端要靠它们显示来源标记（快速评测/历史交易）并支持手动删除。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, rule, source_samples, created_at, COALESCE(origin, 'real') AS origin"
            " FROM review_insights"
            " WHERE active = 0 ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "rule": str(r["rule"]),
                "source_samples": int(r["source_samples"] or 0),
                "created_at": str(r["created_at"] or ""),
                "origin": str(r["origin"] or "real"),
            }
            for r in rows
        ]
    finally:
        if own:
            conn.close()


def count_insights(active: bool, conn: Optional[sqlite3.Connection] = None) -> int:
    """统计生效 / 停用的经验条数（供前端说明空态原因）。"""
    own = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active = ?", (1 if active else 0,)
        ).fetchone()
        return int(row[0] or 0)
    finally:
        if own:
            conn.close()


def toggle_insight(insight_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """手动启用/停用一条经验（1↔0），返回切换后的状态。

    为什么需要：提供人工出口，不必等自动评估。此前自动停用是单向的（只遍历
    active=1 的经验、停用后永不复查），曾出现全部经验"连坐"停用后无法恢复；
    现已改为按命中归因 + 双向可恢复，本接口仍保留供人工即时干预。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT active FROM review_insights WHERE id = ?", (int(insight_id),)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "经验不存在"}
        next_active = 0 if int(row["active"] or 0) else 1
        # 记下"是不是人工停用的"：自动自愈要跳过人工停用，否则重启会把它复活
        conn.execute(
            "UPDATE review_insights SET active = ?, manual_deactivated = ? WHERE id = ?",
            (next_active, 1 if next_active == 0 else 0, int(insight_id)),
        )
        conn.commit()
        return {"ok": True, "id": int(insight_id), "active": next_active}
    finally:
        if own:
            conn.close()


def delete_insight(insight_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """硬删除一条经验规则（含生效与停用）。

    用途：重置模拟盘会保留复盘经验（设计如此），用户需要能手动清理
    —— 尤其是来源=quick_sim 的经验，其底层样本可能已被重置删除、无法再验证。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        cur = conn.execute("DELETE FROM review_insights WHERE id = ?", (int(insight_id),))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "经验不存在"}
        return {"ok": True, "id": int(insight_id)}
    finally:
        if own:
            conn.close()


def delete_insights_by_origin(origin: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """按来源批量删除经验（origin: real / quick_sim / rule）。"""
    key = (origin or "").strip()
    if key not in ("real", "quick_sim", "rule"):
        return {"ok": False, "error": "来源只能是 real / quick_sim / rule"}
    own = conn is None
    conn = conn or db.get_connection()
    try:
        # COALESCE 兼容历史数据里 origin 为空的行（按 real 归类，与读取时一致）
        cur = conn.execute(
            "DELETE FROM review_insights WHERE COALESCE(origin, 'real') = ?", (key,)
        )
        conn.commit()
        return {"ok": True, "origin": key, "deleted": int(cur.rowcount)}
    finally:
        if own:
            conn.close()
