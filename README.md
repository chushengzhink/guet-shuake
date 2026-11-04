# GUET 畅课（TronClass）后台刷课工具

**后台起 Chromium 拿会话，之后直接调接口刷进度，不做任何模拟点击；支持多账号并行 + 断点续跑 + 自动降档。**

接口全部由仓库里那份抓包（`content-autofill.googleapis.com_*.har`，969 条请求）实测反推，不是猜的。

---

## 1. 它到底怎么刷的

TronClass 的视频进度就是一组「已看区间」：

```http
POST /api/course/activities-read/92918
Content-Type: application/json

{"start":0,"end":4}
```

```json
{"activity_id":92918,"completeness":"part","data":{"completeness":1,"ranges":[[0,4]]}}
```

抓包里一个已经刷满的视频，`data` 长这样：

```json
{"completeness":100,"end":743,"ranges":[[0,743]]}
```

也就是说：**把这组区间补到 `[0, floor(时长)]`，进度就是 100%**。资料类活动则按附件计数：

```http
POST /api/course/activities-read/83055
{"upload_id":2764492}
```
```json
{"data":{"completeness":75,"upload_ids":[2764492,2764149,2764487]}}
```

所以本工具做三件事：

| 活动类型 | 处理方式 | 完成标准（抓包实测） |
|---|---|---|
| `online_video`（视频） | 按段 `POST /api/course/activities-read/{id}` 补区间 | `completeness` ≥ 80 |
| `material`（资料/课件） | 逐个 `upload_id` 上报已读 | upload 全部已读 = 100 |
| `homework` / `exam` | **不碰**，只在报告里标"需人工" | 需要真的交作业/考试 |

同时把 `/statistics/api/online-videos?jwt=...` 的播放埋点按真实区间同步一份（教师端"学习时长/行为轨迹"吃这份数据），可用 `--no-telemetry` 关掉。

### 课程事实（抓包当天）

课程 `29066`《基础工业工程（挑战性学习课程）-2610082》：74 个活动 = 60 个 learning + 14 个 exam；
learning 里 `online_video` 30、`material` 18、`homework` 12；当时 `total_completed=22`、`study_completeness=33.0%`。

---

## 2. 接口清单（HAR 反推，全在 `courses.guet.edu.cn`）

| 用途 | 方法 / 路径 | 备注 |
|---|---|---|
| 登录链 | `GET /user/index` → 302 `cas.guet.edu.cn/authserver` → 303 `identity.guet.edu.cn/.../broker/cas-client/login` → 302 `/user/index?ticket=...` | 交给真实浏览器跑 |
| 会话 cookie | `sduuid` / `role_token` / `session` | **每条响应都 Set-Cookie 轮换**，客户端必须自动回写 |
| 会话体检 | `GET /api/config?no-intercept=true` | 未登录时 WAF 直接 403 |
| 课程列表 | `POST /api/my-courses` | `conditions.semester_id=["14"]`、`academic_year_id=["5"]` |
| 学期/学年 | `GET /api/my-semesters`、`GET /api/my-academic-years` | 用于自动发现当前学期 |
| 课程完整度 | `GET /api/course/{cid}/my-completeness` | `completed_result`、`study_completeness` |
| 模块 | `GET /api/courses/{cid}/modules` | 日志分组 |
| 活动列表 | `GET /api/courses/{cid}/activities?sub_course_id=0` | 248 KB，含时长/附件/完成标准 |
| 活动详情 | `GET /api/activities/{aid}?sub_course_id=0` | `data.allow_forward_seeking`、`uploads[].videos[].duration` |
| 已读汇总 | `GET /api/course/{cid}/activity-reads-for-user` | 每个活动 `full`/`part` + `ranges`/`upload_ids` |
| 单活动已读 | `GET /api/course/activity-read/{aid}` | 断点续跑的依据 |
| **视频上报** | `POST /api/course/activities-read/{aid}` `{"start":s,"end":e}` | 整数秒，返回 `data.ranges` / `completeness` |
| **资料上报** | `POST /api/course/activities-read/{aid}` `{"upload_id":uid}` | 返回 `data.completeness` 百分比 |
| 统计埋点 | `POST /statistics/api/online-videos?jwt=<mgsJwt>`、`POST /statistics/api/materials?jwt=` | `jwt` 来自课程页 HTML 的 `window.statisticsSettings.mgsJwt` |
| 取 jwt | `GET /course/{cid}/learning-activity` | 正则抓 `mgsJwt` / `userId` / `orgId` / `departmentId` |
| 课程访问 | `POST /api/course/{cid}/entry/record` | 可选，行为一致性 |

请求头只需 `X-Requested-With: XMLHttpRequest`、`Content-Type: application/json`、`Origin/Referer`；**没有 CSRF token**。

---

## 3. 安装

```powershell
cd E:\大学\guetshuake
python -m pip install -r requirements.txt       # playwright / requests / PyYAML / rich
python -m playwright install chromium           # 已装过会秒过
```

## 4. 两种用法

### 4.1 一键向导（推荐）

```powershell
python start.py
```

它会一路带你走完：检查 Chromium → 没有配置文件就现建 → 逐个账号检查会话（失效就弹浏览器让你登录一次）→ 自动拉「我的课程」并显示完成度 → 你按序号选课（`1` / `1,3` / `all`）→ 显示待处理内容与预计耗时 → 选节奏 → 开刷 → 打印汇总。

```
                                   我的课程
┌─────┬──────────────────────┬────────────┬──────────┬───────────┬────────────┐
│   # │ 课程                 │ 课程号     │       ID │ 完成/总数 │ 学习完整度 │
├─────┼──────────────────────┼────────────┼──────────┼───────────┼────────────┤
│   1 │ 基础工业工程（挑战… │ 2610082    │    29066 │     22/74 │      33.0% │
└─────┴──────────────────────┴────────────┴──────────┴───────────┴────────────┘
? 选课程（如 1 / 1,3 / all，回车=1）:
                        待处理内容（进度以 acc01 为准）
┌──────────────────┬──────┬────────────┬──────┬──────┬────────┬────────┬──────┐
│ 课程             │ 视频 │ 视频总时长 │ 资料 │ 附件 │ 已完成 │ 需人工 │ 预计 │
├──────────────────┼──────┼────────────┼──────┼──────┼────────┼────────┼──────┤
│ 基础工业工程（… │   23 │      5h12m │   18 │   57 │     16 │      3 │ 10m  │
└──────────────────┴──────┴────────────┴──────┴──────┴────────┴────────┴──────┘
```

`--yes` 全程用默认答案（全部账号、第一门课、normal、100%），适合挂着跑。想看向导长什么样又不想登录：`python tools/demo_wizard.py`（假服务端演示）。

### 4.2 分步 CLI（想精确控制时）

```powershell
Copy-Item config.example.yaml   config.yaml
Copy-Item accounts.example.yaml accounts.yaml   # 至少一个 enabled: true 的账号

python run.py login --all                       # 登录一次（会弹浏览器）
python run.py status                            # 看会话与完整度
python run.py probe --account acc01 --activity 92918   # 探针，选节奏
python run.py run --all --dry-run               # 看计划
python run.py run --all --speed normal          # 开刷
```

跑完会自动打印汇总表，并写 `data/result.json`（含每个活动的 status、百分比、请求数、耗时）。

## 5. 多账号 / 并发

`accounts.yaml`：

```yaml
defaults:
  course_ids: [29066]
  semester_id: "14"
  academic_year_id: "5"
accounts:
  - name: acc01          # 会变成 profiles/acc01、data/state.acc01.json、data/logs/acc01.log
    enabled: true
  - name: acc02
    enabled: true
    speed: human         # 单账号覆盖节奏
    engine: browser      # 单账号覆盖引擎
```

* 账号之间**并行**（`--account-jobs`，默认 `min(账号数, 4)`），互不影响；一个账号会话废了只把它标记失败。
* **同一账号同一时刻只有一个请求在飞**（默认 `--per-account-jobs 1`）。原因：`session` cookie 每条响应都轮换，同账号并发写会互相顶号。
  设 `--per-account-jobs 3` 时，工具不改并发写，而是把多个活动的分片**交错轮转**并摊薄段间隔——安全地快 2~3 倍。
* 全局限速 `--global-rps`（默认 8）防止多账号把校园网关打爆；单账号 `--rps` 默认 3。
* 课程结构（248 KB 的活动列表）只抓一次，N 个账号共享（`data/course-cache.json`，TTL 30 分钟）。
* 浏览器上下文有上限（`max_browser_contexts`，默认 2），超出排队的账号先等名额，避免 10 个账号同时开 10 个 Chromium。

## 6. 两种引擎

| engine | 行为 | 适用 |
|---|---|---|
| `http`（默认） | 开头用一次无头 Chromium 验证会话 + 取最新 cookie，之后全部走 `requests` | 多账号、跑得快、占内存少 |
| `browser` | 每个账号常驻一个后台 Chromium，所有请求走 `context.request`（cookie 与页面完全共享） | 想完全贴浏览器行为、被风控盯上时 |

两种都是"后台调浏览器进程 + 直接打接口"，都不点页面。

## 7. 节奏档与耗时

| speed | 段长 | 段间隔 | 说明 |
|---|---|---|---|
| `probe` | 125s（服务端上限，顶格冲） | 0.3~0.8s | 最快；先拿一门课试水 |
| `normal`（默认） | 120s（`chunk_seconds` 可调） | 1.5~3s | 默认 |
| `human` | 30s | 5~15s | 慢但最像人，被风控时用 |

**服务端硬限制（实测）**：单次上报跨度最多 **125 秒**，超出直接 400：

```json
{"message":"Log failed, caused： The set time duration（538s） is too long, maximum is 125s"}
```

所以「整段一次刷满」这条路走不通。工具默认按 125s 切段；万一学校改了这个数，`maximum is Ns` 会被自动解析并按新上限重排（`_server_max_span`），不用改代码。

`config.yaml` 里取消注释 `gap_seconds` 就会**固定所有档位的段间隔**；不写则跟随上表。

按课程 29066 的实际内容（23 个视频共 5h12m + 18 个资料 57 个附件），**单账号**（rps=3）：

| speed | 预计耗时 | 写请求数 |
|---|---|---|
| `probe` | ≈4~5 分钟 | ~207（150 段视频 + 57 附件） |
| `normal` | ≈10 分钟 | ~207 |
| `human` | ≈30 分钟 | ~207 |

实测样本：活动 92919（9m23s）用 `probe` 档 5 段 17 秒刷满，零 400。

多账号并行时总耗时 ≈ 最慢的那个账号；`--per-account-jobs 3` 可再压到 1/3 左右。

### 平台侧到底校验什么（实测）

| 校验点 | 是否校验 | 证据 |
|---|---|---|
| 单段跨度 ≤ 125 秒 | ✅ 硬校验 | 超了返回 `400 ... maximum is 125s` |
| 两次上报之间的真实间隔 | ❌ 不校验 | 563.9 秒的视频 5 段、17 秒刷完，服务端照样给 100% |
| 上报区间是否连续递增 | ❌ 不校验 | `probe` 档直接按 125s 网格补齐即可 |
| `statistics` 埋点的 `ts` | 客户端字段 | `ts/start_at/end_at` 都是前端自己填的，服务端一般只用于报表 |

**结论：进度只认「区间并集」，不认时间。** 所以最快的做法就是顶格 125s 连续上报：

```powershell
python run.py run --all --speed probe --gap 0.5     # 整门课 1~2 分钟
```

`--gap` 是段间隔（秒），越小越快；请求密度 = 1/gap，别同时把 `--rps` 开太高。

#### realtime 档还留着干什么

平台不校验真实时间，所以它**不会更快，只会更慢**。它只在两种情况下有意义：

1. 你想让 `statistics` 埋点的时间线和真实播放一致（教师端"学习时长/行为轨迹"报表）；
2. 想在请求密度上低调一点（每段等真实时长，天然不会密集）。

```powershell
python run.py bench --account acc01 --course 29066         # 实测延迟 + 推荐并行路数
python run.py run --all --speed realtime --lanes auto      # 让它自己算
python run.py run --all --speed realtime --lanes 8         # 或直接指定数字
python run.py run --all --speed realtime --lanes all       # 有多少视频开多少路
```

实测参考（校园网关 RTT ≈335ms，21 个视频共 4h53m）：省心 3 路 ≈ 1h37m、推荐 5 路 ≈ 59m、极速 16 路 ≈ 20m（被最长单个视频 19m54s 卡住）。

### 定时任务（tick 档：单线程 + 固定节拍）

不搞多线程、不搞独立时间线，就一个循环：每 120 秒醒一次，把所有没看完的视频各推一段，然后睡回去。

```powershell
# 长驻：每 120 秒一轮
python run.py run --all --speed tick

# 单轮：跑一轮就退出（适合交给系统计划任务）
python run.py run --account acc01 --speed tick --once

# 生成 Windows 计划任务（每 2 分钟跑一轮 tick --once）
python run.py schedule --account acc01 --every 2            # 只打印命令
python run.py schedule --account acc01 --every 2 --install  # 真的创建
python run.py schedule --account acc01 --delete             # 撤销
```

语义与实测：

* 每个 tick 把**所有**未完成视频各推 `120s` 一段（正好卡在服务端 125s 上限下）；
* 一轮 20 多个请求 3~8 秒发完，剩下 100 多秒都在等时钟 —— **所以这里多线程没有收益**，默认单线程（`--lane-threads 1`；调到 2~3 只是让同一轮的请求并发发出，缩不了那 120 秒，同账号并发写还有顶号风险）；
* 关机、断网、Ctrl-C 都无所谓：`data/state.<账号>.json` 记着每个活动的进度，下次运行从断点继续；
* 计划任务用 `%LOCALAPPDATA%\guet-shuake\tick-<账号>.bat` 包装（GBK 编码 + `chcp 936`，中文路径不会乱），日志写 `data/logs/tick-<账号>.log`。

**什么时候用哪个**：

| 场景 | 选择 |
|---|---|
| 现在就要刷完 | `--speed probe --gap 0.5`（整门课 1~2 分钟） |
| 想低调、要真实时间线 | `--speed realtime --lanes auto`（并行多路） |
| 想挂着不管、开机就跑一点 | `--speed tick` + `schedule --install`（单线程定时） |

单账号 `normal` 大约 **5~10 分钟**（≈180 次请求）；`--per-account-jobs 3` 约 3 分钟。

先探针再决定档位：

```powershell
python run.py probe --account acc01 --activity 92918
```
输出里的 `recommend` 就是建议值（`probe` / `normal` / `human` / `skip`）。

## 8. 自动降档与断点

* 某次上报返回 2xx 但 `completeness` 没涨 → 判定"服务端不认这个跨度" → 段长减半、重算剩余区间、重试；连续失败 7 次才把该活动标 `failed`，不影响其它活动。
* 上报被 `400 maximum is Ns` 拒绝 → 立刻解析出服务端上限，按新段长重排（一次就学会，不靠逐级试）。
* 会话失效（401/跳 CAS/`session` 过期）→ 自动重建会话重试一次；再不行就停下这个账号并提示 `run.py login`。
* 每个活动处理完立即写 `data/state.<账号>.json`；`Ctrl-C` 中断后重跑只补没做的部分。
* 同一个 profile 有 `.lock` 保护，重复启动会明确报错而不是把会话搞乱。

## 9. 命令参考

```powershell
python run.py login  [--all | --account acc01,acc02]
python run.py status [--all | --account acc01]
python run.py list   --course 29066
python run.py probe  --account acc01 --activity 92918 [--course 29066]

python run.py run    [--all | --account acc01] [--course 29066] [--only 92918,92906]
                     [--speed probe|normal|human] [--chunk-seconds 120] [--target 100]
                     [--engine http|browser] [--headed] [--dry-run]
                     [--account-jobs 4] [--per-account-jobs 1]
                     [--rps 3] [--global-rps 8] [--no-telemetry]
```

退出码：`0` 全部成功 · `1` 配置/接口错误 · `2` 会话全部失效（该重新 login）· `3` 部分账号或活动失败。

## 10. 常见报错

| 现象 | 原因 / 处理 |
|---|---|
| `找不到 accounts.yaml` | `Copy-Item accounts.example.yaml accounts.yaml` |
| `profiles/xxx 还没有 profile` | 先 `python run.py login --account xxx` |
| `会话不可用` / `会话已失效` | 重跑 login；若反复失效，检查是否在别处登录把 `session` 顶掉了 |
| `profile 正在被进程 N 使用` | 关了占用该 profile 的浏览器/另一个本工具进程，或删 `profiles/<名字>/.lock` |
| 全部上报都没让百分比上涨 | 用 `human` 档；仍不动就说明课程端有额外限制，此时 `probe` 的 `steps` 会显示每次的 before/after |
| 403 / 419 频繁 | 降 `--rps`、改 `human`、减少 `--account-jobs`；不带 Referer 的 XHR 会被 WAF 直接 403，工具已统一补 Referer |
| `400 ... maximum is 125s` | 单段超服务端上限，工具会解析并自动降段；手动调小 `chunk_seconds` 即可 |
| 找不到 Chromium | `python -m playwright install chromium`（工具也会自动退到本机已有的 `ms-playwright/chromium-*`，只打一条 INFO） |

## 11. 目录

```
start.py               一键向导（登录 → 选课 → 开刷）
run.py                 命令行入口（login / status / list / probe / run）
config.example.yaml    全局配置模板
accounts.example.yaml  多账号模板
guet/
  wizard.py            向导流程（选账号 / 选课 / 选节奏 / 汇总）
  config.py            配置与账号清单
  browser.py           Chromium 生命周期、并发槽、profile 锁、版本回退
  session.py           登录、cookie 导出、按引擎开传输层
  transport.py         统一请求接口（requests / context.request）
  client.py            TronClass 接口封装
  planner.py           活动 -> 任务
  progress.py          区间数学（合并/求补/切段）
  telemetry.py         /statistics 埋点镜像
  runner.py            单账号执行器（VideoJob / MaterialJob 状态机）
  realtime.py          真实时间多路并行（lanes）
  ticker.py            固定节拍 / 单线程定时任务
  benchmark.py         性能自检 + 并行路数推荐
  probe.py             服务端接受度探针
  scheduler.py         多账号编排、课程缓存、错误隔离
  report.py            终端表格 + result.json
tools/
  har_analyze.py       HAR 分析 / 样例抽取 / fixtures 导出
  probe.py             独立探针
  demo_wizard.py       离线演示向导（假服务端）
tests/
  test_offline.py      离线自测（16 项，不需要账号）
  test_integration.py  全链路自测（3 项，假传输层）
  test_wizard.py       向导自测（10 项）
  fixtures/            从 HAR 导出的真实响应
```

## 12. 测试

```powershell
python -m unittest discover -s tests -v     # 29 项全绿，纯离线
```

覆盖：
* `test_offline.py`（16 项）——区间数学、活动规划（对着真实 fixtures 校验 30/18/12 分布）、视频状态机（整段/分段/服务端限跨度时自动降档/服务端完全不理会时快速失败）、资料状态机、配置校验。
* `test_integration.py`（3 项）——用假传输层把 `scheduler → runner → client` 全链路跑通：41 个待办活动全部刷到 100%、埋点被调用、`state.acc01.json` 正常落盘、dry-run 不发写请求、坏账号不影响好账号。
* `test_wizard.py`（10 项）——选择解析、耗时估算、`accounts.yaml` 自动生成，以及用脚本化答案把整个 `start.py` 流程跑一遍（含中途取消、`--yes` 默认路径）。

重新分析抓包 / 导出 fixtures：

```powershell
python tools/har_analyze.py "content-autofill.googleapis.com_2026_11_04_12_46_14.har"
python tools/har_analyze.py <har> --dump 855
python tools/har_analyze.py <har> --export tests/fixtures
```

## 13. 边界

* **作业与考试不自动完成**：需要真交内容/真答题，工具只把它们列进报告的"需人工"。
* 所有 state / profile / 账号密码都在 `.gitignore` 里，不会进仓库。
* 刷课属于自动化手段，成绩与考勤以老师端为准；被系统判定异常时以 `human` 档、降并发、错峰运行处理。
