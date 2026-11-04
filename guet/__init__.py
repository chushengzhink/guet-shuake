"""GUET 畅课（TronClass）后台刷课工具。

模块划分
--------
config     配置与账号清单
browser    Chromium 生命周期（持久化 profile / 并发槽 / 锁）
session    登录、cookie 导出、传输层选择
transport  统一请求接口（requests / 浏览器 context.request）
client     TronClass 接口封装（端点由抓包反推）
planner    活动 -> 任务
progress   已看区间数学（合并 / 求补 / 切段）
telemetry  /statistics 埋点镜像
runner     单账号执行器
scheduler  多账号编排
probe      服务端接受度探针
report     终端输出
"""

__version__ = "1.0.0"
