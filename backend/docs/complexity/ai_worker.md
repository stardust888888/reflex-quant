# 结构体检：`ai_worker()`

- 文件：`src_python/engine/ai_worker.py`
- 位置：第 8859–12315 行（**3457 行**，全文件 12315 行）
- 分支（AST 近似）：**834**；最大嵌套深度：**13**
- `await`：16 处；数据库/文件写调用：114 处
- 顶层语句：26 条 → 纯计算块 2 段 / IO·并发边界 2 段

## 嵌套函数（已天然独立，可优先提升）

「闭包引用」= 它引用了多少个外层函数的局部变量。0 个可直接搬成模块级函数；非 0 个要么改成传参，要么先做一个小对象存状态。

| 函数 | 行 | 行数 | await | 闭包引用 | 名字 |
|---|---|---|---|---|---|
| `_log_progress` | 9042–9062 | 21 | 0 | 3 | `Exception`, `open`, `str` |
| `_batch_heartbeat` | 9068–9088 | 21 | 1 | 2 | `Exception`, `_log_progress` |
| `_batch_claim` | 9203–9318 | 116 | 0 | 17 | `Exception`, `_bt`, `_bt_stk`, `_claim_exc`, `_diag_err` … |
| `_spool_on_done` | 9822–9830 | 9 | 1 | 8 | `Exception`, `_e_sp`, `dict`, `int`, `isinstance` … |
| `_batch_persist` | 9962–12157 | 2196 | 0 | 119 | `Exception`, `TypeError`, `ValueError`, `_BATCH_RETRY_COOLDOWN_SEC`, `_MAX_PENDING_RETRIES` … |
| `_do_reclaim` | 9105–9117 | 13 | 0 | 2 | `_build_reclaim_sql`, `_reclaim_conn` |
| `_do_claim` | 9250–9284 | 35 | 0 | 10 | `Exception`, `_has_fresh_tradeable`, `_pf_exc`, `_prefilter_apply`, `conn` … |
| `_is_channel_failure` | 12115–12117 | 3 | 0 | 4 | `_CHANNEL_FAIL_KW`, `any`, `bool`, `str` |
| `_w` | 9305–9307 | 3 | 0 | 1 | `Exception` |

## 纯计算块（无 await、不碰数据库写 —— 拆分从这里开始最安全）

| 行区间 | 语句数 | 首句 |
|---|---|---|
| 8860–8914 | 5 | `'\n    Concurrent batch processor.\n\n    Every 1 second:\n      1) Atomically` |
| 8993–9030 | 19 | `_last_gate_replay_ts: float = 0.0` |

## IO / 并发边界（必须留在原函数里，或显式传参）

| 行区间 | 语句数 | 卡点 |
|---|---|---|
| 8917–8989 | 1 | IO 调用: close/commit/execute |
| 9032–12315 | 1 | await 16 处；IO 调用: close/commit/execute/fetchall/fetchone/rollback/write |

## 怎么用这份报告

1. 先挑纯计算块里**连续且最长**的那几段，抽成模块级函数 —— 它们没有事务与并发语义，抽完跑回归即可验证。
2. 再动嵌套函数：已经是独立单元，提升到模块级只会让主函数变短，不改行为。
3. 最后才碰 IO 边界：每抽一段，都要确认它和主函数共享的事务（同一个 `conn`）如何处理，宁可传参也不要重建连接。
4. 每一步都跑一次 quality_gate：复杂度只能降，不能升。
