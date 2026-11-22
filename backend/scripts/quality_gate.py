#!/usr/bin/env python3
"""质量门禁：只拦「退化」，不拦「历史债」。

为什么要有这个脚本，而不是在 CI 里直接裸跑 4 个工具：

    backend/src_python 有 441 个平铺模块、6784 个函数。第一次接工具时：
      · vulture 100% 置信度就报 27 条（未使用变量 / 不可达代码 / 冗余条件）
      · complexipy 默认阈值 15 会报 1126 个函数，>25 的有 548 个，最高 1978
    如果这些直接进 CI 红线，第一次提交就满屏红，人的反应一定是加 --ignore，
    门禁当场作废。所以这里把「今天的状态」冻结成基线（.quality/），
    CI 只回答一个问题：**这次改动有没有让情况变差**。

三层各查各的，互不替代：
    import-linter  → 结构方向（拆分不会长回去）
    vulture        → 死代码（没人用的东西）
    deptry         → 依赖声明（装了没用的包）
    complexipy     → 认知复杂度（哪段又变难读了）

它们都**不检查逻辑对错**。闸门算得准不准，只有基线评估和真实样本能回答。

用法（工作目录：backend/）：
    python3 scripts/quality_gate.py            # 检查，有退化则退出码 1
    python3 scripts/quality_gate.py --update   # 重新冻结基线（改完代码后手动跑）
    python3 scripts/quality_gate.py --only vulture

依赖：pip install -r requirements-dev.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BASELINE_DIR = BACKEND / ".quality"
VULTURE_BASE = BASELINE_DIR / "vulture_baseline.txt"
COMPLEX_BASE = BASELINE_DIR / "complexipy_baseline.json"

# 新函数允许的最大认知复杂度。25 是「明显该拆」的线，
# 取 15（complexipy 默认）会把正常的分支聚合也算进去，噪音太大。
NEW_FUNC_MAX_COMPLEXITY = 25
# 已有函数允许的复杂度涨幅。+3 是给「多加一两个分支」留的余量，
# 超过就说明这次改动是在给巨型函数继续加料 —— 那正是要拦的事。
EXISTING_FUNC_SLACK = 3

VENV_PY = Path("/tmp/qenv/bin/python")


def _run(cmd: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 仓库用的是平铺模块名（`import review.xxx`），所以 src_python 必须在前。
    # 不能 setdefault：外部若已设 PYTHONPATH，会把正确路径挤掉。
    src = str(BACKEND / "src_python")
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}:{old}" if old else src
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd, cwd=str(BACKEND), env=env, capture_output=True, text=True,
    )


def _tool(name: str) -> str:
    """优先用 /tmp/qenv 里的工具，否则走 PATH。"""
    local = VENV_PY.parent / name
    return str(local) if local.exists() else name


# ── vulture ────────────────────────────────────────────────────────────
VULTURE_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): (?P<msg>.+?) \(\d+% confidence\)$")


def collect_vulture() -> set[str]:
    proc = _run([_tool("vulture"), "src_python", "--min-confidence", "100"])
    if proc.returncode not in (0, 3):  # 3 = 找到死代码，属正常返回
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"vulture 执行失败（退出码 {proc.returncode}）")
    found: set[str] = set()
    for raw in proc.stdout.splitlines():
        m = VULTURE_LINE.match(raw.strip())
        if not m:
            continue
        # 键里不放行号：改个 import 就会让行号整体位移，那样每次都会误报。
        found.add(f"{m.group('path')} :: {m.group('msg')}")
    return found


# ── complexipy ─────────────────────────────────────────────────────────
def collect_complexipy() -> dict[str, int]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out = tmp.name
    try:
        proc = _run([
            _tool("complexipy"), "src_python",
            "--output-format", "json", "--output", out, "-C", "no", "-q",
        ])
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            raise SystemExit(f"complexipy 执行失败（退出码 {proc.returncode}）")
        data = json.loads(Path(out).read_text(encoding="utf-8"))
    finally:
        Path(out).unlink(missing_ok=True)
    return {f"{item['path']} :: {item['function_name']}": item["complexity"] for item in data}


# ── import-linter / deptry ──────────────────────────────────────────────
def run_import_linter() -> tuple[bool, str]:
    proc = _run([_tool("lint-imports")])
    ok = proc.returncode == 0
    body = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(body.strip().splitlines()[-6:])
    return ok, tail


def run_deptry() -> tuple[bool, str]:
    proc = _run([_tool("deptry"), "src_python", "--no-ansi", "--config", "pyproject.toml"])
    ok = proc.returncode == 0
    lines = [ln for ln in (proc.stdout or "").splitlines()
             if re.search(r"\bDEP\d{3}\b", ln)]
    tail = "\n".join(lines[:10]) or "(无输出)"
    return ok, tail


# ── 比对与基线维护 ─────────────────────────────────────────────────────
def load_baseline_vulture() -> set[str]:
    if not VULTURE_BASE.exists():
        return set()
    return {ln.strip() for ln in VULTURE_BASE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def load_baseline_complex() -> dict[str, int]:
    if not COMPLEX_BASE.exists():
        return {}
    return json.loads(COMPLEX_BASE.read_text(encoding="utf-8"))


def write_baseline(vulture: set[str], complex_: dict[str, int]) -> None:
    BASELINE_DIR.mkdir(exist_ok=True)
    header = (
        "# vulture 基线（--min-confidence 100）\n"
        "# 冻结于 2026-11-22。新增条目会让 quality 工作流变红；\n"
        "# 修掉一个就从这里删一行 —— 这个文件只应该变短。\n"
    )
    VULTURE_BASE.write_text(
        header + "\n".join(sorted(vulture)) + "\n", encoding="utf-8"
    )
    COMPLEX_BASE.write_text(
        json.dumps(dict(sorted(complex_.items())), indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="重新冻结基线")
    ap.add_argument("--only", choices=["vulture", "complexipy", "imports", "deps"],
                    help="只跑某一项")
    args = ap.parse_args()

    failures: list[str] = []

    if args.only in (None, "vulture"):
        cur = collect_vulture()
        if args.update:
            base_v = load_baseline_vulture()
            write_baseline(cur, load_baseline_complex())
            print(f"vulture: 基线已更新，{len(cur)} 条（原 {len(base_v)} 条）")
        else:
            base = load_baseline_vulture()
            added = cur - base
            fixed = base - cur
            print(f"vulture        : 当前 {len(cur)} 条，基线 {len(base)} 条",
                  end="")
            if added:
                print(f" → 新增 {len(added)} 条 ❌")
                for item in sorted(added):
                    print(f"  + {item}")
                failures.append("vulture 新增死代码")
            else:
                print(" → 无新增 ✅")
            if fixed:
                print(f"  （已修掉 {len(fixed)} 条，记得 --update 让基线变短）")

    if args.only in (None, "complexipy"):
        cur_c = collect_complexipy()
        if args.update:
            write_baseline(load_baseline_vulture(), cur_c)
            print(f"complexipy: 基线已更新，{len(cur_c)} 个函数")
        else:
            base_c = load_baseline_complex()
            new_hot = {k: v for k, v in cur_c.items()
                       if k not in base_c and v > NEW_FUNC_MAX_COMPLEXITY}
            grown = {k: (base_c[k], v) for k, v in cur_c.items()
                     if k in base_c and v > base_c[k] + EXISTING_FUNC_SLACK}
            hot_total = sum(1 for v in cur_c.values() if v > NEW_FUNC_MAX_COMPLEXITY)
            print(f"complexipy     : >{NEW_FUNC_MAX_COMPLEXITY} 的函数 {hot_total} 个"
                  f"（基线 {sum(1 for v in base_c.values() if v > NEW_FUNC_MAX_COMPLEXITY)} 个）",
                  end="")
            if new_hot or grown:
                print(f" → 新增高复杂度 {len(new_hot)} / 恶化 {len(grown)} ❌")
                for k, v in sorted(new_hot.items(), key=lambda x: -x[1])[:10]:
                    print(f"  + 新函数 {k} = {v}")
                for k, (old, new) in sorted(
                    grown.items(), key=lambda x: -(x[1][1] - x[1][0])
                )[:10]:
                    print(f"  ↑ {k}: {old} → {new}")
                failures.append("complexipy 复杂度退化")
            else:
                print(" → 无新增、无恶化 ✅")

    if args.only in (None, "imports"):
        ok, tail = run_import_linter()
        print(f"import-linter  : {'✅ 契约全部保持' if ok else '❌ 架构契约被破坏'}")
        if not ok:
            print(tail)
            failures.append("import-linter 契约破坏")

    if args.only in (None, "deps"):
        ok, tail = run_deptry()
        print(f"deptry         : {'✅ 无新增依赖问题' if ok else '❌ 依赖声明有问题'}")
        if not ok:
            print(tail)
            failures.append("deptry 依赖问题")

    print()
    if failures:
        print("门禁未通过：" + "；".join(failures))
        print("说明：这些检查只覆盖结构/死代码/依赖/复杂度，不覆盖业务逻辑正确性。")
        return 1
    print("门禁通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
