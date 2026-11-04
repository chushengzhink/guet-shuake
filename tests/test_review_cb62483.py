"""针对 review-cb62483 的回归测试（R03–R10）。

R01/R02 已在工作区修复并带测试（tests/test_transport.py、test_realtime 的相关用例）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet import cancel, scheduler
from guet.client import TronClassError
from guet.config import ConfigError, load_config
from guet.gui import server as gui_server
from guet.realtime import STATUS_PENDING, LaneJob, run_lanes
from guet.scheduler import CourseCache
from guet.util import shutdown_loggers
from tests.test_realtime import FakeClock, FakeRunner, PerActivityServer, Task


class CourseFailureTest(unittest.TestCase):
    """R03：课程抛异常时账号结果必须是失败，即使没有任何 failed 记录。"""

    def setUp(self):
        shutdown_loggers()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        (self.root / "accounts.yaml").write_text(
            "defaults:\n  course_ids: [29066]\naccounts:\n  - name: acc01\n    enabled: true\n",
            encoding="utf-8",
        )
        (self.root / "config.yaml").write_text("rps: 50\ngap_seconds: [0, 0]\n", encoding="utf-8")
        self.app = load_config(self.root)

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    def test_course_exception_marks_account_failed(self):
        from tests.test_integration import FakeTransport, fixture

        activities = fixture("activities.json")["activities"]
        reads = {int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]}
        transport = FakeTransport(activities, reads)

        def make_client(app, account, limiter, global_limiter):
            from guet.client import TronClassClient
            return TronClassClient(transport, app.runtime, limiter=limiter), transport

        cache = CourseCache(self.root / "cache.json", 1800)
        with mock.patch.object(scheduler, "make_client", make_client), \
                mock.patch.object(scheduler.AccountRunner, "run_course",
                                  side_effect=RuntimeError("offline course failure")):
            outcome = scheduler.run_one(self.app, self.app.account("acc01"), cache, None)

        self.assertFalse(outcome.ok)
        self.assertIn("offline course failure", outcome.error)


class LanesAndPlannedTest(unittest.TestCase):
    """R04：GUI 传字符串 '8' 要能通过；R08：planned 非法不能把状态卡在 running。"""

    def setUp(self):
        shutdown_loggers()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.state = gui_server.AppState(self.root)
        self.state.ensure_files()
        self.state.add_account("acc01")

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    def test_string_lanes_accepted(self):
        started = {}

        def fake_run_all(app, accounts, **kwargs):
            started["ok"] = True
            return []
        with mock.patch.object(gui_server.scheduler, "run_all", fake_run_all):
            gui_server.api_start(self.state, {
                "accounts": ["acc01"], "courses": [29066],
                "speed": "realtime", "lanes": "8", "planned": {"acc01": 10},
            })
        self.assertTrue(started.get("ok"))
        self.assertEqual(self.state.planned.get("acc01"), 10)

    def test_bad_planned_does_not_wedge_state(self):
        with self.assertRaises(ConfigError):
            gui_server.api_start(self.state, {
                "accounts": ["acc01"], "courses": [29066], "planned": "invalid",
            })
        self.assertEqual(self.state.status, "idle")   # 必须还能再次启动
        self.assertIsNone(self.state.run_thread)


class RealtimeTimingTest(unittest.TestCase):
    """R05：不等长缺口要按「下一段长度」等待。"""

    def test_uneven_gaps_wait_by_next_segment(self):
        server = PerActivityServer(200)
        server.ranges[1] = [[10, 100]]            # 剩余 [0,10] 和 [100,200]
        clock = FakeClock()
        runner = FakeRunner(server)
        results = run_lanes(
            runner, [Task(1, 200)], 100, 100, 1,
            clock=clock.now, sleep=clock.sleep,
            wait=lambda t: (clock.sleep(t), False)[1], beat_seconds=0,
        )
        self.assertEqual(results[0].status, "done")
        self.assertEqual(clock.t, 110)            # 10 + 100，而不是 10 + 10
        self.assertEqual(server.calls, [(1, 0, 10), (1, 100, 200)])


class RealtimeSpanLimitTest(unittest.TestCase):
    """R06：服务端反复返回同一个上限时必须有限失败。"""

    class StubbornServer(PerActivityServer):
        def report_video(self, aid, start, end, cid=None):
            self.calls.append((aid, start, end))
            raise TronClassError(
                'POST -> 400 {"message":"The set time duration is too long, maximum is 100s"}'
            )

    def test_repeated_limit_eventually_fails(self):
        server = self.StubbornServer(300)
        clock = FakeClock()
        runner = FakeRunner(server)
        job = LaneJob(runner, Task(1, 300), 100, 100, clock=clock.now)
        job.prepare()
        for _ in range(20):
            clock.sleep(60)
            if job.step() in ("done", "failed"):
                break
        self.assertTrue(job.failed, f"没有收敛：retries={job.retries}")
        self.assertLess(len(server.calls), 10)


class RealtimeCancelTest(unittest.TestCase):
    """R09：取消要把在跑与排队任务都记为 pending。"""

    def test_cancel_records_pending(self):
        cancel.reset()
        server = PerActivityServer(300)
        clock = FakeClock()
        runner = FakeRunner(server)

        def wait_and_cancel(timeout: float) -> bool:
            clock.sleep(timeout)
            cancel.request()
            return True

        try:
            results = run_lanes(
                runner, [Task(1, 300), Task(2, 300)], 100, 100, 1,
                clock=clock.now, sleep=clock.sleep, wait=wait_and_cancel,
                beat_seconds=0,
            )
        finally:
            cancel.reset()

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == STATUS_PENDING for r in results))
        self.assertEqual(len(runner.records), 2)


class CancellableWaitTest(unittest.TestCase):
    """R10：等待要能被取消立刻唤醒。"""

    def test_wait_returns_true_when_cancelled(self):
        cancel.reset()
        try:
            self.assertFalse(cancel.wait(0.01))
            cancel.request()
            self.assertTrue(cancel.wait(5))
        finally:
            cancel.reset()


if __name__ == "__main__":
    unittest.main(verbosity=2)
