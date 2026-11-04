"""针对代码审查问题的回归测试（重试丢项 / 完成误判 / 跨线程 / 并发写 / 参数校验）。"""

from __future__ import annotations

import json
import os
import subprocess
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

import requests  # noqa: E402
import run as cli  # noqa: E402
from guet.client import SessionExpired, TronClassError  # noqa: E402
from guet.config import AppConfig, ConfigError, RuntimeConfig, load_config  # noqa: E402
from guet.realtime import LaneJob  # noqa: E402
from guet.runner import MaterialJob, VideoJob  # noqa: E402
from guet.scheduler import CourseCache  # noqa: E402
from guet.ticker import effective_threads  # noqa: E402
from guet.transport import HttpTransport  # noqa: E402
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
    def test_full_on_refresh_without_data_is_done(self):
        """初始未完成 → 时间线走完 → 复查才返回 full（且不带 data），必须判 done 而不是 failed。

        这是审查指出的原缺陷：旧代码在 `_replan()` 返回 False（没有缺口）时直接进失败分支，
        即使复查已经把 percent 抬到 100。
        """

        class LateFullServer:
            def __init__(self) -> None:
                self.full = False
                self.calls: list[tuple[int, int]] = []

            def activity_read(self, aid, cid=None):
                if self.full:
                    return {"completeness": "full", "data": {}}
                return {"completeness": "", "data": {"completeness": 0, "ranges": []}}

            def report_video(self, aid, start, end, cid=None):
                self.calls.append((start, end))
                self.full = True          # 上报后服务端记成 full，但回包里只有 part 且没有 data
                return {"completeness": "part", "data": {}}

        server = LateFullServer()
        runner = FakeRunner(server)
        clock = FakeClock()
        job = LaneJob(runner, Task(1, 300), 100, 100, clock=clock.now)
        job.prepare()
        self.assertFalse(job.done)        # 前置条件：确实是从「未完成」开始

        outcomes = []
        for _ in range(10):
            outcome = job.step()
            outcomes.append(outcome)
            if outcome in ("done", "failed"):
                break
        self.assertEqual(outcomes[-1], "done")
        self.assertTrue(job.done)
        self.assertFalse(job.failed)
        self.assertEqual(job.percent, 100)
        self.assertEqual(server.calls, [(0, 100), (100, 200), (200, 300)])


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

    def test_lock_release_does_not_close_other_files(self):
        """锁退出时不能关第二次 fd。

        必须在**持锁期间**打开别的文件：那时锁内部 fd 已被关闭，新文件会复用同一个编号，
        旧实现退出时再 close 一次就会把它关掉（随后写入报 OSError 9）。
        """
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "acc.lock"
            handles = []
            with run_lock(lock):
                for index in range(3):      # 多开几个，确保复用命中
                    handles.append(open(Path(raw) / f"other{index}.txt", "w", encoding="utf-8"))
            for index, handle in enumerate(handles):
                handle.write(f"ok{index}")  # 旧实现：这里 OSError 9
                handle.flush()
                handle.close()
            for index in range(3):
                self.assertEqual(
                    (Path(raw) / f"other{index}.txt").read_text(encoding="utf-8"), f"ok{index}"
                )

    def test_lock_not_broken_by_old_mtime(self):
        """老锁文件（mtime 10 小时前）只要原进程仍持锁，就必须继续拒绝。"""
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "acc.lock"
            with run_lock(lock):
                old = time.time() - 10 * 3600
                os.utime(lock, (old, old))
                with self.assertRaises(LockBusy):
                    with run_lock(lock):
                        pass

    def test_lock_blocks_other_process(self):
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "acc.lock"
            code = (
                "import sys, time\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from pathlib import Path\n"
                "from guet.util import run_lock\n"
                f"cm = run_lock(Path({str(lock)!r}))\n"
                "cm.__enter__()\n"
                "print('locked', flush=True)\n"
                "time.sleep(5)\n"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaises(LockBusy):
                    with run_lock(lock):
                        pass
            finally:
                holder.kill()
                holder.wait(timeout=10)
                if holder.stdout is not None:
                    holder.stdout.close()

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


class ProxyTest(unittest.TestCase):
    def capture(self, rt: RuntimeConfig) -> dict:
        transport = HttpTransport(rt, {})
        captured: dict = {}

        def fake_request(self, method, url, **kwargs):
            captured.update(kwargs)
            return mock.Mock(status_code=200, url=url, text="{}", headers={})

        with mock.patch.object(requests.Session, "request", fake_request):
            transport.request("GET", "/api/x")
        return captured

    def test_config_proxy_beats_env(self):
        env = {"HTTP_PROXY": "http://env-b:8080", "HTTPS_PROXY": "http://env-b:8080"}
        with mock.patch.dict(os.environ, env):
            captured = self.capture(RuntimeConfig(proxy="http://cfg-a:1080"))
            self.assertEqual(captured["proxies"],
                             {"http": "http://cfg-a:1080", "https": "http://cfg-a:1080"})
            merged = requests.Session().merge_environment_settings(
                "https://courses.guet.edu.cn/api/x", captured["proxies"] or {}, True, True, None
            )["proxies"]
            self.assertEqual(merged["https"], "http://cfg-a:1080")

    def test_env_proxy_still_used_when_unset(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://env-b:8080"}):
            captured = self.capture(RuntimeConfig(proxy=""))
            self.assertIsNone(captured["proxies"])
            merged = requests.Session().merge_environment_settings(
                "https://courses.guet.edu.cn/api/x", captured["proxies"] or {}, True, True, None
            )["proxies"]
            self.assertEqual(merged["https"], "http://env-b:8080")


class OnlyParsingTest(unittest.TestCase):
    def test_omitted_means_no_filter(self):
        self.assertIsNone(cli.parse_only(None))          # 参数没给

    def test_valid_forms(self):
        self.assertEqual(cli.parse_only("92918"), {92918})
        self.assertEqual(cli.parse_only("92918,92919"), {92918, 92919})
        self.assertEqual(cli.parse_only("92918 92919"), {92918, 92919})
        self.assertEqual(cli.parse_only("92918，92919"), {92918, 92919})
        self.assertEqual(cli.parse_only(" 92918 , 92919 "), {92918, 92919})
        self.assertEqual(cli.parse_only("92918,92918"), {92918})

    def test_empty_and_separator_only_are_rejected(self):
        """显式给了却解析不出 ID —— 必须报错，不能退化成「不限活动」。"""
        for bad in ("", "   ", ",", "，", ",,", ", ，,"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                cli.parse_only(bad)

    def test_invalid_tokens_are_rejected(self):
        for bad in ("abc", "92918,abc", "-1", "1.5", "all", "92918;92919"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                cli.parse_only(bad)


class ConfigTypeTest(unittest.TestCase):
    def build(self, config_text: str,
              accounts_text: str = "accounts:\n  - name: acc01\n    enabled: true\n") -> AppConfig:
        root = Path(tempfile.mkdtemp())
        (root / "config.yaml").write_text(config_text, encoding="utf-8")
        (root / "accounts.yaml").write_text(accounts_text, encoding="utf-8")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        return load_config(root)

    def test_string_numbers_are_coerced(self):
        app = self.build('chunk_seconds: "120"\ntarget_percent: "80"\nrps: "2.5"\ntelemetry: "false"\n')
        self.assertEqual(app.runtime.chunk_seconds, 120)
        self.assertEqual(app.runtime.target_percent, 80)
        self.assertAlmostEqual(app.runtime.rps, 2.5)
        self.assertFalse(app.runtime.telemetry)

    def test_bad_numbers_become_config_error(self):
        cases = {
            "chunk_seconds: nope\n": "chunk_seconds",
            "target_percent: nope\n": "target_percent",
            "rps: fast\n": "rps",
            "telemetry: maybe\n": "telemetry",
            "account_jobs: []\n": "account_jobs",
        }
        for text, field in cases.items():
            with self.assertRaises(ConfigError) as ctx:
                self.build(text)
            self.assertIn(field, str(ctx.exception))

    def test_non_finite_numbers_are_rejected(self):
        """YAML 的 .inf / .nan 是合法字面量，不能让它漏进运行时。"""
        for text, field in (
            ("chunk_seconds: .inf\n", "chunk_seconds"),
            ("chunk_seconds: .nan\n", "chunk_seconds"),
            ("timeout: .nan\n", "timeout"),
            ("rps: .inf\n", "rps"),
            ("gap_seconds: [0, .inf]\n", "gap_seconds"),
        ):
            with self.assertRaises(ConfigError) as ctx:
                self.build(text)
            self.assertIn(field, str(ctx.exception))

    def test_float_never_silently_truncated(self):
        for text, field in (
            ("target_percent: 80.9\n", "target_percent"),
            ("chunk_seconds: 120.5\n", "chunk_seconds"),
            ('target_percent: "80.9"\n', "target_percent"),
        ):
            with self.assertRaises(ConfigError) as ctx:
                self.build(text)
            self.assertIn(field, str(ctx.exception))
        # 整数形态的浮点（80.0）是允许的
        self.assertEqual(self.build("target_percent: 80.0\n").runtime.target_percent, 80)

    def test_non_string_fields_rejected(self):
        for text, field in (
            ("base_url: 123\n", "base_url"),
            ("user_agent: 42\n", "user_agent"),
            ("profile_dir: [a, b]\n", "profile_dir"),
        ):
            with self.assertRaises(ConfigError) as ctx:
                self.build(text)
            self.assertIn(field, str(ctx.exception))

    def test_out_of_range_values_rejected(self):
        for text, field in (
            ("rps: 0\n", "rps"),
            ("timeout: -1\n", "timeout"),
            ("account_jobs: 0\n", "account_jobs"),
            ("gap_seconds: [-1, 2]\n", "gap_seconds"),
            ("base_url: ftp://x\n", "base_url"),
        ):
            with self.assertRaises(ConfigError) as ctx:
                self.build(text)
            self.assertIn(field, str(ctx.exception))

    def test_bad_account_course_ids(self):
        for bad in ("[abc]", "[29066.9]", '[29066, "x"]'):
            with self.assertRaises(ConfigError) as ctx:
                self.build("", f"accounts:\n  - name: acc01\n    enabled: true\n    course_ids: {bad}\n")
            self.assertIn("course_ids", str(ctx.exception))

    def test_bad_account_enabled(self):
        with self.assertRaises(ConfigError) as ctx:
            self.build("", "accounts:\n  - name: acc01\n    enabled: maybe\n")
        self.assertIn("enabled", str(ctx.exception))

    def test_explicit_config_path_must_exist(self):
        root = Path(tempfile.mkdtemp())
        (root / "accounts.yaml").write_text(
            "accounts:\n  - name: acc01\n    enabled: true\n", encoding="utf-8"
        )
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        with self.assertRaises(ConfigError) as ctx:
            load_config(root, root / "mis-typed.yaml", root / "accounts.yaml")
        self.assertIn("mis-typed.yaml", str(ctx.exception))
        # 默认配置文件不存在时仍可以用内置默认值
        self.assertEqual(load_config(root).runtime.rps, 3.0)


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

    def call(self, extra, only: str = "1"):
        argv = [
            "--config", str(self.root / "config.yaml"),
            "--accounts", str(self.root / "accounts.yaml"),
            "run",
        ]
        if only is not None:
            argv += ["--only", only]
        return cli.main(argv + extra)

    def test_target_out_of_range_rejected(self):
        self.assertEqual(self.call(["--target", "101"]), cli.EXIT_ERROR)

    def test_bad_only_rejected_not_widened(self):
        """--only 写错或只给分隔符，必须报错退出，绝不能退化成「不限活动」。"""
        for bad in ("abc", "92918,abc", "", ",", "，"):
            with self.subTest(only=bad):
                self.assertEqual(self.call([], only=bad), cli.EXIT_ERROR)

    def test_omitted_only_is_allowed(self):
        """完全不写 --only 不会被当成参数错误（打桩 run_all，绝不真的联网/刷课）。"""
        with mock.patch.object(cli.scheduler, "run_all", return_value=[]) as run_all, \
                mock.patch.object(cli.report, "print_outcomes"), \
                mock.patch.object(cli.report, "write_result"):
            code = self.call([], only=None)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(run_all.call_args.kwargs.get("only"), None)

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
