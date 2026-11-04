"""针对代码审查问题的回归测试（重试丢项 / 完成误判 / 跨线程 / 并发写 / 参数校验）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run as cli  # noqa: E402
from guet.client import SessionExpired, TronClassError  # noqa: E402
from guet.config import load_config  # noqa: E402
from guet.realtime import LaneJob  # noqa: E402
from guet.runner import MaterialJob, VideoJob  # noqa: E402
from guet.scheduler import CourseCache  # noqa: E402
from guet.ticker import effective_threads  # noqa: E402
from guet.util import LockBusy, atomic_write_json, run_lock, shutdown_loggers  # noqa: E402
from tests.test_realtime import FakeClock, FakeRunner, PerActivityServer, Task  # noqa: E402


class FlakyServer(PerActivityServer):
    """第一次调用抛 SessionExpired，模拟会话失效被 _guard 重建后重试。"""

    def __init__(self, duration: int = 300) -> None:
        super().__init__(duration)
        self.expire_next_video = False
        self.expire_next_upload: int | None = None
        self.upload_calls: list[int] = []

    def report_video(self, aid, start, end, cid=None):
        if self.expire_next_video:
            self.expire_next_video = False
            raise SessionExpired("session expired")
        return super().report_video(aid, start, end, cid)

    def report_upload(self, aid, upload_id, cid=None):
        self.upload_calls.append(upload_id)
        if self.expire_next_upload == upload_id:
            self.expire_next_upload = None
            raise SessionExpired("session expired")
        return {"completeness": "part", "data": {"completeness": 50, "upload_ids": [upload_id]}}


def material_task(uploads):
    task = Task(1, 0)
    task.upload_ids = list(uploads)
    task.type = "material"
    return task


class RetryKeepsItemTest(unittest.TestCase):
    def test_upload_not_lost_on_session_expired(self):
        server = FlakyServer()
        runner = FakeRunner(server)
        job = MaterialJob(runner, material_task([10, 20]), 100)
        job.prepare()
        server.expire_next_upload = 10

        with self.assertRaises(SessionExpired):
            job.step()
        # 队列没被 pop，重试仍是 10 而不是跳到 20
        self.assertEqual(job.pending[0], 10)
        job.step()
        job.step()
        self.assertEqual(server.upload_calls, [10, 10, 20])

    def test_video_segment_not_lost_on_session_expired(self):
        server = FlakyServer(300)
        runner = FakeRunner(server)
        job = VideoJob(runner, Task(1, 300), 100, 100)
        job.prepare()
        first = job.pieces[0]
        server.expire_next_video = True

        with self.assertRaises(SessionExpired):
            job.step()
        self.assertEqual(job.pieces[0], first)

        for _ in range(20):
            if job.step() in ("done", "failed"):
                break
        self.assertTrue(job.done, job.detail)
        self.assertEqual(server.ranges[1], [[0, 300]])


class RealtimeCompletionTest(unittest.TestCase):
    def test_full_state_without_ranges_is_done(self):
        """state=full 但 data 里没有 completeness/ranges 时不能判失败。"""
        class FullServer:
            def activity_read(self, aid, cid=None):
                return {"completeness": "full", "data": {}}

            def report_video(self, aid, start, end, cid=None):
                return {"completeness": "full", "data": {}}

        runner = FakeRunner(FullServer())
        clock = FakeClock()
        job = LaneJob(runner, Task(1, 300), 100, 100, clock=clock.now)
        job.prepare()
        clock.sleep(100)
        # 直接走到时间线末尾：先把 cursor 推到头
        job.cursor = job.total
        self.assertEqual(job.step(), "done")
        self.assertTrue(job.done)
        self.assertFalse(job.failed)


class TickThreadsTest(unittest.TestCase):
    def test_browser_engine_forces_serial(self):
        runner = FakeRunner(PerActivityServer(300))
        runner.client.transport = SimpleNamespace(engine="browser")
        self.assertEqual(effective_threads(runner, 4), 1)

    def test_http_engine_keeps_threads(self):
        runner = FakeRunner(PerActivityServer(300))
        runner.client.transport = SimpleNamespace(engine="http")
        self.assertEqual(effective_threads(runner, 4), 4)


class ConcurrencyTest(unittest.TestCase):
    def test_atomic_write_under_concurrency(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            errors: list[BaseException] = []

            def writer(index: int) -> None:
                try:
                    for _ in range(20):
                        atomic_write_json(path, {"writer": index, "items": list(range(50))})
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)
            leftovers = [p.name for p in Path(raw).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_run_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "acc.lock"
            with run_lock(path):
                with self.assertRaises(LockBusy):
                    with run_lock(path):
                        pass
            with run_lock(path):          # 释放后可以再次获取
                pass

    def test_course_cache_courses_load_in_parallel(self):
        class SlowClient:
            def course_meta(self, cid):
                time.sleep(0.2)
                return {"name": f"课程{cid}", "course_code": str(cid)}

            def activities(self, cid):
                return [{"id": cid}]

            def modules(self, cid):
                return [{"id": 1, "name": "第一章"}]

        with tempfile.TemporaryDirectory() as raw:
            cache = CourseCache(Path(raw) / "cache.json", 1800)
            client = SlowClient()
            started = time.perf_counter()
            threads = [threading.Thread(target=cache.bundle, args=(cid, client)) for cid in (1, 2, 3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.45)     # 串行需要 0.6s 以上
            self.assertEqual(len(cache.bundle(1, client)["activities"]), 1)


class CliValidationTest(unittest.TestCase):
    def setUp(self):
        shutdown_loggers()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "accounts.yaml").write_text(
            "accounts:\n  - name: acc01\n    enabled: true\n", encoding="utf-8"
        )
        (self.root / "config.yaml").write_text("rps: 3\n", encoding="utf-8")

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    def call(self, extra):
        return cli.main([
            "--config", str(self.root / "config.yaml"),
            "--accounts", str(self.root / "accounts.yaml"),
            "run", "--only", "1", *extra,
        ])

    def test_target_out_of_range_rejected(self):
        self.assertEqual(self.call(["--target", "101"]), cli.EXIT_ERROR)

    def test_negative_lanes_rejected(self):
        self.assertEqual(self.call(["--speed", "realtime", "--lanes", "-1"]), cli.EXIT_ERROR)

    def test_schedule_bat_keeps_config_paths(self):
        app = load_config(self.root, self.root / "config.yaml", self.root / "accounts.yaml")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            code = cli.cmd_schedule(app, SimpleNamespace(
                account=["acc01"], all=False, every=2, name=None, install=False, delete=False,
            ))
        self.assertEqual(code, cli.EXIT_OK)
        bat = (self.root / "guet-shuake" / "tick-acc01.bat").read_bytes()
        text = bat.decode("gbk")
        self.assertIn("--config", text)
        self.assertIn("--accounts", text)
        self.assertIn("--speed tick --once", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
