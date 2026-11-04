"""离线自测：不碰网络，用抓包导出的 fixtures 验证解析、区间数学与降档逻辑。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet.config import ConfigError, load_config  # noqa: E402
from guet.client import TronClassError  # noqa: E402
from guet.planner import ACTION_MANUAL, ACTION_MATERIAL, ACTION_SKIP, ACTION_VIDEO, build_plan  # noqa: E402
from guet.progress import complement, coverage_percent, covered_seconds, merge_ranges, plan_chunks  # noqa: E402
from guet.runner import MaterialJob, VideoJob, _server_max_span  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ----------------------------------------------------------------- 测试替身
class NullTelemetry:
    def video(self, *args, **kwargs):
        pass

    def video_view(self, *args, **kwargs):
        pass

    def material(self, *args, **kwargs):
        pass


class FakeServer:
    """模拟 activities-read：区间并集 / 整数百分比 / 可选的最大可接受跨度。"""

    def __init__(self, duration: int, max_span: int | None = None, ignore_all: bool = False,
                 reject_over: int | None = None):
        self.duration = duration
        self.max_span = max_span
        self.ignore_all = ignore_all
        self.reject_over = reject_over
        self.ranges: list[list[int]] = []
        self.uploads: list[int] = []
        self.upload_total = 0
        self.calls: list[tuple[str, int, int]] = []

    @property
    def percent(self) -> int:
        if self.ignore_all:
            return 0
        return min(100, int(covered_seconds(self.ranges) * 100 / self.duration))

    def _state(self, percent: int) -> dict:
        return {
            "completeness": "full" if percent >= 100 else ("part" if self.ranges or self.uploads else ""),
            "data": {"completeness": percent, "ranges": [list(r) for r in self.ranges]},
            "activity_id": 1,
            "activity_type": "learning_activity",
        }

    def activity_read(self, aid, cid=None):
        return self._state(self.percent)

    def report_video(self, aid, start, end, cid=None):
        self.calls.append(("video", start, end))
        if self.reject_over is not None and end - start > self.reject_over:
            raise TronClassError(
                'POST /api/course/activities-read/1 -> 400 {"message":"Log failed, caused： '
                f'The set time duration（{end - start}s） is too long, maximum is {self.reject_over}s"}}'
            )
        if not self.ignore_all and (self.max_span is None or end - start <= self.max_span):
            merged = merge_ranges(list(self.ranges) + [[start, end]])
            self.ranges = [[s, e] for s, e in merged]
        return self._state(self.percent)

    def report_upload(self, aid, upload_id, cid=None):
        self.calls.append(("upload", upload_id, 0))
        if not self.ignore_all and upload_id not in self.uploads:
            self.uploads.append(upload_id)
        percent = 100 if self.upload_total and len(self.uploads) >= self.upload_total else int(
            (len(self.uploads) / max(1, self.upload_total)) * 100
        )
        return {
            "completeness": "full" if percent >= 100 else "part",
            "data": {"completeness": percent, "upload_ids": list(self.uploads)},
        }


class FakeRunner:
    def __init__(self, client):
        self.client = client
        self.request_count = 0
        self.max_span = 125
        self.telemetry = NullTelemetry()
        self.log = logging.getLogger("test-runner")


class StubClient:
    def activity_detail(self, aid, cid=None):
        return {}


class Task:
    def __init__(self, aid=92918, duration=542, uploads=None, cid=29066):
        self.activity_id = aid
        self.course_id = cid
        self.module_id = 21203
        self.duration = duration
        self.upload_ids = uploads or []
        self.type = "online_video"
        self.title = "概念"
        self.label = f"{aid} 概念"


# ----------------------------------------------------------------- 区间数学
class ProgressTest(unittest.TestCase):
    def test_merge_and_complement(self):
        self.assertEqual(merge_ranges([[0, 4], [4, 10], [20, 25]]), [(0, 10), (20, 25)])
        self.assertEqual(complement([[0, 10], [20, 25]], 30), [(10, 20), (25, 30)])
        self.assertEqual(complement([], 542), [(0, 542)])
        self.assertEqual(complement([[0, 542]], 542), [])

    def test_split_and_percent(self):
        self.assertEqual(plan_chunks([], 300, 120), [(0, 120), (120, 240), (240, 300)])
        self.assertEqual(plan_chunks([], 300, None), [(0, 300)])
        self.assertAlmostEqual(coverage_percent([[0, 4]], 542), 0.74, places=2)

    def test_float_duration_is_floored(self):
        self.assertEqual(plan_chunks([], 743.4, None), [(0, 743)])


# ----------------------------------------------------------------- 任务规划
class PlannerTest(unittest.TestCase):
    def build(self):
        return build_plan(
            StubClient(),
            29066,
            None,
            activities=fixture("activities.json")["activities"],
            reads={int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]},
            modules=fixture("modules.json")["modules"],
            completeness=fixture("my_completeness.json"),
            name="基础工业工程",
        )

    def test_counts_match_har(self):
        plan = self.build()
        counts = plan.summary()
        self.assertEqual(counts["total"], 60)
        self.assertEqual(counts["video"] + counts["material"] + counts["skip"] + counts["manual"],
                         counts["total"])
        # 抓包里 30 个 video 中有 7 个已是满进度、18 个 material 全部未完成、9 个 homework 已提交
        self.assertEqual(counts["video"], 23)
        self.assertEqual(counts["material"], 18)
        self.assertEqual(counts["skip"], 16)
        self.assertEqual(counts["manual"], 3)
        self.assertEqual(sum(1 for t in plan.tasks if t.type == "online_video"), 30)
        self.assertEqual(sum(1 for t in plan.tasks if t.type == "material"), 18)
        self.assertEqual(sum(1 for t in plan.tasks if t.type == "homework"), 12)

    def test_video_duration_and_uploads(self):
        plan = self.build()
        task = next(t for t in plan.tasks if t.activity_id == 92918)
        self.assertEqual(task.action, ACTION_VIDEO)
        self.assertEqual(task.duration, 542)
        self.assertEqual(task.upload_ids, [2764500])
        self.assertEqual(task.module_name, "第四章程序分析")

    def test_completed_and_manual_split(self):
        plan = self.build()
        done = [t for t in plan.tasks if t.action == ACTION_SKIP]
        self.assertTrue(any(t.activity_id == 92913 for t in done))
        homeworks = [t for t in plan.tasks if t.type == "homework"]
        self.assertTrue(all(t.action in (ACTION_MANUAL, ACTION_SKIP) for t in homeworks))
        self.assertTrue(all(t.action != ACTION_VIDEO for t in homeworks))
        self.assertGreater(len(plan.actionable()), 20)


# ----------------------------------------------------------------- 上报状态机
class VideoJobTest(unittest.TestCase):
    def run_job(self, server, chunk=None, target=100, limit=500):
        runner = FakeRunner(server)
        job = VideoJob(runner, Task(duration=server.duration), chunk, target)
        job.prepare()
        for _ in range(limit):
            outcome = job.step()
            if outcome in ("done", "failed"):
                break
        return job

    def test_probe_single_request_marks_full(self):
        server = FakeServer(542)
        job = self.run_job(server, chunk=None)
        self.assertTrue(job.done, job.detail)
        self.assertEqual(job.percent, 100)
        self.assertEqual(len(server.calls), 1)

    def test_chunked_reporting(self):
        server = FakeServer(542)
        job = self.run_job(server, chunk=120)
        self.assertTrue(job.done, job.detail)
        self.assertEqual(job.percent, 100)
        self.assertGreaterEqual(len(server.calls), 5)

    def test_degrades_when_server_caps_span(self):
        server = FakeServer(542, max_span=100)
        job = self.run_job(server, chunk=None)
        self.assertTrue(job.done, job.detail)
        self.assertEqual(job.percent, 100)
        self.assertGreater(len(server.calls), 1)
        self.assertTrue(all(end - start <= 100 for _, start, end in server.calls[1:]))

    def test_fails_fast_when_server_ignores_everything(self):
        server = FakeServer(542, ignore_all=True)
        job = self.run_job(server, chunk=120)
        self.assertTrue(job.failed)
        self.assertLess(len(server.calls), 60)

    def test_learns_server_max_span_from_400(self):
        """实测报文：maximum is 125s —— 一次 400 就该学会上限，不再逐级试。"""
        server = FakeServer(542, reject_over=100)
        runner = FakeRunner(server)
        job = VideoJob(runner, Task(duration=542), chunk=300, target=100)
        job.prepare()
        for _ in range(100):
            if job.step() in ("done", "failed"):
                break
        self.assertTrue(job.done, job.detail)
        self.assertEqual(job.percent, 100)
        self.assertEqual(runner.max_span, 100)
        self.assertTrue(all(end - start <= 100 for _, start, end in server.calls[1:] if end - start > 0))

    def test_parse_max_span(self):
        msg = ('POST /api/course/activities-read/92918 -> 400 {"message":"Log failed, caused： '
               'The set time duration（538s） is too long, maximum is 125s"}')
        self.assertEqual(_server_max_span(msg), 125)
        self.assertIsNone(_server_max_span("some random error"))
        self.assertIsNone(_server_max_span("maximum is 99999s"))

    def test_target_80_stops_early(self):
        server = FakeServer(1000)
        job = self.run_job(server, chunk=100, target=80)
        self.assertTrue(job.done)
        self.assertGreaterEqual(job.percent, 80)
        self.assertLessEqual(len(server.calls), 9)


class MaterialJobTest(unittest.TestCase):
    def test_all_uploads_marked(self):
        server = FakeServer(1)
        server.upload_total = 3
        runner = FakeRunner(server)
        task = Task(duration=0, uploads=[11, 12, 13])
        task.type = "material"
        job = MaterialJob(runner, task, 100)
        job.prepare()
        for _ in range(10):
            if job.step() in ("done", "failed"):
                break
        self.assertTrue(job.done, job.detail)
        self.assertEqual(job.percent, 100)
        self.assertEqual(len(server.calls), 3)


# ----------------------------------------------------------------- 配置
class ConfigTest(unittest.TestCase):
    def write(self, tmp: Path, accounts_yaml: str, config_yaml: str = "") -> Path:
        (tmp / "accounts.yaml").write_text(accounts_yaml, encoding="utf-8")
        (tmp / "config.yaml").write_text(config_yaml, encoding="utf-8")
        return tmp

    def test_defaults_merge_and_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = self.write(
                Path(raw),
                "defaults:\n  course_ids: [29066]\n  speed: normal\n"
                "accounts:\n  - name: acc01\n    enabled: true\n"
                "  - name: acc02\n    course_ids: [1, 2]\n    speed: human\n    engine: browser\n",
                "account_jobs: 2\nrps: 5\n",
            )
            app = load_config(tmp)
            self.assertEqual(app.runtime.account_jobs, 2)
            self.assertEqual(app.runtime.rps, 5)
            self.assertEqual([a.name for a in app.enabled_accounts()], ["acc01", "acc02"])
            self.assertEqual(app.account("acc01").course_ids, [29066])
            self.assertEqual(app.account("acc02").course_ids, [1, 2])
            self.assertEqual(app.account("acc02").effective_speed(app.runtime), "human")
            self.assertEqual(app.account("acc02").effective_engine(app.runtime), "browser")
            self.assertTrue(str(app.state_path(app.account("acc01"))).endswith("state.acc01.json"))

    def test_bad_account_name_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = self.write(Path(raw), "accounts:\n  - name: '账号一'\n    enabled: true\n")
            with self.assertRaises(ConfigError):
                load_config(tmp)

    def test_missing_accounts_file(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ConfigError):
                load_config(Path(raw))

    def test_no_enabled_accounts(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = self.write(Path(raw), "accounts:\n  - name: acc01\n    enabled: false\n")
            with self.assertRaises(ConfigError):
                load_config(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
