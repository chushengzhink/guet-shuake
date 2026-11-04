"""固定节拍模式（tick）：单线程定时任务，每 `interval` 秒推进一轮。

和 realtime 的区别只有调度方式：

* realtime：每条视频一条独立时间线（各自 next_at），主线程谁先到期处理谁；
* tick：所有视频对齐同一个节拍，每个 tick 把所有在跑视频各推一段，然后 sleep。

tick 的心智模型最简单：一个 while 循环 + 一次 sleep，
并且天然支持「单轮模式」——跑一轮就退出，交给 Windows 计划任务每 2 分钟拉起一次。
"""

from __future__ import annotations

import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence

from .client import SERVER_MAX_SPAN_DEFAULT
from .realtime import STATUS_DONE, STATUS_FAILED, LaneJob
from .util import human_seconds

STATUS_PENDING = "pending"


def effective_threads(runner, threads: int) -> int:
    """浏览器引擎的 Playwright 对象绑定创建线程，跨线程调用会崩，强制串行。"""
    threads = max(1, int(threads))
    engine = getattr(getattr(getattr(runner, "client", None), "transport", None), "engine", "")
    if threads > 1 and engine == "browser":
        runner.log.warning("浏览器引擎（browser）不支持跨线程请求，lane-threads 已强制回 1")
        return 1
    return threads


def _safe_step(job: LaneJob) -> str:
    """并发模式下兜住异常：会话失效等无法安全重试的，直接标记失败。"""
    try:
        return job.step()
    except Exception as exc:  # SessionExpired / 网络异常
        job.failed = True
        job.detail = f"并发上报异常：{exc}（可改回 --lane-threads 1）"
        return STATUS_FAILED


def run_ticks(
    runner,
    tasks: Sequence,
    chunk: int,
    target: int,
    interval: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int | None = None,
    lanes: int = 0,
    threads: int = 1,
    beat_seconds: float = 300.0,
) -> list:
    """每 `interval` 秒一轮，每轮把所有在跑活动各推一段。

    max_ticks=1 表示「只跑一轮就退出」（配合计划任务）。
    lanes>0 时限制同时推进的活动数，默认 0 = 有多少开多少。
    threads>1 时同一轮内的多个上报并发发出（同账号并发写有顶号风险，默认 1）。
    """
    lanes = max(0, int(lanes))
    threads = effective_threads(runner, threads)
    interval = max(0.0, float(interval))
    chunk = max(1, min(int(chunk), int(getattr(runner, "max_span", SERVER_MAX_SPAN_DEFAULT))))

    pending = deque(tasks)
    active: list[LaneJob] = []
    results: list = []
    started = clock()
    last_beat = started
    ticks = 0

    def activate() -> None:
        while pending and (lanes <= 0 or len(active) < lanes):
            task = pending.popleft()
            job = LaneJob(runner, task, chunk, target, clock=clock)
            runner._guard(job.prepare)
            if job.done:
                runner.log.info("  ○ %s 已是目标进度，跳过", task.label)
                continue
            active.append(job)

    activate()
    if not active:
        runner.log.info("没有需要推进的活动")
        return results

    runner.log.info(
        "定时模式：%d 个活动同时推进，每 %.0f 秒一轮，每轮每路 %ss 段%s%s",
        len(active), interval, chunk,
        f"，同轮并发 {threads} 路" if threads > 1 else "",
        "（单轮模式，跑一轮就退出）" if max_ticks == 1 else "",
    )

    while active:
        batch = list(active)
        if threads > 1 and len(batch) > 1:
            with ThreadPoolExecutor(max_workers=min(threads, len(batch))) as pool:
                outcomes = list(pool.map(_safe_step, batch))
        else:
            outcomes = [runner._guard(job.step) for job in batch]
        for job, outcome in zip(batch, outcomes):
            if outcome in (STATUS_DONE, STATUS_FAILED):
                if job in active:
                    active.remove(job)
                result = job.result(outcome)
                results.append(result)
                runner._record(result)
        activate()
        ticks += 1

        if max_ticks is not None and ticks >= max_ticks:
            # 单轮模式：把没跑完的收尾成 pending，进度已写进 state，下次接着做
            for job in active:
                result = job.result(STATUS_PENDING)
                results.append(result)
                runner._record(result)
            runner.log.info(
                "  本轮结束：%d 路完成，%d 路待续（%.0f%% 收尾已写入 state）",
                sum(1 for r in results if r.status == STATUS_DONE), len(active),
                min((j.percent for j in active), default=100.0),
            )
            break

        if not active:
            break

        if beat_seconds and clock() - last_beat >= beat_seconds:
            last_beat = clock()
            runner.log.info(
                "  ‖ 第 %d 轮：%d 路在跑，最慢 %.0f%%，累计 %s",
                ticks, len(active), min(j.percent for j in active), human_seconds(clock() - started),
            )
        sleep(interval)

    if max_ticks != 1:
        runner.log.info(
            "定时模式结束：%d 轮，用时 %s，%d 个活动",
            ticks, human_seconds(clock() - started), len(results),
        )
    return results
