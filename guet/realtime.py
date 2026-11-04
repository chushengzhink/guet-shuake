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
from .cancel import requested as cancel_requested, wait as cancel_wait
from .util import human_seconds

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"


def _pending_result(task, detail: str):
    """给「还没开始就被取消」的活动生成一条 pending 结果（延迟导入避免循环）。"""
    from .runner import TaskResult

    return TaskResult(
        course_id=task.course_id,
        activity_id=task.activity_id,
        type=task.type,
        title=task.title,
        action="video",
        status=STATUS_PENDING,
        completeness=0,
        detail=detail,
        chunks=0,
        elapsed=0.0,
    )


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
        self.gaps: list[tuple[int, int]] = []
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
        self._load_gaps_from(ranges, percent, state)
        # 第一段按『实际缺口长度』等，而不是统一按 chunk（碎片缺口时更准）
        first = self.gaps[0] if self.gaps else None
        span = min(self.chunk, first[1] - first[0]) if first else self.chunk
        self.next_at = self.started + max(1, span)
        self.runner.telemetry.video_view(
            self.task.course_id, self.task.module_id, self.task.activity_id,
            self.task.upload_ids[0] if self.task.upload_ids else None,
        )

    def _load_gaps_from(self, ranges, percent: int, state: str = "") -> None:
        """把「还没看过的区间」排成队列（F23：原来只记一个 cursor，会重报已覆盖的区间）。"""
        self.percent = max(self.percent, 100 if state == "full" else percent)
        self.gaps = complement(merge_ranges(ranges), self.total)
        self.cursor = self.gaps[0][0] if self.gaps else self.total

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
        while self.gaps and self.gaps[0][1] <= self.cursor:
            self.gaps.pop(0)                       # 已补齐的缺口跳过
        if not self.gaps:
            return self._advance(now)
        gap_start, gap_end = self.gaps[0]
        start = max(self.cursor, gap_start)
        end = min(start + self.chunk, gap_end)
        if end <= start:
            self.gaps.pop(0)
            self.cursor = self.gaps[0][0] if self.gaps else self.total
            return "progress" 

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
                # R06：只有真的把段长改小才算学到新信息；同一上限反复拒绝要计入预算
                new_chunk = max(1, min(self.chunk, limit))
                learned = new_chunk < self.chunk
                self.chunk = new_chunk
                self.runner.max_span = min(getattr(self.runner, "max_span", limit), limit)
                self.detail = f"服务端上限 {limit}s，改用 {self.chunk}s 段"
                if learned:
                    self.retries = 0
                    self.runner.log.info(
                        "  ‖ %s 服务端限制单段 ≤%ss，改用 %ss 段", self.task.label, limit, self.chunk
                    )
                else:
                    self.retries += 1
                self.next_at = now + 10
                if self.retries > 3:
                    self.failed = True
                    self.detail = f"服务端持续拒绝上报（单段上限 {limit}s），放弃该活动"
                    return "failed"
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
        if end >= gap_end:                         # 这个缺口补齐了，换下一个
            self.gaps.pop(0)
            self.cursor = self.gaps[0][0] if self.gaps else self.total
        # R05：按「即将上报的那一段」的长度等待，而不是刚上报完的长度
        if self.gaps:
            next_start = max(self.cursor, self.gaps[0][0])
            next_end = min(next_start + self.chunk, self.gaps[0][1])
            wait = max(1, next_end - next_start)
        else:
            wait = max(1, min(self.chunk, self.total or self.chunk))
        self.next_at = now + wait
        self.runner.telemetry.video(
            self.task.course_id, self.task.module_id, self.task.activity_id,
            self.task.upload_ids[0] if self.task.upload_ids else None, start, end,
        )
        return self._advance(now)

    # ------------------------------------------------------------------ 内部
    def _advance(self, now: float) -> str:
        """缺口都处理完了吗？走完但服务端没给满就再查一次。"""
        if self.gaps:
            return "progress"
        if self.percent >= self.target:
            self.done = True
            return "done"
        self.rounds += 1
        refreshed = self._replan()
        # 复查可能把 percent 抬到 100（state=full），所以要在 replan 之后再判一次
        if self.percent >= self.target:
            self.done = True
            return "done"
        if self.rounds > 2 or not refreshed:
            self.failed = True
            self.detail = self.detail or f"时间线走完，服务端只记到 {self.percent}%"
            return "failed"
        gap_start, gap_end = self.gaps[0]
        self.next_at = now + max(1, min(self.chunk, gap_end - gap_start))
        return "progress"

    def _replan(self) -> bool:
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, ranges = parse_activity_read(read)
        self._load_gaps_from(ranges, percent, state)
        return bool(self.gaps)

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
    wait: Callable[[float], bool] | None = None,
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
        if cancel_requested():
            # R09：取消是明确终态，在跑的和排队没开始的都要留痕
            runner.log.warning("收到停止请求，收尾退出（进度已写入 state）")
            for job in list(active):
                result = job.result(STATUS_PENDING)
                results.append(result)
                runner._record(result)
            for task in pending:
                result = _pending_result(task, "排队中未开始")
                results.append(result)
                runner._record(result)
            pending.clear()
            active.clear()
            break
        active.sort(key=lambda j: j.next_at)
        now = clock()
        delay = active[0].next_at - now
        if delay > 0:
            (wait or cancel_wait)(min(max(delay, 0.01), max_wait))
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
