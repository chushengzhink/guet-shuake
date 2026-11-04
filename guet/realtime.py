"""按真实时间并行刷课（`realtime` 档）。

每条「车道」独立维护自己的时间线：上报完一段，必须等真实的 `chunk` 秒才报下一段，
所以 9 分钟的视频就是 9 分钟走完；同时开 N 条车道，总耗时 ≈ 总时长 / N。

为什么不是纯多线程：
同一账号的 `session` cookie 每条响应都会轮换，多个线程同时写会互相顶号。
这里让调度循环串行发请求（每路间隔 ≥ chunk 秒，压根不会排队），
但每条视频的时间线仍是严格的物理时间——效果等价于多线程，且没有顶号风险。
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Callable, List, Sequence

from .client import (
    SERVER_MAX_SPAN_DEFAULT,
    SessionExpired,
    TronClassError,
    parse_activity_read,
    parse_max_span,
)
from .progress import complement, merge_ranges
from .transport import TransportError
from .util import human_seconds

STATUS_DONE = "done"
STATUS_FAILED = "failed"


class LaneJob:
    """一条「按真实时间推进」的活动时间线。"""

    def __init__(
        self,
        runner,
        task,
        chunk: int,
        target: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner
        self.task = task
        self.chunk = max(1, int(chunk))
        self.target = target
        self.clock = clock
        self.total = int(task.duration)
        self.cursor = 0
        self.percent = 0
        self.sent = 0
        self.retries = 0
        self.rounds = 0
        self.next_at = 0.0
        self.started = 0.0
        self.done = False
        self.failed = False
        self.detail = ""
        self.history: List[tuple[float, int, int]] = []

    # ------------------------------------------------------------------ 生命周期
    def prepare(self) -> None:
        """激活这条车道：读服务端已有进度，从断点接着走。"""
        self.started = self.clock()
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, ranges = parse_activity_read(read)
        self.percent = percent
        if state == "full" or percent >= self.target:
            self.done = True
            return
        merged = merge_ranges(ranges)
        self.cursor = min(self.total, merged[-1][1] if merged else 0)
        self.next_at = self.started + self.chunk
        self.runner.telemetry.video_view(
            self.task.course_id, self.task.module_id, self.task.activity_id,
            self.task.upload_ids[0] if self.task.upload_ids else None,
        )

    @property
    def segments(self) -> int:
        return max(1, math.ceil(self.total / self.chunk))

    def step(self) -> str:
        """到点了：上报下一段。返回 progress / done / failed。"""
        if self.done:
            return "done"
        if self.failed:
            return "failed"

        now = self.clock()
        start = self.cursor
        end = min(self.total, start + self.chunk)
        if end <= start:
            return self._advance(now)

        self.runner.request_count += 1
        try:
            resp = self.runner.client.report_video(
                self.task.activity_id, start, end, self.task.course_id
            )
        except SessionExpired:
            raise
        except (TronClassError, TransportError) as exc:
            limit = parse_max_span(str(exc))
            if limit:
                # 服务端告诉了我们单段上限：改小段长，10 秒后重发这一段
                self.chunk = max(1, min(self.chunk, limit))
                self.runner.max_span = min(getattr(self.runner, "max_span", limit), limit)
                self.detail = f"服务端上限 {limit}s，改用 {self.chunk}s 段"
                self.runner.log.info(
                    "  ‖ %s 服务端限制单段 ≤%ss，改用 %ss 段", self.task.label, limit, self.chunk
                )
                self.next_at = now + 10
                return "progress"
            self.retries += 1
            self.detail = f"{start}-{end}s 上报异常：{exc}"
            if self.retries > 3:
                self.failed = True
                return "failed"
            self.next_at = now + 10 * self.retries
            return "progress"

        self.sent += 1
        self.history.append((now, start, end))
        self.next_at = now + self.chunk

        state, percent, _ = parse_activity_read(resp)
        if percent > self.percent:
            self.percent = percent
            self.retries = 0
        if state == "full":
            self.percent = 100
        if self.percent >= self.target:
            self.done = True
            return "done"

        self.cursor = end
        self.runner.telemetry.video(
            self.task.course_id, self.task.module_id, self.task.activity_id,
            self.task.upload_ids[0] if self.task.upload_ids else None, start, end,
        )
        return self._advance(now)

    # ------------------------------------------------------------------ 内部
    def _advance(self, now: float) -> str:
        """时间线走到头了吗？走完但服务端没给满就按缺口补一轮。"""
        if self.cursor < self.total:
            return "progress"
        self.rounds += 1
        if self.percent >= self.target:
            self.done = True
            return "done"
        if self.rounds > 2 or not self._replan():
            self.failed = True
            self.detail = self.detail or f"时间线走完，服务端只记到 {self.percent}%"
            return "failed"
        self.next_at = now + self.chunk
        return "progress"

    def _replan(self) -> bool:
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, ranges = parse_activity_read(read)
        self.percent = max(self.percent, 100 if state == "full" else percent)
        gaps = complement(ranges, self.total)
        if not gaps:
            return False
        self.cursor = gaps[0][0]
        return True

    def result(self, status: str):
        from .runner import TaskResult  # 延迟导入，避免 runner <-> realtime 循环

        return TaskResult(
            course_id=self.task.course_id,
            activity_id=self.task.activity_id,
            type=self.task.type,
            title=self.task.title,
            action="video",
            status=status,
            completeness=self.percent,
            detail=self.detail or ("按真实时长走完" if status == STATUS_DONE else ""),
            chunks=self.sent,
            elapsed=max(0.0, self.clock() - self.started),
        )


def run_lanes(
    runner,
    tasks: Sequence,
    chunk: int,
    target: int,
    lanes: int,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    beat_seconds: float = 300.0,
    max_wait: float = 60.0,
) -> list:
    """开 `lanes` 条车道并行推进，每条车道严格按真实时间走。"""
    lanes = max(1, int(lanes))
    chunk = max(1, min(int(chunk), int(getattr(runner, "max_span", SERVER_MAX_SPAN_DEFAULT))))
    pending = deque(tasks)
    active: List[LaneJob] = []
    results: list = []
    started = clock()
    last_beat = started
    total_planned = 0

    runner.log.info(
        "实时模式：%d 个活动，同时 %d 路，每路每 %ss 上报一段（时间线=真实播放时间）",
        len(tasks), lanes, chunk,
    )

    def activate() -> None:
        nonlocal total_planned
        while pending and len(active) < lanes:
            task = pending.popleft()
            job = LaneJob(runner, task, chunk, target, clock=clock)
            runner._guard(job.prepare)
            if job.done:
                runner.log.info("  ○ %s 已是目标进度，跳过", task.label)
                continue
            active.append(job)
            total_planned += job.segments
            runner.log.info(
                "  ‖ 启动 %s：时长 %s，%d 段 × %ss，预计 %s",
                task.label, human_seconds(job.total), job.segments, job.chunk,
                human_seconds(job.segments * job.chunk),
            )

    activate()
    while active:
        active.sort(key=lambda j: j.next_at)
        now = clock()
        wait = active[0].next_at - now
        if wait > 0:
            sleep(min(max(wait, 0.01), max_wait))
            continue

        due = [j for j in active if j.next_at <= now][:lanes]
        for job in due:
            outcome = runner._guard(job.step)
            if outcome in (STATUS_DONE, STATUS_FAILED):
                active.remove(job)
                result = job.result(outcome)
                results.append(result)
                runner._record(result)
                activate()

        if beat_seconds and clock() - last_beat >= beat_seconds:
            last_beat = clock()
            done_segments = sum(j.sent for j in active) + sum(r.chunks for r in results)
            runner.log.info(
                "  ‖ 实时刷新中：%d 路在跑，已完成 %d/%d 段，最慢一路 %.0f%%，累计 %s",
                len(active), done_segments, max(total_planned, done_segments), 
                min((j.percent for j in active), default=100.0),
                human_seconds(clock() - started),
            )

    runner.log.info(
        "实时模式结束：用时 %s，%d 个活动", human_seconds(clock() - started), len(results)
    )
    return results
