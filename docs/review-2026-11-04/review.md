# 项目完整代码评估

评估日期：2026-11-04。代码基线：`c6e66a0`。本次只审查及生成离线验证材料，未修改业务源码、未提交代码、未调用学校服务写入进度。

**结论：此前 CLI 与运行锁的修复总体有效；当前主要风险已转移到新增 GUI、取消流程和异常收尾。建议先处理下列 6 项 P1，再发布新版本。** 共记录 23 项可执行修复建议：6 项 P1、17 项 P2；另列维护优化及尚需真实环境确认的事项。这里的优先级依据触发后的影响，不代表每项都会在默认配置下发生。

P1：正常操作即可造成核心功能不可用、执行范围不符预期，或任务不能自行结束。P2：特定输入、异常、多账号或部署场景中的正确性与可靠性问题。

## 范围与验证

| 范围 | 本次覆盖 |
|---|---|
| 入口 | `main.py`、`run.py`、`start.py`，命令行解析与覆盖优先级 |
| GUI | 本地 HTTP 服务、账户管理、前端动作、进度展示、退出流程 |
| 核心 | 配置、规划、普通/realtime/tick 执行器、取消、状态、缓存、结果报告 |
| 网络与资源 | 客户端、传输、会话、浏览器生命周期、限速、重试、埋点 |
| 其他 | 向导、性能估算、探针、HAR 工具、离线演示 |
| 发布 | PyInstaller spec、构建/签名脚本、Inno Setup、GitHub Actions |
| 测试 | 现有全部测试、临时目录与假客户端复现、本地回环 HTTP 请求 |

验证结果：

- `python -m unittest discover -s tests -q`：**83 项通过**。
- 37 个受 Git 跟踪的 Python 文件完成语法编译检查；截图工具文档字符串有一个无效转义 `SyntaxWarning`，未影响编译。
- [verify_review.py](E:/大学/guetshuake/docs/review-2026-11-04/verify_review.py) 保存复现过程；[evidence.json](E:/大学/guetshuake/docs/review-2026-11-04/evidence.json) 保存输出。可在项目根目录执行 `python docs/review-2026-11-04/verify_review.py`。它是**当前缺陷证据脚本**，断言当前错误行为；修复后应将这些场景转成断言正确行为的回归测试。
- HTTP 验证仅访问临时启动的 `127.0.0.1` 服务；Cookie 检查仅构造请求，没有发送给外部域名。
- 没有使用真实账号登录、执行真实课程任务、启动安装包或进行浏览器点击测试。浏览器生命周期和发布问题以代码路径分析为依据；不把静态推断写成实机结果。
- 未将 HAR 原件、账号密码、已有 profile、真实 Cookie 纳入输出；Inno 的第三方语言文件、图标和截图不属于业务逻辑逐行审查范围。

## 优先修复：P1

### F01 停止按钮的方法与后端路由不匹配【已复现】

位置：[index.html:377](<E:/大学/guetshuake/guet/gui/web/index.html:377>)、[server.py:497](<E:/大学/guetshuake/guet/gui/server.py:497>)。

`stop()` 调用 `api('/api/stop')` 时没有传 body，公共 API 函数选择 GET；后端只在 POST 路由表中注册停止。用有效 token 请求 GET 得到 **404**，取消标记仍为 false；相同地址使用 POST 才能取消。

建议：前端明确 POST，例如传 `{}`；增加从前端调用约定到后端路由的契约测试，覆盖开始、停止、退出。

### F02 GUI 进度回调签名不一致，所有完成事件被吞掉【已复现】

位置：[server.py:341](<E:/大学/guetshuake/guet/gui/server.py:341>)、[scheduler.py:210](<E:/大学/guetshuake/guet/scheduler.py:210>)、[runner.py:697](<E:/大学/guetshuake/guet/runner.py:697>)。

GUI 定义 `on_result(account_name, result)`；scheduler 原样传给 runner，后者仅调用 `self.on_result(res)`。产生的 `TypeError` 被 `except Exception: pass` 吞掉。已模拟一次成功任务：`events=[]`，done/failed/pending 都是 0。

建议：在 scheduler 按账号包装回调，或统一为一个带账号的事件类型；回调失败至少写调试日志。测试需要通过 scheduler→runner→GUI 的真实调用链，而非分别测试函数。

### F03 添加非法账号先写磁盘再校验，导致整个配置不可用【已复现】

位置：[server.py:129](<E:/大学/guetshuake/guet/gui/server.py:129>)。

`add_account()` 只检查空值和重复名，随后写入 YAML；`reload()` 验证失败时把 `state.app` 置为 None，却不回滚也不抛错。输入 `bad name` 后，无效账号已落盘，原本有效的账号列表也不再显示。前端仍可能弹出“已添加”，界面又没有删除账号入口。

建议：先构造候选配置并完成全部验证，再原子替换文件和内存配置；失败保持旧配置。文件读改写和账户修改操作也应加锁，防止并发请求覆盖彼此。

### F04 GUI 没有账号选择，启动时隐式执行所有“已登录”账号【代码路径确认】

位置：[index.html:316](<E:/大学/guetshuake/guet/gui/web/index.html:316>)、[index.html:361](<E:/大学/guetshuake/guet/gui/web/index.html:361>)。

课程列表取第一个有 profile 的账号，启动却将全部 `logged_in` 账号发送给后端，且未过滤 `enabled:false`。界面没有对应的账号勾选控件。用户浏览 A 的课程并点击开始时，B 也可能被执行同一组课程；不同账号课程不一致时还会产生权限/课程不存在错误。

建议：显式选择账号，默认只执行当前查看账号；禁用账号不得自动选中。多账号课程交集或逐账号映射应单独处理，并在开始前展示实际执行范围。

### F05 运行状态没有互斥，登录和再次启动会破坏取消流程【部分已复现，竞态为静态确认】

位置：[server.py:196](<E:/大学/guetshuake/guet/gui/server.py:196>)、[server.py:308](<E:/大学/guetshuake/guet/gui/server.py:308>)。

`api_start()` 只拒绝 running，允许 stopping 和 logging-in；它随后调用全局 `cancel.reset()`。已复现 stopping 状态下新启动把取消标记清除。`api_login()` 无论原状态是什么，结束后都设 idle；已复现运行期间登录使状态变成 idle。前端运行期间仍能点击登录。状态检查与赋值也没有在同一把锁内，HTTP 服务又是多线程。

建议：用锁保护状态转换，只有 idle 可以启动/登录；每次运行使用独立取消令牌和 run ID。旧 worker 的 finally 只能结束自己的运行，不能覆盖新运行状态。账号运行锁只能阻止部分重复执行，不能修复共享状态和取消标记问题。

### F06 完整区间但不足目标百分比时，补报可能无限重试超长区间【已复现】

位置：[runner.py:192](<E:/大学/guetshuake/guet/runner.py:192>)、[runner.py:229](<E:/大学/guetshuake/guet/runner.py:229>)。

当服务端返回 `ranges=[[0,300]]`、百分比 99、目标 100 时，`_replan()` 的兜底直接生成 `(0,300)`，绕过段长限制。服务端拒绝并提示 maximum 125 后，代码把 stalled 清零，再次生成同一段。连续执行 8 步，全部请求都是 `(0,300)`，failed=false、stalled=0；状态没有向终止条件推进。

建议：兜底也必须按上限切段；无进展/重复拒绝使用独立且有界的重试预算，不因再次识别同一上限而清零。对区间和百分比不一致应先复查，再明确报错，不能无限补报。

## 正确性与可靠性：P2

### F07 GUI 参数绕过统一配置验证【已复现】

位置：[server.py:308](<E:/大学/guetshuake/guet/gui/server.py:308>)。

`api_start()` 直接写入 speed、target、lanes，不调用统一验证。模拟 `target=101`、`speed='invalid'` 被接受；浏览器 number 输入的 min/max 也不会自动阻止此处 JS fetch，因没有表单提交校验。错误 speed 还可能被执行器静默回落为 normal。

建议：对请求 schema、正整数课程 ID、目标范围、速度枚举、路数做统一校验；先在配置副本上验证再启动，不先污染 `state.app`。

### F08 取消未记录未完成任务，等待不可中断【已复现 + 代码路径确认】

位置：[ticker.py:102](<E:/大学/guetshuake/guet/ticker.py:102>)、[ticker.py:143](<E:/大学/guetshuake/guet/ticker.py:143>)、[runner.py:623](<E:/大学/guetshuake/guet/runner.py:623>)、[realtime.py:247](<E:/大学/guetshuake/guet/realtime.py:247>)。

取消分支直接 break，未把 active/pending 任务转为取消或待续结果。假服务端已收到 0–100 秒区间，但 tick 返回 results=0、records=0，和“进度已写入 state”的日志不符。服务端进度仍在，因此不是必然丢失远端进度；丢的是本地任务状态与汇总。scheduler 对空结果执行 all()，还可能显示全部成功。tick 默认一次 sleep 120 秒；realtime 最多一次等待 60 秒；资料内部循环没有逐附件取消检查。

建议：取消作为明确终态，记录未完成项；使用 `Event.wait(timeout)` 等可唤醒等待，在任务/请求边界检查取消。课程间及激活队列也应检查。保留 tick_once 的正常 pending 语义，避免将单轮执行误报为失败。

### F09 后续课程异常会丢失之前完成课程的汇总【已复现】

位置：[scheduler.py:219](<E:/大学/guetshuake/guet/scheduler.py:219>)、[runner.py:479](<E:/大学/guetshuake/guet/runner.py:479>)。

scheduler 要等 `runner.run()` 返回整份字典才复制结果。第二门课抛异常时，第一门课即使已完成也没被复制。模拟第一门完成、第二门失败，最终 outcome.results={}、done=0。磁盘上已记录的活动不一定丢失，但报告会漏报。

建议：逐课程执行并立即累计结果，或 yield 课程结果；异常路径也保存请求计数和已完成结果。

### F10 GUI 丢弃最终 outcome，不保存结果与账号错误【已复现】

位置：[server.py:359](<E:/大学/guetshuake/guet/gui/server.py:359>)。

worker 调用 `scheduler.run_all()` 后不使用返回值，不写 result.json，也不更新 state.error。模拟 run_all 返回失败 outcome，界面状态仅变 idle，error 仍为空。大量账号失败已在 scheduler 内捕获，不会触发 GUI 外层 except。日志可作为线索，但不是明确的最终结果。

建议：保存 outcomes、更新每账号最终状态和错误、原子写结果文件，区分完成/失败/取消；重新打开页面能查看上次运行结果。

### F11 进度条分母不是任务总量【代码路径确认】

位置：[index.html:255](<E:/大学/guetshuake/guet/gui/web/index.html:255>)、[server.py:338](<E:/大学/guetshuake/guet/gui/server.py:338>)。

分母为已收到事件的 done+failed+pending，初始化全部为 0，没有计划总数。修复 F02 后，只要第一个事件成功就显示 100%，即使还有几十个活动未完成。

建议：启动时为每个账号记录计划任务总数，计算终态任务数/总数；活动完成率与服务端课程完整度分别展示。

### F12 GUI 退出没有统一等待与清理生命周期【代码路径确认，未实机点击验证】

位置：[app.py:125](<E:/大学/guetshuake/guet/gui/app.py:125>)、[server.py:418](<E:/大学/guetshuake/guet/gui/server.py:418>)。

没有 Edge 时启动器进入无条件 while True；页面 quit 只关闭 HTTP 服务，不会结束这个循环。在有 Edge 时依赖 window.wait()；关闭窗口后的收尾只等待 0.3 秒，worker 又是 daemon，不能保证已等待保存/浏览器关闭完成。重复启动时 Edge 还可能把窗口交给已有进程，使新 Popen 退出不等于窗口退出，此点需实机验证。

建议：统一应用 shutdown Event，退出时停止接收新任务、取消当前运行、等待 worker 有界收尾、关闭 transports 和 server，再结束进程。默认浏览器分支至少监听 server 线程或退出事件。

### F13 课程缓存缺少站点和子课程维度【已模拟复现】

位置：[scheduler.py:44](<E:/大学/guetshuake/guet/scheduler.py:44>)、[client.py:197](<E:/大学/guetshuake/guet/client.py:197>)。

活动接口受 base_url、sub_course_id 影响，缓存却只用 cid。两份配置共享 state_dir，切换站点/子课程后，同 cid 仍复用 TTL 内旧活动。模拟第二个客户端请求同 cid，直接得到第一个客户端数据。

建议：缓存键至少包含规范化 base_url、course_id、sub_course_id，并增加格式版本。是否还需账号/班级维度，应根据真实 API 是否返回差异化活动确认。

### F14 账号名验证不符合 Windows 路径语义【已复现部分】

位置：[config.py:393](<E:/大学/guetshuake/guet/config.py:393>)。

正则允许 `.`、`..`；已验证账号 `..` 的 profile 路径解析到应用根目录。Windows 上大小写不同的名字、尾点名称和保留设备名也未处理，可能让逻辑上的不同账号使用同一实际目录，或无法创建文件。

建议：拒绝点路径、尾点和 Windows 保留名，按实际平台路径规范判断唯一性，并验证 resolved profile 路径严格位于 profile 根目录下。

### F15 CLI 覆盖后的验证仍接受非有限数【已复现】

位置：[run.py:141](<E:/大学/guetshuake/run.py:141>)、[config.py:353](<E:/大学/guetshuake/guet/config.py:353>)。

YAML 的 `_to_float` 已拒绝 nan/inf，但 argparse type=float 仍接受它们；后续 validate 只比较下限。`--rps inf` 覆盖后通过验证，失去预期速率限制。`--eta inf`、`--interval nan` 等还可能在 int 转换时直接抛出异常。

建议：有限性与范围检查放在统一验证层，供 YAML、CLI、GUI 共同使用；转换异常统一为 ConfigError。直接配置 gap_min/gap_max 目前也未走 `_to_float`，应一并覆盖。

### F16 `--per-account-jobs` 未覆盖账号级设置【已复现】

位置：[run.py:143](<E:/大学/guetshuake/run.py:143>)、[runner.py:604](<E:/大学/guetshuake/guet/runner.py:604>)。

命令行只改 runtime 值，没有像 speed/target 一样清除账号覆盖。账号值为 8，CLI 指定 1 后，实际表达式仍选 8，会影响请求间隔。

建议：统一所有可覆盖字段的优先级：CLI > 账号 > 全局；测试使用与账号配置冲突的 CLI 值。

### F17 `start.py --yes` 仍然提问【已复现】

位置：[wizard.py:410](<E:/大学/guetshuake/guet/wizard.py:410>)、[wizard.py:441](<E:/大学/guetshuake/guet/wizard.py:441>)。

assume_yes 只影响 ask_yes，两账号选择、课程、节奏等仍使用 safe_ask。离线模拟 assume_yes=True，仍收到账号选择 prompt。在有控制台但无人响应时会阻塞，与参数承诺不同。

建议：由统一输入适配层在 assume_yes 下返回明确默认值；测试应让 ask 一旦被调用就失败，不能仅用空答案队列代替真人。

### F18 向导显式指定不存在的账号文件会回落到默认账号【已复现】

位置：[wizard.py:431](<E:/大学/guetshuake/guet/wizard.py:431>)。

传入 missing-accounts.yaml 时，向导使用 ensure_accounts_file(root) 返回的 accounts.yaml。已模拟确认。与 run.py 严格报错的行为不一致，可能执行另一组账号。

建议：显式指定的 accounts 路径不存在就报错；仅省略参数时创建默认文件。账号选择输入错误当前也会回落全部账号，建议重问而非扩大选择范围。

### F19 向导的浏览器预检没有适配 Edge/Chrome 回退【代码路径确认】

位置：[wizard.py:149](<E:/大学/guetshuake/guet/wizard.py:149>)、[browser.py:161](<E:/大学/guetshuake/guet/browser.py:161>)。

实际启动已支持系统 Edge→Chrome→Chromium；browser_ready() 仍只检查 resolve_chromium 得到的文件。只有 Edge、未下载 Chromium 的机器，会在向导入口被提前拒绝，即使真实启动器能够工作。

建议：预检与 launch_context 共用候选通道选择逻辑；针对只有 Edge、只有 Chrome、仅 bundled 和全部缺失的环境做 mock 测试。

### F20 Cookie 导出丢失 domain/path/secure 作用域【仅离线请求构造复现】

位置：[session.py:40](<E:/大学/guetshuake/guet/session.py:40>)、[transport.py:166](<E:/大学/guetshuake/guet/transport.py:166>)。

浏览器 Cookie 被压缩为 name→value，导入 Requests 时没有域和路径。构造发往 unrelated.invalid 的请求时，假 session Cookie 也出现在 Cookie 头中；未真的发送请求。实际遇到跨域重定向时存在非预期携带风险，同名不同路径的 Cookie 也会互相覆盖。域筛选使用字符串 endswith，没有完整域边界判断。

建议：保留完整 Cookie 属性建立 CookieJar；使用浏览器对指定 URL 的 Cookie 查询或规范域匹配。验证跨域、同名不同路径、secure、过期和重定向场景。不能仅根据“当前接口都在一个站点”忽略作用域丢失。

### F21 课程发现固定取第一页【代码路径确认，触发需超过一页】

位置：[client.py:167](<E:/大学/guetshuake/guet/client.py:167>)。

my_courses 固定 page=1、page_size=50，没有分页循环；GUI、向导和自动发现都调用它。超过一页时静默漏课程。

建议：按 API 的总数/下一页字段分页，或以不足 page_size 作为停止条件，并防止服务端异常重复页造成无限循环。

### F22 正式发布缺少失败阻断条件【静态审查，未执行构建/签名】

位置：[build.ps1:65](<E:/大学/guetshuake/packaging/build.ps1:65>)、[build.ps1:87](<E:/大学/guetshuake/packaging/build.ps1:87>)、[sign.ps1:128](<E:/大学/guetshuake/packaging/sign.ps1:128>)。

full 缺少浏览器仅警告，仍可能产出标为 full 的包；显式要求签名但失败也只警告继续；签名脚本无论 pfx/selfsigned 都把 UnknownError 计为成功。CI 可以继续发布这种产物。此外目前 CI 运行源码单元测试，没有验证打包后的 GUI 能启动、资源齐全及停止/退出行为。

建议：正式发布要求 full 内核存在；pfx 签名必须验证成功，开发自签名策略单独明确；失败退出非零。打包后用临时数据目录做启动、健康检查、退出 smoke test。缺真实证书时不要把自签名视为系统信任的替代。

### F23 realtime 补缺口时重报已覆盖区间【已复现】

位置：[realtime.py:75](<E:/大学/guetshuake/guet/realtime.py:75>)、[realtime.py:168](<E:/大学/guetshuake/guet/realtime.py:168>)。

初始化 cursor 取最后一个已看区间的终点；重排只保留第一个缺口起点，随后持续向视频末尾推进，没有跳过之后的已覆盖区间。1000 秒视频、已看 `[0,100] [200,800] [900,1000]` 时仅剩两个 100 秒缺口，模拟却发出 **8 次请求，耗时 900 秒**。

建议：保存缺口区间队列，逐缺口推进并在补齐后跳到下一缺口；时间安排按实际待处理段长计算。补充碎片化区间、已覆盖尾部和不足一个 chunk 的最后一段测试。

## 可后置的维护与性能改进

1. **请求计数统一。** outcome.requests 取 runner.request_count，主要统计业务写入尝试，没有覆盖读取、重试、埋点。应分开显示总 HTTP 请求、业务写入和重试，避免用偏小数字判断负载。
2. **避免重复读取。** build_plan 已批量取得 activity reads，随后 prepare 又逐活动读取；可在保持数据新鲜度前提下复用初始快照，只对执行前必要场景和结果复查读取。先测量请求比例，再优化。
3. **持久化一致性。** report.write_result 仍是直接 write_text；多个不同账号进程共享 result.json 会最后写入覆盖。课程缓存按课程加锁解决了网络串行，但 `_data` 修改未统一纳入序列化锁。建议每次运行独立报告、原子写入，并在短锁内复制缓存快照后再写盘。
4. **单次埋点失败不应永久关闭整个账号的埋点。** `_settings` 短暂失败会返回空字典，video/material 将 enabled=false；后续课程也不再重试。可按课程设置短暂退避。请求日志中的 JWT 查询参数应脱敏；本次没有检查或输出真实 token。
5. **时间估算区分模式。** wizard 的 tick 走普通写入/限速估算，没有计入轮次间隔；realtime 估算采用完整视频时长而非剩余缺口。GUI 固定“约几分钟”文案也不是当前任务的测量结果。建议统一基于剩余任务模型。
6. **错误反馈保持一致。** schedule --delete 忽略 schtasks 返回码；probe 对会话不可用等抛 RuntimeError，CLI 未统一捕获。GUI 的请求 body 类型、Content-Length 和 logs.after 的非法值还需返回明确 400；当前部分转换发生在异常捕获外。
7. **发布文案与产物一致。** installer 的“开机托盘静默运行”没有对应托盘实现；CI 仅上传 exe，未上传构建生成的 SHA256SUMS 和对应自签名公钥。明确 full/lite 是同一 AppId 的替换安装，而非可并存产品。
8. **测试可重复性。** 缓存并发测试用 `<0.45s` 断言可能受慢 CI 影响，宜用 barrier/event 验证重叠执行。构建依赖仅设下界，缺少锁定版本；GUI、取消、打包生命周期缺少持续回归测试。截图脚本注入演示数据只验证展示效果，不能替代真实状态流验证。
9. **小型清理。** browser.launch_context 抛异常后的两行不可达；若保留 HAR 导出工具，应说明硬编码 entry 索引仅适用于特定抓包，并对越界和 base64 content 做提示。Pillow、PyInstaller 等开发依赖可单独列出。

## 仍需真实环境确认

- 服务端是否真的每次响应轮换 session，以及 HTTP 会话更新是否需要同步回 profile。现有代码不把 Requests 的更新 Cookie 写回浏览器；重建是否失效取决于服务端策略，不能仅凭注释断言必现。
- 同一课程在不同账号/教学班/权限下是否返回不同活动集合；若是，缓存还需要加入可见性维度。
- browser 模式长任务、Edge 已有进程、多次启动 GUI、关闭窗口时的子进程回收；这些需要隔离 profile 做实机测试。
- full/lite 安装、升级、卸载保留数据、正式签名时间戳与 Windows 信任结果。本次未修改注册表、安装包或证书。
- 上游返回结构异常、网络中断、403/429/5xx、代理与重定向组合；本次测试证明本地控制流，不证明学校当前服务端行为。

## 建议实施顺序与验收

1. **GUI 基本链路：** 修 F01/F02/F03/F04/F05/F07。验收：显式选择一个账号只运行该账号；成功事件能出现；非法输入不落盘；停止请求确实取消；运行/停止中不能重登或启动第二个任务。
2. **执行与结果：** 修 F06/F08/F09/F10/F11/F12/F23。验收：任何无进展循环有上限；取消保留待续项；后续课程失败仍保留前序结果；退出等待收尾；碎片缺口只处理缺失段。
3. **输入与隔离：** 修 F13–F20。验收：不同入口同样拒绝非法参数；账号路径不越界且无 Windows 名称冲突；显式路径拼错不回退；Cookie 不发送到无关域。
4. **规模与发布：** 修 F21/F22，完善打包 smoke test 和依赖约束，再做真实环境验证。

本次已完成上述范围的静态代码审查、现有测试和可安全执行的离线复现。“全部评估完整”在此指覆盖现有模块并给出完整发现清单，不等于证明软件没有其他缺陷；未执行的真实环境项目已明确列出。
