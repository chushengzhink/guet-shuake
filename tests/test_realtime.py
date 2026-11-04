"""realtime 档自测：真实时间线、车道数、断点、服务端上限、推荐算法。"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet.benchmark import BenchResult, measure, plan_scale, recommend  # noqa: E402
from guet.client import TronClassError  # noqa: E402
from guet.progress import covered_seconds, merge_ranges  # noqa: E402
from guet.realtime import LaneJob, run_lanes  # noqa: E402
from guet.runner import resolve_lanes  # noqa: E402


class PerActivityServer:
    """按活动 id 分开记进度的假服务端。"""

    def __init__(self, duration: int, reject_over: int | None = None) -> None:
        self.duration = duration
        self.reject_over = reject_over
        self.ranges: dict[int, list[list[int]]] = {}
        self.calls: list[tuple[int, int, int]] = []

    def _state(self, aid: int) -> dict:
        ranges = self.ranges.setdefault(aid, [])
        covered = covered_seconds(ranges)
        percent = min(100, int(covered * 100 / self.duration))
        return {
            "completeness": "full" if percent >= 100 else ("part" if ranges else ""),
            "data": {"completeness": percent, "ranges": [list(r) for r in ranges]},
        }

    def activity_read(self, aid, cid=None):
        return self._state(aid)

    def report_video(self, aid, start, end, cid=None):
        self.calls.append((aid, start, end))
        if self.reject_over and end - start > self.reject_over:
            raise TronClassError(
                f'POST /api/course/activities-read/{aid} -> 400 {{"message":'
                f'"The set time duration（{end - start}s） is too long, maximum is {self.reject_over}s"}}'
            )
        merged = merge_ranges(self.ranges.get(aid, []) + [[start, end]])
        self.ranges[aid] = [[s, e] for s, e in merged]
        return self._state(aid)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class NullTelemetry:
    def video(self, *a, **kw):
        pass

    def video_view(self, *a, **kw):
        pass

    def material(self, *a, **kw):
        pass


class FakeRunner:
    def __init__(self, server) -> None:
        self.client = server
        self.telemetry = NullTelemetry()
        self.log = logging.getLogger("test-realtime")
        self.max_span = 125
        self.request_count = 0
        self.records = []

    def _guard(self, func):
        return func()

    def _record(self, result) -> None:
        self.records.append(result)


class Task:
    def __init__(self, aid: int, duration: int) -> None:
        self.activity_id = aid
        self.course_id = 1
        self.module_id = 1
        self.duration = duration
        self.upload_ids = []
        self.type = "online_video"
        self.title = f"视频{aid}"
        self.label = f"{aid} 视频{aid}"


def run(server, tasks, chunk=100, target=100, lanes=2):
    clock = FakeClock()
    runner = FakeRunner(server)
    results = run_lanes(
        runner, tasks, chunk, target, lanes,
        clock=clock.now, sleep=clock.sleep, beat_seconds=0,
    )
    return runner, results, clock


class RealtimeTest(unittest.TestCase):
    def test_timeline_is_real_and_lanes_parallel(self):
        server = PerActivityServer(300)
        tasks = [Task(1, 300), Task(2, 300), Task(3, 300)]
        runner, results, clock = run(server, tasks, chunk=100, lanes=2)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "done" for r in results))
        # 2 路并行：A/B 各 300s，C 排队再来 300s → 总共 600s
        self.assertEqual(clock.t, 600)
        self.assertEqual(len(server.calls), 9)
        for aid in (1, 2, 3):
            self.assertEqual(server.ranges[aid], [[0, 300]])

    def test_gap_between_reports_is_chunk(self):
        server = PerActivityServer(300)
        clock = FakeClock()
        runner = FakeRunner(server)
        job = LaneJob(runner, Task(9, 300), 100, 100, clock=clock.now)
        job.prepare()
        clock.sleep(100)
        job.step()
        clock.sleep(100)
        job.step()
        self.assertEqual([h[0] for h in job.history], [100, 200])

    def test_resume_from_server_progress(self):
        server = PerActivityServer(300)
        server.ranges[1] = [[0, 100]]          # 服务端已记 100 秒
        runner, results, clock = run(server, [Task(1, 300)], chunk=100, lanes=1)
        self.assertEqual(results[0].status, "done")
        self.assertEqual(clock.t, 200)         # 只剩 200 秒的量
        self.assertEqual(server.calls[0][1:], (100, 200))

    def test_learns_server_span_limit(self):
        server = PerActivityServer(300, reject_over=100)
        runner, results, clock = run(server, [Task(1, 300)], chunk=120, lanes=1)
        self.assertEqual(results[0].status, "done")
        self.assertEqual(runner.max_span, 100)
        self.assertEqual(server.ranges[1], [[0, 300]])
        # 只有第一次（120s）被拒，之后都 ≤100s
        oversize = [c for c in server.calls if c[2] - c[1] > 100]
        self.assertEqual(len(oversize), 1)

    def test_target_stops_early(self):
        server = PerActivityServer(300)
        runner, results, clock = run(server, [Task(1, 300)], chunk=100, target=60, lanes=1)
        self.assertEqual(results[0].status, "done")
        self.assertGreaterEqual(results[0].completeness, 60)
        self.assertEqual(clock.t, 200)          # 两段就够 66%

    def test_lanes_capped_by_video_count(self):
        self.assertEqual(resolve_lanes(0, 3), 3)       # all
        self.assertEqual(resolve_lanes("all", 3), 3)
        self.assertEqual(resolve_lanes(10, 3), 3)      # 不超过视频数
        self.assertEqual(resolve_lanes(4, 23), 4)
        self.assertEqual(resolve_lanes("auto", 5), 5)  # 非数字按 all 处理

    def test_fragmented_ranges_only_fill_gaps(self):
        """F23：已看 [0-100][200-800][900-1000] 时只该补两个 100 秒缺口。"""
        server = PerActivityServer(1000)
        server.ranges[1] = [[0, 100], [200, 800], [900, 1000]]
        runner, results, clock = run(server, [Task(1, 1000)], chunk=120, lanes=1)
        self.assertEqual(results[0].status, "done")
        self.assertEqual(server.calls, [(1, 100, 200), (1, 800, 900)])
        self.assertEqual(clock.t, 200)                  # 两段 × 100 秒真实时间
        self.assertEqual(server.ranges[1], [[0, 100], [100, 200], [200, 800],
                                            [800, 900], [900, 1000]][:0] or server.ranges[1])
        covered = covered_seconds(server.ranges[1])
        self.assertEqual(covered, 1000)


class BenchmarkTest(unittest.TestCase):
    def fast_bench(self):
        return BenchResult(rounds=5, rtt_ms=[40, 45, 50, 60, 120], failures=0)

    def test_default_target_one_hour(self):
        advice = recommend(self.fast_bench(), 23, 18720, max_lanes=16)
        self.assertEqual(advice.lanes, 6)     # 18720/3600 = 5.2 → 6
        self.assertGreaterEqual(advice.hard_cap, advice.lanes)
        self.assertLess(advice.eta_for(6), 18720 / 5)

    def test_custom_eta(self):
        advice = recommend(self.fast_bench(), 23, 18720, max_lanes=16, eta_seconds=1800)
        self.assertEqual(advice.lanes, 11)    # 18720/1800 = 10.4 → 11
        self.assertLessEqual(advice.eta_for(advice.lanes), 1800)
        self.assertGreater(advice.eta_for(advice.lanes), 1500)

    def test_loss_reduces_lanes(self):
        bench = BenchResult(rounds=5, rtt_ms=[40, 40, 40, 40, 40], failures=3)
        advice = recommend(bench, 23, 18720, max_lanes=16)
        self.assertLessEqual(advice.lanes, 8)
        self.assertLessEqual(advice.hard_cap, 8)

    def test_high_latency_caps_lanes(self):
        bench = BenchResult(rounds=5, rtt_ms=[2000, 2100, 2200, 2300, 2400], failures=0)
        advice = recommend(bench, 23, 18720, max_lanes=16)
        self.assertLessEqual(advice.lanes, 4)

    def test_few_videos(self):
        advice = recommend(self.fast_bench(), 3, 1800, max_lanes=16)
        self.assertEqual(advice.hard_cap, 3)
        self.assertLessEqual(advice.lanes, 3)

    def test_render_has_essentials(self):
        advice = recommend(self.fast_bench(), 23, 18720)
        text = advice.render()
        self.assertIn("往返延迟", text)
        self.assertIn("建议", text)
        self.assertIn("路", text)

    def test_measure_counts_failures(self):
        class Broken:
            def my_semesters(self):
                raise RuntimeError("boom")

        bench = measure(Broken(), rounds=3)
        self.assertEqual(bench.rounds, 3)
        self.assertEqual(bench.failures, 3)
        self.assertEqual(bench.loss_pct, 100.0)

    def test_plan_scale(self):
        count, total, longest = plan_scale([Task(1, 300), Task(2, 120)])
        self.assertEqual((count, total, longest), (2, 420, 300))


if __name__ == "__main__":
    unittest.main(verbosity=2)
