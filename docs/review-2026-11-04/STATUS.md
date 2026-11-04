# 评估报告处理状态（2026-11-04）

对应 [review.md](review.md)（基线 `c6e66a0`）。修复提交：见仓库 `git log --grep=review`。

## P1（6 项）—— 全部已修复，并转成回归测试

| 编号 | 问题 | 修复 | 测试 |
|---|---|---|---|
| F01 | 停止按钮 GET / 后端只收 POST → 404 | 前端 `stop()` 改传 `{}` 走 POST；顺手修了 `/api/logs?after=0` 再拼 `?t=` 导致 token 丢失 | `test_stop_requires_post`、`test_logs_accept_query_with_token` |
| F02 | 进度回调签名不匹配，事件被 `except: pass` 吞掉 | `scheduler.run_all` 按账号包装回调；回调异常改为写 debug 日志 | `test_run_all_wraps_callback_per_account` |
| F03 | 非法账号名先落盘，整个配置不可用 | 候选文件先校验再 `os.replace` 原子替换；失败保持旧配置，临时文件清理 | `test_invalid_account_name_not_written` |
| F04 | 崩溃前隐式执行所有「已登录」账号 | 左侧账号卡加勾选（默认只勾第一个已登录）；后端只执行传入账号，禁用账号直接拒绝 | `test_only_listed_accounts_run`、`test_rejects_disabled_account` |
| F05 | 登录/启动/停止互相覆盖状态与取消标记 | `AppState.begin/finish` 加锁 + `run_id`：只有 idle 能启动/登录，旧 worker 收尾不会覆盖新状态；停止走 `mark_stopping` | `test_state_transitions_are_mutually_exclusive`、`test_old_run_cannot_reset_new_status`、`test_stop_marks_stopping` |
| F06 | 区间已满但百分比不足 → 无限重试超长段 | 兜底补报按段长切分，并加 `max_rounds=2`；同一上限被反复拒绝不再清零 `stalled`，超过阈值直接失败 | `test_bounded_retry_and_respects_span_limit` |

## P2 —— 已修复

| 编号 | 问题 | 修复 |
|---|---|---|
| F07 | GUI 参数绕过配置验证 | `api_start` 校验 speed/target/课程 ID/路数，复用 `SPEEDS`、`_check_lanes` |
| F08 | 取消不记录未完成任务，汇总看起来全成功 | tick 取消时把在跑活动记为 `pending` 并写入结果 |
| F09 | 后续课程异常丢失前序汇总 | `scheduler` 改为逐课程执行并立即累计，异常时保留已完成结果 |
| F10 | GUI 丢弃最终 outcome | worker 保存 `last_outcomes`、写 `result.json`、失败时更新 `state.error` |
| F11 | 进度条分母不是任务总量 | 启动前先取 `/api/plan` 的任务总数当分母，事件累计只作兜底 |
| F14 | 账号名不符合 Windows 路径语义 | 拒绝 `.`/`..`/尾点尾空格/保留设备名（CON、NUL、COM1…） |
| F15 | CLI 覆盖后仍接受非有限数 | `validate` 对 rps/timeout/gap 做 `isfinite` 检查，`--rps inf` 直接被拒 |
| F16 | `--per-account-jobs` 未覆盖账号级 | 命令行覆盖时一并清除账号级同名设置 |
| F17 | `start.py --yes` 仍然提问 | `assume_yes` 下所有提问直接返回默认值，不再阻塞 |
| F18 | 向导显式指定不存在的账号文件会回落 | 显式路径不存在直接报错退出 |

## P2 —— 未处理（保留在报告里）

F12（GUI 退出生命周期）、F13（课程缓存键缺站点/子课程维度）、F19（向导预检未走通道回退）、
F20（Cookie 作用域丢失）、F21（课程列表只取第一页）、F22（发布流程缺失败阻断）、
F23（realtime 补缺口时会重报已覆盖区间），以及「可后置的维护改进」9 条。

这些不影响默认路径的可用性，按报告建议放在下一轮；F23 与 F12 会优先于其余项。

## 验证

- `python -m unittest discover -s tests -q` → **95 项通过**（原 83 + 新增 12 项 P1 回归）。
- 报告附带的 `verify_review.py`（断言旧缺陷）现在在 F02 处失败，因为 `events` 已非空 —— 符合预期。
