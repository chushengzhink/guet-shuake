"""tick（固定节拍/定时任务）自测。"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet.ticker import STATUS_PENDING, run_ticks  # noqa: E402
from tests.test_realtime import FakeClock, FakeRunner, PerActivityServer, Task  # noqa: E402


def run(server, tasks, chunk=100, target=100, interval=100, **kw):
    clock = FakeClock()
    runner = FakeRunner(server)
    results = run_ticks(
        runner, tasks, chunk, target, interval,
        clock=clock.now, sleep=clock.sleep,
        wait=lambda t: (clock.sleep(t), False)[1],
        beat_seconds=0, **kw
    )
    return runner, results, clock


class TickTest(unittest.TestCase):
    def test_each_tick_pushes_every_video_one_segment(self):
        server = PerActivityServer(300)
        tasks = [Task(1, 300), Task(2, 300), Task(3, 300)]
        runner, results, clock = run(server, tasks, chunk=100, interval=100)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "done" for r in results))
        # 3 段 × 100s：第 0/100/200 秒各一轮 → 200 秒走完（同时推进，不是 600s）
        self.assertEqual(clock.t, 200)
        self.assertEqual(len(server.calls), 9)
        for aid in (1, 2, 3):
            self.assertEqual(server.ranges[aid], [[0, 300]])

    def test_once_mode_returns_pending(self):
        server = PerActivityServer(300)
        tasks = [Task(1, 300), Task(2, 300)]
        runner, results, clock = run(server, tasks, chunk=100, interval=100, max_ticks=1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == STATUS_PENDING for r in results))
        self.assertEqual(clock.t, 0)          # 一轮不睡觉
        self.assertEqual(len(server.calls), 2)
        # 进度已落进 state，下一次运行接着推
        self.assertEqual(server.ranges[1], [[0, 100]])

    def test_resume_after_once(self):
        server = PerActivityServer(300)
        server.ranges[1] = [[0, 100]]
        runner, results, clock = run(server, [Task(1, 300)], chunk=100, interval=100, max_ticks=1)
        self.assertEqual(server.calls[0][1:], (100, 200))
        self.assertEqual(results[0].completeness, 66)

    def test_lanes_limit(self):
        server = PerActivityServer(300)
        tasks = [Task(1, 300), Task(2, 300), Task(3, 300)]
        runner, results, clock = run(server, tasks, chunk=100, interval=100, lanes=1)
        self.assertEqual(len(results), 3)
        # 只有 1 路：每 100s 推一段，A 在 200s 完成，B 从下一轮开始（300s），C 再到 600s → 800s
        self.assertEqual(clock.t, 800)
        self.assertEqual(len(server.calls), 9)

    def test_threads_concurrent(self):
        server = PerActivityServer(300)
        tasks = [Task(1, 300), Task(2, 300), Task(3, 300)]
        runner, results, clock = run(server, tasks, chunk=100, interval=100, threads=3)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "done" for r in results))
        self.assertEqual(clock.t, 200)

    def test_server_limit_still_learned(self):
        server = PerActivityServer(300, reject_over=100)
        runner, results, clock = run(server, [Task(1, 300)], chunk=125, interval=125)
        self.assertEqual(results[0].status, "done")
        self.assertEqual(runner.max_span, 100)
        self.assertEqual(server.ranges[1], [[0, 300]])

    def test_target_stops_early(self):
        server = PerActivityServer(300)
        runner, results, clock = run(server, [Task(1, 300)], chunk=100, interval=100, target=60)
        self.assertEqual(results[0].status, "done")
        # tick0 立即推一段（33%），tick1（100s 后）推到 66% ≥ 60 → 停在 100s
        self.assertEqual(clock.t, 100)
        self.assertEqual(results[0].completeness, 66)


if __name__ == "__main__":
    unittest.main(verbosity=2)
