# 最新提交审查：97cb993

基线：本地 HEAD `97cb993070f30a6d3204941cc03870f0f82a527e`（2026-11-04）。本次没有拉取远程；“最新版”指当前本地提交。开始时工作区干净，审查覆盖 `cb62483..HEAD` 的运行时代码变更，重点为客户端复用、课程构成统计、取消与上一轮问题回归。

结论：7 项问题，1 项 P1、6 项 P2。只新增此报告，未修改业务代码和测试。

## 1. [P1] 课程统计在线程池中调用同一个 Playwright 客户端

位置：`guet/gui/server.py:381`，`guet/transport.py` 的 `BrowserTransport._send()`。

`api_courses()` 在 HTTP 请求处理线程创建客户端，再把同一个 client 传给最多三个课程统计线程。browser 引擎使用同步 Playwright，其上下文只能由创建它的线程调用。显式 browser 配置，以及 HTTP 被 WAF 拒绝后自动切换到 browser，都会进入这条路径。

验证：本机启动 Playwright APIRequestContext（没有启动浏览器、没有访问学校服务），从另一个线程调用请求方法，直接得到 `error: Cannot switch to a different thread`。代码调用链确认新增线程池正是这种使用方式；本轮未进行学校服务端到端联调。

影响：这些账号的课程构成统计失败，随后被异常处理改写为零（问题 4）。默认 HTTP 路径也把有可变 Cookie 的同一个 requests.Session 用于并行请求，不能将浏览器引擎的线程限制忽略为一般网络并发。

建议验收：browser 统计在拥有该上下文的线程串行执行，或由固定拥有者线程统一调度；覆盖显式 browser 和 HTTP 自动回退两种路径。

## 2. [P2] 客户端刚入缓存就被调用方关闭，缓存生命周期不成立

位置：`guet/gui/server.py:289-303`；`api_courses/api_plan/api_bench` 的 finally。

首次 `_client_for()` 把 client/transport 写入缓存，却返回真实 transport；调用方 finally 随即 close。只有缓存命中时才返回 `_NoopClose`，无法撤销首次关闭。后续请求探测的是已经关闭过的 transport。

离线 Mock 复现：首次获取后按 API 约定 close，再次获取；原 transport 已关闭 1 次，仍返回同一个 client 和 `_NoopClose`。Mock 用于证明所有权流程，不代表真实 browser 会通过探测。

实际区别：requests.Session.close 通常允许后续重新建立连接，因此不能声称 HTTP 每次必崩；但连接池复用已被破坏。browser 上下文关闭后不可复用，后续探测失败会重新启动浏览器，缓存未达到“少起浏览器”的目的。缓存超过 TTL 时还会直接覆盖旧条目，没有统一关闭；退出函数也未统一清理缓存。

建议验收：明确缓存持有资源、请求借用资源的所有权，覆盖首次调用、命中、过期、重新登录、退出；browser 资源同时满足线程归属要求。

## 3. [P2] 账号锁只保护获取客户端，没有保护使用过程

位置：`guet/gui/server.py:289`、`:303`、`:265`。

`_client_for()` 返回即释放 account_lock，课程读取、计划和测速在锁外执行；`api_login()` 可以随后拿到同一个锁并执行 `drop_client()`，关闭前一个请求正在使用的资源。课程 API 不占用 AppState 的 running/logging-in 状态，因此状态机也没有排除这组并发。

触发场景：刷新课程仍在读取/统计时点击登录，或第二个请求命中缓存后，第一个请求执行 finally.close。结果可能是会话失效、browser 上下文被关闭，或者并发请求使用正在轮换的 Cookie。新增“串行化账号操作”的锁实际只串行化了客户端获取阶段。

此项为控制流审查，未操作用户实际登录会话。建议验收：用阻塞假客户端重叠课程读取和登录，保证在用客户端不会被关闭；启动任务与界面读取也需有明确的账号操作协调规则。

## 4. [P2] 统计失败被伪装成确定的零可刷项

位置：`guet/gui/server.py:383-388`，`guet/gui/web/index.html` 的 `courseTags()`。

任意统计异常（网络失败、会话过期、线程错误等）都会把 todo_video/todo_material/manual/auto 设置为 0。前端据此显示“已无可刷项”或仅显示“待完成”，无法区分统计失败和真正没有自动任务。原先 None 表示未知，却在失败时被丢弃。

离线复现：Mock `_course_breakdown()` 抛出 RuntimeError，接口仍返回 ok=True，课程 `auto=0`、`manual=0`；平台显示剩余一项时只得到 `other=1`。

建议验收：保留未知状态并提供失败/重试提示，不能把获取失败当成业务上的零值。覆盖暂时网络失败和会话失效。

## 5. [P2] “可刷项”只按类型计数，与实际执行计划不一致

位置：`guet/gui/server.py:314-332`，对照 `guet/planner.py:build_plan()`。

新增 `_course_breakdown()` 只检查类型和 full，遗漏 planner 的未发布、无附件、视频时长不可获取等规则。卡片宣称可自动处理的活动，实际运行会跳过或归为人工。

离线复现：同一组活动包含“无附件 material”和“published=False 的 online_video”，卡片统计为 `todo_video=1, todo_material=1, auto=2`；实际 build_plan 的两个 action 均为 skip，原因分别是“无附件”和“未发布”。

建议验收：课程卡和执行器共用可处理性判定；对无附件、未发布、未知时长视频和人工活动比较两者统计，避免另写一套类型规则。

## 6. [P2] 用户取消仍会被汇总成成功

位置：`guet/scheduler.py:246`。

上一轮已修复课程异常后成功误报，也给 realtime 在跑和排队任务补记了 pending。但账号成功条件仍是“无 error 且没有 failed”，不识别用户取消，pending 仍满足成功条件。

离线复现：置位取消标记，让课程返回 pending 结果，经真实 scheduler.run_one 汇总得到 `ok=True, error=''`。验证后已复位取消标记。GUI 根据 `not o.ok` 判断失败，结果文件也会保留这个成功值。

建议验收：用户取消应有明确终态，不能展示“全部成功”。需要区分主动取消和 tick_once 的预期分批执行，不能简单规定所有 pending 都是失败。

## 7. [P2] 低目标百分比的计划分母仍与执行不一致

位置：`guet/gui/server.py:400-419`；`guet/gui/web/index.html` 的 start()。

前端现在逐账号查询计划，并传 target，但 api_plan 完全没有读取该字段。planner 仍仅将 full 视为完成，describe_plan 直接统计视频/资料数量。实际实时执行在 percent >= target 时跳过，且跳过不会产生对应完成回调。

触发场景：目标 80%，某视频进度已 90% 但尚非 full；计划把它计入分母，执行直接跳过，导致任务结束而进度条未满，极端情况下还会触发“没有可自动处理的活动”的概括提示。

此项为代码路径核对，也是 `STATUS-cb62483.md` 已承认的未处置部分。按账号分母已改进，但不能视为上一轮 R11 完全解决。建议验收：计划采用本次 target/speed 等执行参数，并统一达标跳过项的统计口径。

## 验证结果与回归判断

- `python -m unittest discover -s tests -q`：106 项通过（约 2.5 秒）。
- `python -m ruff check guet run.py main.py start.py --select F821,F822,F823`：通过。仅代表所选未定义名称规则，不代表完整 lint。
- 上一轮 Cookie 导入、实时重新规划变量、课程异常成功误报、数字路数、碎片计时、重复上限重试、planned 状态卡住，均有对应修复；现有回归测试通过。
- 取消记录和可中断等待已改进，但账号取消终态仍有问题 6；按账号查询计划已改进，但低目标计划仍有问题 7。
- 新增缓存和课程统计缺少能暴露资源所有权、跨线程 Playwright、统计错误与执行计划不一致的测试。当前全绿不能覆盖这些路径。
- 离线验证使用 Mock、临时目录以及本机 Playwright API 上下文，没有访问学校服务、上报课程进度或读取真实凭据。Playwright 跨线程复现退出时出现待取消任务清理警告，为该独立验证进程的异常清理输出。
- 未重建安装包、未执行真实账号/浏览器 GUI 联调；本报告不声称全项目不存在其他问题。审查结束时 HEAD 未变化。
