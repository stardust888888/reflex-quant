# -*- coding: utf-8 -*-
"""结构守卫：四个巨型文件、一个超长函数，只允许变小，不允许变大。

为什么需要这条测试：ai_worker.py 一度 12,911 行、里面有一个 3,479 行的函数。
靠人自觉是守不住的 —— 拆一次只减几百行，而"顺手再加一个 if 分支"一天就能长回来。
所以把当前实测值写成上限：想变大，必须先改这里并写清理由，让"变大"成为一件
需要解释的事，而不是默认发生的事。

口径：上限取 v535 实测值，只可下调。
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"

# 只列真正的巨石；小文件不值得挨个盯
FILE_LINE_CAP = {
    "engine/ai_worker.py": 12_303,
    "api_server.py": 8_472,
    "bridge_server.py": 7_634,
    "review/review_agent.py": 4_846,
}

# 单函数上限：现有最长是 ai_worker() 的 3,479 行，留 21 行余量
MAX_FUNC_LINES = 3_500
MAX_FUNC_LINES_FILE = "engine/ai_worker.py"


def _read_lines(rel: str) -> int:
    return len((SRC / rel).read_text(encoding="utf-8").splitlines())


def test_giant_files_only_shrink():
    over = []
    for rel, cap in FILE_LINE_CAP.items():
        n = _read_lines(rel)
        if n > cap:
            over.append(f"{rel}: {n:,} 行 > 上限 {cap:,} 行（多了 {n - cap:,} 行）")
    assert not over, (
        "巨型文件又变大了。这些文件只能拆、不能续写，请把新增逻辑放到独立模块：\n  "
        + "\n  ".join(over)
    )


def test_longest_function_does_not_grow():
    path = SRC / MAX_FUNC_LINES_FILE
    tree = ast.parse(path.read_text(encoding="utf-8"))
    worst = max(
        (
            (n.end_lineno - n.lineno + 1, n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        default=(0, ""),
    )
    length, name = worst
    assert length <= MAX_FUNC_LINES, (
        f"{MAX_FUNC_LINES_FILE} 里的 {name}() 已经 {length} 行，超过上限 {MAX_FUNC_LINES}。"
        "这个函数只能拆，不能再加分支。"
    )


def test_no_new_file_over_eight_thousand_lines():
    """新写一个大文件是最容易的退化方式，这里顺手拦一下。"""
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in FILE_LINE_CAP:
            continue
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > 8_000:
            violations.append(f"{rel}: {n:,} 行")
    assert not violations, (
        "出现新的超大文件（>8000 行），请先拆再提交：\n  " + "\n  ".join(violations)
    )
