"""向导自测：纯函数 + 用假传输层跑完整个 `start.py` 流程。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet import scheduler, wizard  # noqa: E402
from guet.client import TronClassClient  # noqa: E402
from guet.config import load_config  # noqa: E402
from guet.runner import SPEED_PROFILES  # noqa: E402
from guet.scheduler import AccountOutcome  # noqa: E402
from guet.util import shutdown_loggers  # noqa: E402
from tests.test_integration import COURSE, FakeTransport, fixture  # noqa: E402


class SelectionTest(unittest.TestCase):
    def test_all_forms(self):
        self.assertEqual(wizard.parse_selection("1", 5), [0])
        self.assertEqual(wizard.parse_selection("1,3", 5), [0, 2])
        self.assertEqual(wizard.parse_selection(" 2 ，4 ", 5), [1, 3])
        self.assertIsNone(wizard.parse_selection("", 5))
        self.assertIsNone(wizard.parse_selection("all", 5))
        self.assertIsNone(wizard.parse_selection("全部", 5))

    def test_bad_input(self):
        for bad in ["0", "9", "x", "1,x"]:
            with self.assertRaises(ValueError):
                wizard.parse_selection(bad, 5)


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "accounts.yaml").write_text(
            "defaults:\n  course_ids: [29066]\n  speed: normal\n"
            "accounts:\n  - name: acc01\n    enabled: true\n"
            "  - name: acc02\n    enabled: true\n    speed: human\n",
            encoding="utf-8",
        )
        (self.root / "config.yaml").write_text("rps: 3\nchunk_seconds: 120\n", encoding="utf-8")
        self.app = load_config(self.root)

    def tearDown(self):
        shutdown_loggers()
        self.tmp.cleanup()

    def plan(self):
        activities = fixture("activities.json")["activities"]
        reads = {int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]}
        transport = FakeTransport(activities, reads)
        client = TronClassClient(transport, self.app.runtime)
        return wizard.build_plan(client, COURSE, self.app.runtime, name="基础工业工程")

    def test_eta_grows_with_slower_speed(self):
        plan = self.plan()
        normal = wizard.estimate_seconds(plan, self.app.runtime, self.app.account("acc01"))
        human = wizard.estimate_seconds(plan, self.app.runtime, self.app.account("acc02"))
        probe_app = self.app.account("acc01")
        probe_app.speed = "probe"
        probe = wizard.estimate_seconds(plan, self.app.runtime, probe_app)
        self.assertGreater(normal, 0)
        self.assertGreater(human, normal)
        self.assertLess(probe, normal)

    def test_describe_plan_counts(self):
        info = wizard.describe_plan(self.plan(), self.app.runtime, self.app.account("acc01"))
        self.assertEqual(info["course_id"], COURSE)
        self.assertEqual(info["videos"], 23)
        self.assertEqual(info["materials"], 18)
        self.assertGreater(info["uploads"], 40)
        self.assertGreater(info["video_seconds"], 10000)


class WizardFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "accounts.yaml").write_text(
            "defaults:\n  course_ids: []\n  semester_id: \"14\"\n  academic_year_id: \"5\"\n"
            "accounts:\n  - name: acc01\n    enabled: true\n",
            encoding="utf-8",
        )
        (self.root / "config.yaml").write_text(
            "rps: 1000\ngap_seconds: [0, 0]\nchunk_seconds: 600\ntelemetry: false\n",
            encoding="utf-8",
        )
        activities = fixture("activities.json")["activities"]
        reads = {int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]}
        self.transport = FakeTransport(activities, reads)
        self.calls: list = []

        self._orig = (
            wizard.session.cookies_from_profile,
            scheduler.make_client,
            scheduler.run_all,
            wizard.browser_ready,
        )

        # 别让测试真的去启动 Chromium：既慢，退出时还会留 Playwright 的
        # "Task was destroyed / TargetClosedError" 噪音
        wizard.browser_ready = lambda: (True, "test-chromium")  # type: ignore[assignment]
        wizard.session.cookies_from_profile = lambda app, acc: {"session": "x"}  # type: ignore[assignment]

        def fake_make_client(app, account, limiter, global_limiter):
            return TronClassClient(self.transport, app.runtime, limiter=limiter), self.transport

        scheduler.make_client = fake_make_client  # type: ignore[assignment]

        def fake_run_all(app, accounts, only=None, dry_run=False):
            self.calls.append({
                "accounts": [a.name for a in accounts],
                "course_ids": [list(a.course_ids) for a in accounts],
                "speed": app.runtime.speed,
                "target": app.runtime.target_percent,
                "dry_run": dry_run,
            })
            return [AccountOutcome(account=a.name, ok=True) for a in accounts]

        scheduler.run_all = fake_run_all  # type: ignore[assignment]

    def tearDown(self):
        (wizard.session.cookies_from_profile, scheduler.make_client,
         scheduler.run_all, wizard.browser_ready) = self._orig
        shutdown_loggers()
        self.tmp.cleanup()

    def run_wizard(self, answers, assume_yes=False):
        queue = list(answers)

        def ask(prompt: str) -> str:
            print(f"[ask] {prompt}", file=io.StringIO())
            return queue.pop(0) if queue else ""

        return wizard.run_wizard(self.root, assume_yes=assume_yes, ask=ask)

    def test_happy_path(self):
        code = self.run_wizard(["1", "", "", "y", ""])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["accounts"], ["acc01"])
        self.assertEqual(call["course_ids"], [[COURSE]])
        self.assertEqual(call["speed"], "normal")
        self.assertEqual(call["target"], 100)
        self.assertFalse(call["dry_run"])

    def test_probe_choice_and_target(self):
        code = self.run_wizard(["1", "1", "80", "y", ""])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0]["speed"], "probe")
        self.assertEqual(self.calls[0]["target"], 80)

    def test_abort_at_confirm(self):
        code = self.run_wizard(["1", "", "", "n", ""])
        self.assertEqual(code, 1)      # 没执行，返回非 0
        self.assertEqual(self.calls, [])

    def test_assume_yes_uses_defaults(self):
        code = self.run_wizard([], assume_yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0]["course_ids"], [[COURSE]])


class AccountsFileTest(unittest.TestCase):
    def test_creates_valid_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = wizard.ensure_accounts_file(root, lambda prompt: "")
            app = load_config(root)
            self.assertEqual([a.name for a in app.enabled_accounts()], ["acc01"])
            self.assertTrue(path.exists())
            self.assertFalse(app.accounts[0].course_ids)   # 空表示自动发现

    def test_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "accounts.yaml").write_text(
                "accounts:\n  - name: mine\n    enabled: true\n", encoding="utf-8"
            )
            wizard.ensure_accounts_file(root, lambda prompt: "other")
            self.assertEqual([a.name for a in load_config(root).enabled_accounts()], ["mine"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
