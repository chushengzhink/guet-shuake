# review-97cb993 处理记录

基线 `97cb993`，[review-97cb993.md](review-97cb993.md) 提出的 7 项全部处理完毕。

| 编号 | 级别 | 问题 | 处理 |
|---|---|---|---|
| #1 | P1 | 课程统计在线程池里用同一个 Playwright 客户端 | 去掉 `ThreadPoolExecutor`，改为**在请求线程内串行**统计。browser 引擎的 `APIRequestContext` 只能由创建线程调用；串行同时满足 HTTP 的 Session 串行化 |
| #2 | P2 | 客户端刚入缓存就被调用方关闭 | `_client_for()` 首次创建也返回 `_NoopClose`，资源由缓存持有；只有 `drop_client()`（登录/过期/退出）才真正 close；`api_quit` 统一清理缓存 |
| #3 | P2 | 账号锁只保护获取、不保护使用 | `api_courses/api_plan/api_bench` 整个读取过程持 `account_lock`；锁改为 `RLock`（同一线程取 client 后还要继续用）；登录会等待读取结束 |
| #4 | P2 | 统计失败被伪装成「零可刷项」 | 失败时保留 `auto/manual/other = None` 并置 `stats_error=True`，前端显示「统计失败 · 点刷新重试」 |
| #5 | P2 | 卡片可刷数量与实际计划不一致 | 统计改用 `build_plan()` 本身，与执行器共用「未发布 / 无附件 / 拿不到时长 / 人工」全部规则 |
| #6 | P2 | 取消仍汇总为成功 | `run_one` 在存在 pending 且 `cancel.requested()` 时写入 `error="已取消，N 项待续"`，`ok=False`；未取消的 tick_once 不受影响 |
| #7 | P2 | 目标 <100% 时计划分母不准 | `build_plan(..., target=)` 新增达标跳过：活动已读百分比 ≥ target 时标记 skip（reason「已达目标 N%」）；`/api/plan` 读取并透传 target |

## 验证

- `python -m unittest discover -s tests -q` → **112 项通过**（新增 `tests/test_review_97cb993.py` 6 项：串行断言、缓存所有权、统计失败保留未知、计划一致性、达标跳过、取消终态）
- `ruff --select F821,F822,F823` → All checks passed
- 真实账号（17 门课）复测：`基础工业工程 63/74 → 人工 3 · 待完成 8`、`马克思 172/204 → 待完成 32`、`工程经济学 8/8 → 全 0`，与 build_plan 计划一致

## 仍未处置

- `#7` 的达标跳过只作用于**活动级**已读百分比（与 `VideoJob` 判定一致）；课程级 `study_completeness` 仍由平台计算，二者口径不同时以活动级为准。
- 报告未覆盖、已知遗留：请求计数未拆分、`build_plan` 与 `prepare` 存在重复读取（性能项）。
