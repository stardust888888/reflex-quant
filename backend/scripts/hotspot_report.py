#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hotspot_report.py — 给一个"巨型函数"做结构体检，产出可复跑的拆分底稿。

为什么需要
----------
quality_gate 能告诉你"这个函数复杂度 1978"，但说不清**从哪切**。直接上手改
一个 3400 行的协程，等于在没有网的情况下跳伞：拆錯一处，风控/落库/推送全断，
而且没有测试会告诉你断在哪。

这个脚本只做静态分析、不 import 目标模块，所以能在任何环境下跑：
    1) 量规模：行数、分支数、最大嵌套深度、await / 数据库写调用密度；
    2) 列嵌套函数：哪些内部函数已经天然是独立单元（可以直接提升为模块级）；
    3) 标"安全块"：连续若干条**不含 await、不碰数据库写**的顶层语句 —— 这类
       块没有并发/事务语义，抽成纯函数风险最低，适合作为拆分的起点；
    4) 其余位置标成"IO/并发边界"，提示拆分时必须保留在原函数里（或显式传参）。

用法:
    python3 scripts/hotspot_report.py src_python/engine/ai_worker.py --function ai_worker
    python3 scripts/hotspot_report.py src_python/engine/ai_worker.py --function ai_worker \
        --out docs/complexity/ai_worker.md

注意：这里的分支计数是 AST 近似值，和 complexipy 的认知复杂度口径不同，
只用来判断量级与分布，不作为门禁数值。
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

BRANCH_TYPES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith,
    ast.BoolOp, ast.IfExp, ast.ExceptHandler, ast.Assert,
)
# 只要出现这些调用，就认为这段不能当纯函数抽走（事务/并发/外部副作用）
IO_ATTRS = ("execute", "executemany", "executescript", "commit", "rollback",
            "fetchone", "fetchall", "close", "write", "send", "flush")


@dataclass
class Block:
    start: int
    end: int
    count: int
    first_line: str
    pure: bool
    reasons: list[str] = field(default_factory=list)


def _find_function(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _module_names(tree: ast.AST) -> set[str]:
    """模块顶层定义的名字（import / 赋值 / def / class）—— 这些不算闭包变量。"""
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _closure_refs(fn: ast.AST, module_names: set[str]) -> list[str]:
    """嵌套函数引用了多少"外层函数的局部变量"。

    这是提升为模块级函数的真正卡点：引用 0 个 → 直接搬走；引用 N 个 → 要么
    作为参数传入，要么做成 small object 保存状态。报告必须先把这件事说清楚，
    否则"提升嵌套函数"看起来比实际简单。
    """
    local: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.arg):
            local.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            local.add(n.name)
    loads = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(loads - local - module_names)


def _branch_count(node: ast.AST) -> int:
    total = 0
    for child in ast.walk(node):
        if isinstance(child, BRANCH_TYPES):
            # 一个 BoolOp 里有 N 个 and/or 记 N-1 个额外分支，和认知复杂度口径接近
            total += max(1, len(child.values) - 1) if isinstance(child, ast.BoolOp) else 1
    return total


def _max_depth(node: ast.AST, depth: int = 0) -> int:
    """复合语句的嵌套深度（不算函数体本身这一层）。"""
    nested_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, nested_types):
            best = max(best, _max_depth(child, depth + 1))
        else:
            best = max(best, _max_depth(child, depth))
    return best


def _awaits(node: ast.AST) -> int:
    return sum(1 for c in ast.walk(node) if isinstance(c, ast.Await))


def _io_calls(node: ast.AST) -> list[str]:
    hits = []
    for c in ast.walk(node):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute):
            if c.func.attr in IO_ATTRS:
                hits.append(c.func.attr)
    return hits


def _first_line(node: ast.AST, limit: int = 78) -> str:
    try:
        text = ast.unparse(node).splitlines()[0].strip()
    except Exception:
        text = type(node).__name__
    return text[:limit]


def _blocks(fn: ast.AST) -> list[Block]:
    """把函数体按"能不能当纯函数抽走"切成连续块。"""
    out: list[Block] = []
    cur: Block | None = None
    for stmt in getattr(fn, "body", []):
        reasons = []
        if _awaits(stmt):
            reasons.append("await %d 处" % _awaits(stmt))
        io = _io_calls(stmt)
        if io:
            reasons.append("IO 调用: " + "/".join(sorted(set(io))))
        pure = not reasons
        if cur is not None and pure and cur.pure:
            cur.end = stmt.end_lineno or stmt.lineno
            cur.count += 1
            continue
        cur = Block(start=stmt.lineno, end=stmt.end_lineno or stmt.lineno,
                    count=1, first_line=_first_line(stmt), pure=pure, reasons=reasons)
        out.append(cur)
    return out


def build_report(path: Path, fn_name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_function(tree, fn_name)
    if fn is None:
        raise SystemExit(f"找不到函数 {fn_name}（{path}）")

    lines = src.splitlines()
    start = fn.lineno
    end = getattr(fn, "end_lineno", None) or start
    body_lines = end - start + 1
    nested = [n for n in ast.walk(fn)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn]
    blocks = _blocks(fn)
    pure_blocks = [b for b in blocks if b.pure]
    io_blocks = [b for b in blocks if not b.pure]

    md = []
    md.append(f"# 结构体检：`{fn_name}()`")
    md.append("")
    md.append(f"- 文件：`{path.as_posix()}`")
    md.append(f"- 位置：第 {start}–{end} 行（**{body_lines} 行**，全文件 {len(lines)} 行）")
    md.append(f"- 分支（AST 近似）：**{_branch_count(fn)}**；最大嵌套深度：**{_max_depth(fn)}**")
    md.append(f"- `await`：{_awaits(fn)} 处；数据库/文件写调用：{len(_io_calls(fn))} 处")
    md.append(f"- 顶层语句：{len(getattr(fn, 'body', []))} 条 → 纯计算块 {len(pure_blocks)} 段 / IO·并发边界 {len(io_blocks)} 段")
    md.append("")
    if nested:
        module_names = _module_names(tree)
        md.append("## 嵌套函数（已天然独立，可优先提升）")
        md.append("")
        md.append("「闭包引用」= 它引用了多少个外层函数的局部变量。0 个可直接搬成模块级函数；"
                  "非 0 个要么改成传参，要么先做一个小对象存状态。")
        md.append("")
        md.append("| 函数 | 行 | 行数 | await | 闭包引用 | 名字 |")
        md.append("|---|---|---|---|---|---|")
        for n in nested:
            n_end = getattr(n, "end_lineno", n.lineno) or n.lineno
            refs = _closure_refs(n, module_names)
            shown = ", ".join(f"`{r}`" for r in refs[:5]) + (" …" if len(refs) > 5 else "")
            md.append(f"| `{n.name}` | {n.lineno}–{n_end} | {n_end - n.lineno + 1} | "
                      f"{_awaits(n)} | {len(refs)} | {shown or '—'} |")
        md.append("")
    md.append("## 纯计算块（无 await、不碰数据库写 —— 拆分从这里开始最安全）")
    md.append("")
    if not pure_blocks:
        md.append("（无：整个函数都泡在 IO/并发里，拆分需要先抽出可注入的依赖）")
    else:
        md.append("| 行区间 | 语句数 | 首句 |")
        md.append("|---|---|---|")
        for b in sorted(pure_blocks, key=lambda x: -x.end + x.start)[:25]:
            md.append(f"| {b.start}–{b.end} | {b.count} | `{b.first_line}` |")
    md.append("")
    md.append("## IO / 并发边界（必须留在原函数里，或显式传参）")
    md.append("")
    md.append("| 行区间 | 语句数 | 卡点 |")
    md.append("|---|---|---|")
    for b in io_blocks:
        md.append(f"| {b.start}–{b.end} | {b.count} | {'；'.join(b.reasons)} |")
    md.append("")
    md.append("## 怎么用这份报告")
    md.append("")
    md.append("1. 先挑纯计算块里**连续且最长**的那几段，抽成模块级函数 —— 它们没有事务与并发语义，抽完跑回归即可验证。")
    md.append("2. 再动嵌套函数：已经是独立单元，提升到模块级只会让主函数变短，不改行为。")
    md.append("3. 最后才碰 IO 边界：每抽一段，都要确认它和主函数共享的事务（同一个 `conn`）如何处理，宁可传参也不要重建连接。")
    md.append("4. 每一步都跑一次 quality_gate：复杂度只能降，不能升。")
    md.append("")
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="源文件路径")
    ap.add_argument("--function", required=True, help="要分析的函数名")
    ap.add_argument("--out", help="写到文件；不给就打印到 stdout")
    args = ap.parse_args()

    report = build_report(Path(args.path), args.function)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"written: {out} ({len(report)} chars)")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
