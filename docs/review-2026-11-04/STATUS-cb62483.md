# review-cb62483 处理记录

基线 `cb62483`，[review-cb62483.md](review-cb62483.md) 提出的 11 项（3 P1 + 8 P2）全部处理完毕。

| 编号 | 级别 | 问题 | 处理 |
|---|---|---|---|
| R01 | P1 | `_import_cookies` 里引用未定义的 `rt` | 请求头设置移到 `HttpTransport.__init__`，三种 Cookie 形态（None / Mapping / 列表）都先设头再导 cookie；`test_transport.py` 覆盖四种入参 |
| R02 | P1 | realtime `_advance` 用了 `step` 的局部 `end/start` | `_advance` 改为从 `self.gaps[0]` 取缺口长度；新增「上报未被记账」用例 |
| R03 | P1 | 课程异常后账号仍报 `ok=True` | `outcome.ok = not outcome.error and all(...)`；日志措辞区分「有失败项 / 执行出错」 |
| R04 | P2 | GUI 数字路数（字符串）被拒 | `_check_lanes` 接受数字字符串；`api_start` 统一把 `"8"` 转 int 后再校验 |
| R05 | P2 | 不等长缺口按上一段计时 | `next_at` 改为按**下一个待发段**长度计算。实测 200 秒视频、已看 `[10,100]`：耗时从 20 秒修正为 **110 秒**（等于剩余播放量） |
| R06 | P2 | realtime 重复收到上限会无限重试 | 只有「真正缩小段长」才清零重试计数；同一上限反复拒绝累计到 3 次即 `failed` |
| R07 | P2 | 勾选账号与课程/测速账号不一致 | 新增 `currentAccount()`（优先勾选的已登录账号）；切换勾选会清空并重载课程；`loadCourses` / `bench` 都用它 |
| R08 | P2 | `planned` 解析失败后 GUI 卡在 running | `planned`/`lanes` 全部在 `begin("running")` **之前**解析校验；失败不改变状态 |
| R09 | P2 | realtime 取消漏记未完成任务 | 取消时在跑任务与排队任务都生成 `pending` 结果并 `_record` |
| R10 | P2 | tick 等待不可中断、`--no-window` 不走清理 | `cancel.wait(timeout)` 可中断等待（ticker/realtime 均接入）；`--no-window` 分支走同一 `_shutdown()` |
| R11 | P2 | 多账号进度分母取首账号 | 启动前按账号逐个 `/api/plan` 取任务数，`planned` 改为 `{账号: 数量}`；后端兼容单个数字 |

## 验证

- `python -m ruff check guet run.py main.py start.py --select F821,F822,F823` → **All checks passed**（此前 3 处未定义变量）。
- `python -m unittest discover -s tests -q` → **106 项通过**（新增 `tests/test_review_cb62483.py` 7 项，覆盖 R03/R04/R05/R06/R08/R09/R10）。
- R05 实测：`server.calls == [(1, 0, 10), (1, 100, 200)]`，`clock.t == 110`。

## 已知未处置

- R11 提到「计划请求未携带目标百分比」：`/api/plan` 现在会收到 `target`，但 planner 的达标判定仍以 `completeness == full` 为准，因此目标 < 100% 时进度分母略偏大（保守）。要做到完全一致需要让 planner 感知 target，属于行为调整，留待后续。
- 报告提到的「末尾断言退化为自比较」已在 `test_fragmented_ranges_only_fill_gaps` 中改为校验 `covered_seconds`。
