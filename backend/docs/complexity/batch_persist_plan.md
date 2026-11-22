# `_batch_persist()` 拆分方案（方案稿，未动代码）

## 一、现状（实测，不是估计）

| 指标 | 值 |
|---|---|
| 位置 | `src_python/engine/ai_worker.py` 第 10012–12202 行 |
| 行数 | **2191 行** |
| 分支（AST 近似） | **573** |
| 最大嵌套深度 | **12** |
| `await` | 0（纯同步，跑在线程里） |
| 数据库/文件写调用 | **79** 处 |
| 事务结构 | 单个 `try:` 包住 10041–12201，即**一个事务写完整批** |
| 真实闭包变量 | **14 个**（不是体检脚本早先报的 98 —— 那个数字把内层函数自身的局部和 builtin 也算进去了） |

14 个闭包变量，按用途分三组：

- **输入数据（9）**：`successes`、`batch`、`content_map`、`published_map`、`timestamp_map`、`model_pool`、`gate_verdict_map`、`gate_strength_map`、`gate_impact_map`
- **上下文（2）**：`market_context`、`decision_context`
- **运行环境（3）**：`batch_end`、`loop`、`failures`

这 14 个变量在 2191 行里只被引用 **31 次**。也就是说：拆这个函数真正的难点不在"变量搬不动"，而在"573 个分支 + 79 处写 + 单事务不能拆错"。

## 二、为什么不能直接整体搬到模块级

整体搬出去，`_batch_persist` 会变成一个新函数，复杂度约 573 —— 门禁的新函数上限是 25，必然判红。

这不是理论风险：本轮 `_batch_claim` 就是先整体搬（复杂度 27）、被门禁拦下，才继续拆成 `_main_budget_exhausted`（3）和 `_dump_lock_diag`（7）才过的。所以**搬移和拆分必须同批完成**，不能"先搬后拆"。

## 三、拆法：四段，每段独立提交

### 阶段 0：上下文打包（不改逻辑）

模块级新增一个数据类：

```python
@dataclass
class _BatchPersistCtx:
    successes: Any
    batch: Any
    content_map: dict
    ...
```

`_batch_persist` 先改成接收 `ctx`（仍留在函数体内），内部 31 处引用改 `ctx.xxx`。
此步行为零变化，只为了让后面每段能被单独调用。

### 阶段 1：3a 主写入循环（最大的一块）

docstring 已声明两阶段：3a 写 `ai_decisions` 主行 + 聚合 + 入场价；3b 用 Doubao 结果回填同一行。
先抽 3a：

```python
def _bp_write_primary(conn, ctx) -> List[Dict[str, Any]]:
    ...
```

返回 `written`，供 3b 用。

### 阶段 2：3b Doubao 回填

```python
def _bp_fill_doubao(conn, ctx, written) -> None
```

### 阶段 3：中途抢救 / 校验 / 告警

`_bp_rescue(...)`、`_bp_validate(...)` 之类，按实际段落切。原则：**每段 ≤300 行、≤25 分支**，超了就再切一层。

### 阶段 4：主函数只剩事务壳

```python
def _batch_persist(ctx):
    conn = _open_db()
    ...busy_timeout 60s...
    try:
        written = _bp_write_primary(conn, ctx)
        _bp_fill_doubao(conn, ctx, written)
        _bp_rescue(conn, ctx, ...)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return written
```

到这一步它复杂度应该 ≤10，再整体搬到模块级就安全了。`ai_worker` 会一次性降掉 500 多。

## 四、红线（每段都必须守）

1. **同一个 `conn`，同一个事务**。任何子函数都不许自己 `_open_db()`。
2. **顺序不可交换**：先写主行、再回填 Doubao、再做抢救。
3. **异常语义不变**：任何一段抛 → 整事务 `rollback` → 外层 `retry_on_busy`/重试 6 次的那套逻辑原样生效。
4. **`busy_timeout=60s` 必须在开事务之前设好**，不能落到子函数里。
5. 每抽一段立即验证：`py_compile` → 5 个 ai_worker 用例（42 passed / 1 skipped）→ 四项门禁。
6. 每段提交独立，出问题可单点回退。

## 五、验收标准

- `ai_worker` 复杂度从 1856 降到 **100 以内**
- 所有新函数复杂度 **≤25**
- 现有 42 条 ai_worker 用例全过，新增每段的针对性用例（临时 db + 最小表）
- 门禁四项绿 + CI 绿

## 六、主要风险

- 单事务里混着"写库 / 计算 / 告警"，最易出错的是 **conn 生命周期错位**（某段提前 close 或提前 commit），会让整批写入变成部分提交 —— 这比崩溃更难查。
- 573 个分支里大量是字段兜底（`or ""`、`or 0`、`isinstance` 判断），切段时容易把兜底挪到错误的层级，导致脏数据绕过校验。
- 每段都要有"写进去了什么"的断言，不能只测"没抛异常"。

## 七、建议节奏

| 批次 | 内容 | 预期效果 |
|---|---|---|
| B1 | 阶段 0 上下文打包 | 行为不变，为后续铺路 |
| B2 | 抽 3a 主写入 | `ai_worker` 降约 200+ |
| B3 | 抽 3b 回填 + 抢救段 | 再降约 200 |
| B4 | 剩余校验段 + 事务壳搬模块级 | 落到 100 以内 |

如果中间任一批门禁红，按"先拆到 ≤25 再提交"处理，不允许把红的状态合进去。
