"""review-97cb993 的回归测试（#2 #4 #5 #6 #7，另加 #1/#3 的结构性断言）。"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet import cancel, scheduler
from guet.config import RuntimeConfig
from guet.gui import server as gui_server
from guet.planner import ACTION_SKIP, build_plan
from guet.runner import STATUS_PENDING, TaskResult
from guet.util import shutdown_loggers


class StubClient:
    """足够 build_plan 用的最小客户端。"""

    def __init__(self, activities, reads, completeness=None):
        self._activities = activities
        self._reads = reads
        self._completeness = completeness or {}

    def activities(self, cid):
        return self._activities

    def activity_reads_for_user(self, cid):
        return self._reads

    def modules(self, cid):
        return []

    def my_completeness(self, cid):
        return self._completeness

    def activity_detail(self, aid, cid=None):
        return {"uploads": []}


class PlanConsistencyTest(unittest.TestCase):
    """#5：卡片统计必须和执行计划同一套判定。"""

    def test_unpublished_and_attachmentless_are_not_counted(self):
        activities = [
            {"id": 1, "type": "online_video", "published": False},
            {"id": 2, "type": "material"},                       # 没有附件
            {"id": 3, "type": "online_video", "uploads": [{"id": 9, "type": "video", "videos": [{"duration": 300}]}]},
        ]
        reads = {
            1: {"completeness": "part"},
            2: {"completeness": "part"},
            3: {"completeness": "part"},
        }
        client = StubClient(activities, reads)
        plan = build_plan(client, 1, RuntimeConfig(), activities=activities,
                          reads=reads, modules=[])
        summary = plan.summary()
        self.assertEqual(summary["video"], 1)          # 只有 #3 能刷
        self.assertEqual(summary["material"], 0)       # #2 无附件 → skip
        self.assertEqual(
            [t.reason for t in plan.tasks if t.activity_id == 1], ["未发布"]
        )

        # 卡片统计用的就是同一个 build_plan
        breakdown = gui_server._course_breakdown(
            client, RuntimeConfig(), 1, activities, reads, {}, "课程", 100,
        )
        self.assertEqual(breakdown["auto"], 1)
        self.assertEqual(breakdown["todo_video"], 1)
        self.assertEqual(breakdown["todo_material"], 0)

    def test_target_below_100_skips_reached_items(self):
        """#7：目标 80% 时，已经 90% 的活动不该算进分母。"""
        activities = [{"id": 1, "type": "online_video",
                       "uploads": [{"id": 9, "type": "video", "videos": [{"duration": 300}]}]}]
        reads = {1: {"completeness": "part", "data": {"completeness": 90}}}
        client = StubClient(activities, reads)

        full_plan = build_plan(client, 1, RuntimeConfig(), activities=activities,
                               reads=reads, modules=[], target=100)
        self.assertEqual(full_plan.summary()["video"], 1)

        soft_plan = build_plan(client, 1, RuntimeConfig(), activities=activities,
                               reads=reads, modules=[], target=80)
        self.assertEqual(soft_plan.summary()["video"], 0)
        skipped = [t for t in soft_plan.tasks if t.action == ACTION_SKIP]
        self.assertTrue(any("已达目标" in t.reason for t in skipped))


class CancelOutcomeTest(unittest.TestCase):
    """#6：用户取消后不能汇总成成功。"""

    def setUp(self):
        shutdown_loggers()
        import tempfile

        from guet.config import load_config

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        (root / "accounts.yaml").write_text(
            "defaults:\n  course_ids: [29066]\naccounts:\n  - name: acc01\n    enabled: true\n",
            encoding="utf-8",
        )
        (root / "config.yaml").write_text("rps: 50\ngap_seconds: [0, 0]\n", encoding="utf-8")
        self.app = load_config(root)
        self.root = root

    def tearDown(self):
        cancel.reset()
        shutdown_loggers()
        self.tmp.cleanup()

    def test_cancelled_with_pending_is_not_success(self):
        from tests.test_integration import FakeTransport, fixture

        activities = fixture("activities.json")["activities"]
        reads_data = {int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]}
        transport = FakeTransport(activities, reads_data)

        def make_client(app, account, limiter, global_limiter):
            from guet.client import TronClassClient
            return TronClassClient(transport, app.runtime, limiter=limiter), transport

        pending = TaskResult(
            course_id=29066, activity_id=1, type="online_video", title="x",
            action="video", status=STATUS_PENDING, completeness=0,
            detail="已取消", chunks=0, elapsed=0.0,
        )
        cancel.request()
        with mock.patch.object(scheduler, "make_client", make_client), \
                mock.patch.object(scheduler.AccountRunner, "run_course", return_value=[pending]):
            outcome = scheduler.run_one(self.app, self.app.account("acc01"),
                                        scheduler.CourseCache(self.root / "c.json", 60), None)

        self.assertFalse(outcome.ok)
        self.assertIn("取消", outcome.error)


class ClientOwnershipTest(unittest.TestCase):
    """#2：缓存持有的 client 不能被调用方关掉；#4：统计失败要保留未知状态。"""

    def setUp(self):
        shutdown_loggers()
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state = gui_server.AppState(Path(self.tmp.name))
        self.state.ensure_files()
        self.state.add_account("acc01")

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    def _client(self, fail_breakdown=False):
        account = self.state.app.account("acc01")

        class FakeTransport:
            closed = 0

            def close(self):
                FakeTransport.closed += 1

        class FakeClient:
            def check_session(self):
                return True

            def my_courses(self, sem, ay):
                return [{"id": 29066, "display_name": "课程A", "course_code": "29066"}]

            def my_completeness(self, cid):
                return {"completed_result": {"total_completed": 63, "total_activities": 74},
                        "study_completeness": 87.4}

            def activity_reads_for_user(self, cid):
                return {}

        class FakeCache:
            def bundle(self, cid, client):
                return {"activities": []}

        transport = FakeTransport()
        return account, FakeClient(), transport, FakeCache()

    def test_client_for_keeps_cached_transport_open(self):
        """#2：创建后立刻被调用方 close 的旧问题 —— 返回的应当是空操作关闭器。"""
        calls = {"n": 0}

        class FakeTransport:
            closed = 0

            def close(self):
                FakeTransport.closed += 1

        class FakeClient:
            def check_session(self):
                return True

        transport = FakeTransport()

        def make_client(app, account, limiter, global_limiter):
            calls["n"] += 1
            return FakeClient(), transport

        with mock.patch.object(gui_server.scheduler, "make_client", make_client):
            _, c1, close1 = gui_server._client_for(self.state, "acc01")
            close1.close()                      # 模拟 api_* 的 finally
            _, c2, close2 = gui_server._client_for(self.state, "acc01")
            close2.close()

        self.assertIs(c1, c2)                   # 命中缓存
        self.assertEqual(calls["n"], 1)          # 只建了一次
        self.assertEqual(transport.closed, 0)    # 缓存持有的资源没被关

    def test_breakdown_failure_keeps_unknown(self):
        account, client, transport, cache = self._client()
        with mock.patch.object(gui_server, "_client_for",
                               return_value=(account, client, transport)), \
                mock.patch.object(gui_server, "_course_cache", return_value=cache), \
                mock.patch.object(gui_server, "_course_breakdown",
                                  side_effect=RuntimeError("network down")):
            data = gui_server.api_courses(self.state, {"name": "acc01"})

        course = data["courses"][0]
        self.assertTrue(course["stats_error"])
        self.assertIsNone(course["auto"])        # 未知，不是 0
        self.assertIsNone(course["manual"])
        self.assertIsNone(course["other"])


class NoThreadPoolTest(unittest.TestCase):
    """#1：统计必须串行 —— 线程池会让 browser 引擎的 Playwright 上下文报跨线程错误。"""

    def test_breakdown_runs_in_caller_thread(self):
        source = (ROOT / "guet" / "gui" / "server.py").read_text(encoding="utf-8")
        body = source[source.index("def api_courses("):source.index("def api_plan(")]
        self.assertNotIn("ThreadPoolExecutor", body)
        self.assertNotIn("max_workers", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
