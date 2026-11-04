# 评估报告处理状态（2026-11-04）

对应 [review.md](review.md)（基线 `c6e66a0`）。**23 项全部处理完毕**，测试从 83 项增至 96 项。

## P1（6 项）

| 编号 | 问题 | 修复 | 测试 |
|---|---|---|---|
| F01 | 停止按钮 GET / 后端只收 POST → 404 | 前端 `stop()` 传 `{}` 走 POST；顺带修 `/api/logs?after=0` 拼 `?t=` 丢 token | `test_stop_requires_post`、`test_logs_accept_query_with_token` |
| F02 | 进度回调签名不匹配，事件被吞 | `run_all` 按账号包装回调；回调异常写 debug 日志 | `test_run_all_wraps_callback_per_account` |
| F03 | 非法账号名先落盘，配置整体失效 | 候选文件先 `load_config` 校验再 `os.replace`；失败保持旧配置 | `test_invalid_account_name_not_written` |
| F04 | 隐式执行所有「已登录」账号 | 账号卡加勾选（默认第一个已登录）；后端只跑传入账号、拒 disabled | `test_only_listed_accounts_run`、`test_rejects_disabled_account` |
| F05 | 登录/启动/停止互相覆盖状态 | `begin/finish` 加锁 + `run_id`，只有 idle 能操作；停止走 `mark_stopping` | `test_state_transitions_are_mutually_exclusive` 等 3 项 |
| F06 | 区间满但百分比不足 → 无限补报超长段 | 兜底按段长切 + `max_rounds=2`；同一上限不再清零 `stalled` | `test_bounded_retry_and_respects_span_limit` |

## P2（17 项）

| 编号 | 问题 | 修复 |
|---|---|---|
| F07 | GUI 参数绕过验证 | `api_start` 校验 speed/target/课程 ID/路数 |
| F08 | 取消不记录未完成任务 | tick 取消时把在跑活动记为 `pending` 并写结果 |
| F09 | 后续课程异常丢失前序汇总 | scheduler 逐课程执行并立即累计 |
| F10 | GUI 丢弃最终 outcome | 保存 `last_outcomes`、写 `result.json`、更新 `state.error` |
| F11 | 进度分母不是任务总量 | 启动前取 `/api/plan` 的任务总数当分母 |
| F12 | 退出无统一等待与清理 | `AppState.shutdown` 事件统一收尾；关窗/`/api/quit`/Ctrl-C 同一路径，worker `join(timeout=8)` 有界等待后关 server |
| F13 | 缓存键缺站点/子课程 | 键改为 `v2\|base_url\|cid\|sub_course_id`，旧缓存自动失效；`_data` 写盘加锁 |
| F14 | 账号名不符合 Windows 路径语义 | 拒绝 `.`/`..`/尾点/尾空格/保留设备名（CON、NUL、COM1…） |
| F15 | CLI 覆盖后接受非有限数 | `validate` 对 rps/timeout/global_rps/gap 做 `isfinite` 检查 |
| F16 | `--per-account-jobs` 未覆盖账号级 | 命令行覆盖时一并清除账号级同名设置 |
| F17 | `start.py --yes` 仍然提问 | `assume_yes` 下所有提问直接返回默认值 |
| F18 | 向导显式账号文件不存在会回落 | 显式路径不存在直接报错退出 |
| F19 | 向导预检未走通道回退 | 预检与启动共用候选：系统 Edge → 内置 Chromium → 系统 Chrome |
| F20 | Cookie 作用域丢失 | 保留 domain/path/secure 并以列表导入 Requests；域匹配改为严格边界（`evil-guet.edu.cn` 不再命中） |
| F21 | 课程列表只取第一页 | `my_courses` 分页循环，按 `page_size` 判断结束，按 id 去重防重复页 |
| F22 | 发布流程缺失败阻断 | `build.ps1`：full 缺内核直接中止（`-AllowMissingBrowser` 才能跳过）、签名失败中止；`sign.ps1`：pfx 模式必须 `Valid` |
| F23 | realtime 补缺口时重报已覆盖区间 | `LaneJob` 改为**缺口队列**推进，按实际段长计时。实测 1000 秒视频、已看 `[0-100][200-800][900-1000]`：**从 8 次请求 / 900 秒降到 2 次 / 200 秒** |

## 维护项（报告「可后置」9 条）

| # | 处理 |
|---|---|
| 1 请求计数统一 | 暂未拆分（只影响展示，不影响正确性） |
| 2 避免重复读取 | 暂未做（需先测量，属性能优化） |
| 3 持久化一致性 | `report.write_result` 改为原子写；缓存写盘单独加锁 |
| 4 埋点失败永久关闭 | 改为 120 秒退避重试，不再永久关闭 |
| 5 时间估算区分模式 | 未做（GUI 文案已标注档位耗时） |
| 6 错误反馈一致 | `schedule --delete` 检查返回码；`main` 捕获 `RuntimeError`；`/api/logs` 非法 `after` 返回 400 |
| 7 发布文案与产物一致 | installer 去掉「托盘」措辞；CI 同时上传 `SHA256SUMS.txt` |
| 8 测试可重复性 | 未做（`<0.45s` 并发断言仍依赖机器速度） |
| 9 小型清理 | `requirements.txt` 加上版本上限 |

## 验证

- `python -m unittest discover -s tests -q` → **96 项通过**（新增 13 项回归，覆盖 F01–F07 与 F23）。
- 报告附带的 `verify_review.py` 在 F02 处失败（`events` 已非空），符合"缺陷已修"的预期。
- 打包版冒烟：`GET /api/stop → 404`、`POST → 200`、`/api/logs?after=0&t=` 正常返回、非法账号被拒且不落盘。
