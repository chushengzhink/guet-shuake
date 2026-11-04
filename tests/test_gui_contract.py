"""针对 2026-11-04 评估报告 P1 的回归测试（断言修复后的正确行为）。

覆盖：F01 停止路由、F02 进度回调链路、F03 非法账号不落盘、F04 只跑勾选账号、
F05 状态互斥与 run_id、F06 无进展时的补报上限。
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet import cancel, scheduler
from guet.client import TronClassError
from guet.config import ConfigError
from guet.gui import server as gui_server
from guet.runner import VideoJob
from guet.scheduler import AccountOutcome
from guet.util import shutdown_loggers
from tests.test_realtime import FakeClock, FakeRunner, Task


class AppStateTest(unittest.TestCase):
    def setUp(self):
        shutdown_loggers()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.state = gui_server.AppState(self.root)
        self.state.ensure_files()

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    # ---------------- F03 ----------------
    def test_invalid_account_name_not_written(self):
        self.state.add_account("acc01")
        before = self.state.accounts_path.read_text(encoding="utf-8")

        with self.assertRaises(ConfigError):
            self.state.add_account("bad name")          # 含空格，配置不合法

        after = self.state.accounts_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)                 # 文件没被改
        self.assertIsNotNone(self.state.app)            # 内存配置仍可用
        self.assertEqual([a.name for a in self.state.app.accounts], ["acc01"])
        self.assertFalse(list(self.root.glob("*.pending")))   # 临时文件也清了

    # ---------------- F05 ----------------
    def test_state_transitions_are_mutually_exclusive(self):
        run_id = self.state.begin("running")
        with self.assertRaises(ConfigError):
            self.state.begin("running")                 # 运行中不能再启动
        with self.assertRaises(ConfigError):
            self.state.begin("logging-in")              # 运行中不能登录

        self.state.finish(run_id)
        self.assertEqual(self.state.status, "idle")

    def test_old_run_cannot_reset_new_status(self):
        first = self.state.begin("running")
        self.state.finish(first)
        second = self.state.begin("running")
        self.state.finish(first)                        # 旧 run 收尾
        self.assertEqual(self.state.status, "running")  # 不能把新运行置 idle
        self.state.finish(second)

    def test_stop_marks_stopping(self):
        run_id = self.state.begin("running")
        cancel.reset()
        try:
            self.assertEqual(self.state.mark_stopping(), "stopping")
            self.assertTrue(cancel.requested)
        finally:
            cancel.reset()
            self.state.finish(run_id)


class HttpContractTest(unittest.TestCase):
    """F01：前端调用方式必须和后端路由对得上。"""

    def setUp(self):
        shutdown_loggers()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.state = gui_server.AppState(self.root)
        self.state.ensure_files()
        self.server = gui_server.build_server(self.state, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.token = self.state.token

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutdown_loggers()
        self.tmp.cleanup()

    def request(self, method: str, path: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            try:
                data = json.loads(body) if body else {}
            except ValueError:
                data = {"raw": body}
            return resp.status, data
        finally:
            conn.close()

    def test_stop_requires_post(self):
        cancel.reset()
        status, _ = self.request("GET", f"/api/stop?t={self.token}")
        self.assertEqual(status, 404)                   # 旧前端就是死在这

        status, data = self.request("POST", f"/api/stop?t={self.token}")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "idle")    # 空闲时停止是无操作
        cancel.reset()

    def test_logs_accept_query_with_token(self):
        """F01 的连带问题：?after=0 再拼一个 ?t= 会让 token 丢失。"""
        status, data = self.request("GET", f"/api/logs?after=0&t={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("lines", data)

    def test_bad_token_rejected(self):
        status, _ = self.request("GET", "/api/status?t=wrong")
        self.assertEqual(status, 403)


class ProgressCallbackTest(unittest.TestCase):
    """F02：scheduler 必须按账号包装回调，否则 GUI 的 (name, result) 签名对不上。"""

    def test_run_all_wraps_callback_per_account(self):
        captured = {}

        def fake_run_one(app, account, cache, limiter, only, dry_run, callback=None):
            captured[account.name] = callback
            return AccountOutcome(account=account.name, ok=True)

        received = []

        def on_result(name, result):
            received.append((name, result))

        app = mock.Mock()
        app.runtime.max_browser_contexts = 1
        app.runtime.course_cache_ttl = 60
        app.runtime.global_rps = 8.0
        app.runtime.account_jobs = 2
        app.runtime.engine = "http"
        app.runtime.speed = "normal"
        app.course_cache_path.return_value = Path(tempfile.mkdtemp()) / "cache.json"

        acc_a = mock.Mock(name="acc_a")
        acc_a.name = "accA"
        acc_b = mock.Mock(name="acc_b")
        acc_b.name = "accB"
        app.enabled_accounts.return_value = [acc_a, acc_b]

        with mock.patch.object(scheduler, "run_one", fake_run_one):
            scheduler.run_all(app, [acc_a, acc_b], on_result=on_result)

        self.assertEqual(set(captured), {"accA", "accB"})
        # runner 只传 result，包装后应带上账号名
        captured["accA"]("RESULT-A")
        self.assertEqual(received, [("accA", "RESULT-A")])


class InconsistentProgressTest(unittest.TestCase):
    """F06：区间已满但百分比不足时，不能整段重发、也不能无限重试。"""

    class InconsistentServer:
        def __init__(self):
            self.calls: list[tuple[int, int]] = []

        def activity_read(self, aid, cid=None):
            return {"completeness": "part",
                    "data": {"completeness": 99, "ranges": [[0, 300]]}}

        def report_video(self, aid, start, end, cid=None):
            self.calls.append((start, end))
            if end - start > 125:
                raise TronClassError(
                    'POST -> 400 {"message":"The set time duration is too long, maximum is 125s"}'
                )
            return {"completeness": "part",
                    "data": {"completeness": 99, "ranges": [[0, 300]]}}

    def test_bounded_retry_and_respects_span_limit(self):
        server = self.InconsistentServer()
        runner = FakeRunner(server)
        clock = FakeClock()
        job = VideoJob(runner, Task(1, 300), chunk=120, target=100)
        job.prepare()

        for _ in range(40):                          # 上限 40 步，正常应远小于
            if job.step() in ("done", "failed"):
                break

        self.assertTrue(job.failed, job.detail)
        self.assertLess(len(server.calls), 20, f"请求次数没有收敛：{len(server.calls)}")
        self.assertTrue(all(e - s <= 125 for s, e in server.calls),
                        f"出现了超长段：{server.calls}")


class GuiStartValidationTest(unittest.TestCase):
    """F04 / F07：只执行勾选的账号，参数非法直接拒绝。"""

    def setUp(self):
        shutdown_loggers()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.state = gui_server.AppState(self.root)
        self.state.ensure_files()
        self.state.add_account("acc01")
        self.state.add_account("acc02")

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    def call(self, body):
        return gui_server.api_start(self.state, body)

    def test_rejects_bad_target(self):
        with self.assertRaises(ConfigError):
            self.call({"accounts": ["acc01"], "courses": [1], "target": 101})
        with self.assertRaises(ConfigError):
            self.call({"accounts": ["acc01"], "courses": [1], "speed": "fast"})

    def test_rejects_disabled_account(self):
        for acc in self.state.app.accounts:
            if acc.name == "acc02":
                acc.enabled = False
        with self.assertRaises(ConfigError):
            self.call({"accounts": ["acc02"], "courses": [1]})

    def test_only_listed_accounts_run(self):
        started = {}

        def fake_run_all(app, accounts, **kwargs):
            started["accounts"] = [a.name for a in accounts]
            return [AccountOutcome(account=a.name, ok=True) for a in accounts]

        with mock.patch.object(gui_server.scheduler, "run_all", fake_run_all):
            self.call({"accounts": ["acc01"], "courses": [1]})
            for _ in range(50):
                if started:
                    break
                threading.Event().wait(0.02)
        self.assertEqual(started.get("accounts"), ["acc01"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
