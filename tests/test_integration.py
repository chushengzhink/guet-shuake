"""集成自测：用假传输层把 scheduler -> runner -> client 整条链路跑通（不联网）。

验证点：
* 从活动列表自动挑出未完成的视频与资料；
* 视频区间被补满、资料 upload 全部上报；
* 埋点镜像被调用；
* state.<账号>.json / result 结构正确；
* 一个账号失败不会影响另一个账号（错误隔离）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet import scheduler  # noqa: E402
from guet.client import TronClassClient  # noqa: E402
from guet.config import load_config  # noqa: E402
from guet.progress import merge_ranges  # noqa: E402
from guet.transport import HttpResult  # noqa: E402
from guet.util import shutdown_loggers  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COURSE = 29066


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransport:
    """按路径路由到 fixtures，把 activities-read 的写入真的记在内存里。"""

    limiter = None

    def __init__(self, activities: list[dict], reads: dict[int, dict], log: list | None = None):
        self.activities = {int(a["id"]): a for a in activities}
        self.state: dict[int, dict[str, Any]] = {}
        for aid, read in reads.items():
            data = read.get("data") or {}
            self.state[aid] = {
                "ranges": [list(r) for r in (data.get("ranges") or [])],
                "uploads": list(data.get("upload_ids") or []),
                "full": read.get("completeness") == "full",
            }
        self.calls: list[tuple[str, str]] = log if log is not None else []
        self.session_ok = True
        self.mgs_jwt_calls = 0
        self.stats_calls = 0

    # -- 内部工具
    def _duration(self, aid: int) -> int:
        best = 0
        for upload in self.activities.get(aid, {}).get("uploads") or []:
            for video in upload.get("videos") or []:
                best = max(best, int(float(video.get("duration") or 0)))
        return best

    def _uploads(self, aid: int) -> list[int]:
        return [int(u["id"]) for u in (self.activities.get(aid, {}).get("uploads") or []) if u.get("id")]

    def _video_read(self, aid: int) -> dict:
        item = self.state.setdefault(aid, {"ranges": [], "uploads": [], "full": False})
        duration = self._duration(aid)
        covered = sum(e - s for s, e in merge_ranges(item["ranges"]))
        percent = 100 if item["full"] else (min(100, covered * 100 // duration) if duration else 0)
        return {
            "activity_id": aid,
            "activity_type": "learning_activity",
            "completeness": "full" if percent >= 100 else ("part" if item["ranges"] else ""),
            "data": {"completeness": percent, "ranges": [list(r) for r in item["ranges"]]},
        }

    def _material_read(self, aid: int) -> dict:
        item = self.state.setdefault(aid, {"ranges": [], "uploads": [], "full": False})
        total = len(self._uploads(aid)) or 1
        percent = 100 if item["full"] else int(len(item["uploads"]) * 100 / total)
        return {
            "activity_id": aid,
            "activity_type": "learning_activity",
            "completeness": "full" if percent >= 100 else ("part" if item["uploads"] else ""),
            "data": {"completeness": percent, "upload_ids": list(item["uploads"])},
        }

    # -- Transport 协议
    def request(self, method: str, path: str, *, params=None, json_body=None, body=None,
                content_type=None, referer=None, headers=None) -> HttpResult:
        self.calls.append((method, path))
        clean = path.split("?")[0]

        if clean == "/api/config":
            if not self.session_ok:
                return HttpResult(403, path, "403: Forbidden")
            return HttpResult(200, path, "{}")

        if clean == "/api/courses/29066/activities":
            return HttpResult(200, path, json.dumps(fixture("activities.json")))
        if clean == "/api/courses/29066/modules":
            return HttpResult(200, path, json.dumps(fixture("modules.json")))
        if clean == f"/api/courses/{COURSE}":
            return HttpResult(200, path, json.dumps(fixture("course_meta.json")))
        if clean == "/api/my-courses":
            return HttpResult(200, path, json.dumps({"courses": [{
                "id": COURSE,
                "name": "基础工业工程",
                "display_name": "基础工业工程（挑战性学习课程）-2610082",
                "course_code": "2610082",
                "course_avg_completeness": 28.11,
            }]}))
        if clean == "/api/my-semesters":
            return HttpResult(200, path, json.dumps({"semesters": [
                {"id": 14, "academic_year_id": 5, "is_active": True, "name": "2026-2027第1学期"},
            ]}))
        if clean == "/api/my-academic-years":
            return HttpResult(200, path, json.dumps({"academic_years": [
                {"id": 5, "is_active": True, "name": "2026-2027"},
            ]}))
        if clean == f"/api/course/{COURSE}/my-completeness":
            return HttpResult(200, path, json.dumps(fixture("my_completeness.json")))
        if clean == f"/api/course/{COURSE}/activity-reads-for-user":
            reads = []
            for aid in self.activities:
                atype = self.activities[aid].get("type")
                reads.append(self._material_read(aid) if atype == "material" else self._video_read(aid))
            return HttpResult(200, path, json.dumps({"activity_reads": reads}))

        if clean.startswith("/api/course/activity-read/"):
            aid = int(clean.rsplit("/", 1)[1])
            atype = self.activities.get(aid, {}).get("type")
            payload = self._material_read(aid) if atype == "material" else self._video_read(aid)
            return HttpResult(200, path, json.dumps({"activity_read": payload}))

        if clean.startswith("/api/course/activities-read/"):
            aid = int(clean.rsplit("/", 1)[1])
            item = self.state.setdefault(aid, {"ranges": [], "uploads": [], "full": False})
            if isinstance(json_body, dict) and "upload_id" in json_body:
                uid = int(json_body["upload_id"])
                if uid not in item["uploads"]:
                    item["uploads"].append(uid)
                return HttpResult(201, path, json.dumps(self._material_read(aid)))
            start = int((json_body or {}).get("start") or 0)
            end = int((json_body or {}).get("end") or 0)
            merged = merge_ranges(list(item["ranges"]) + [[start, end]])
            item["ranges"] = [[s, e] for s, e in merged]
            return HttpResult(201, path, json.dumps(self._video_read(aid)))

        if clean.startswith("/api/activities/"):
            aid = int(clean.rsplit("/", 1)[1])
            activity = dict(self.activities.get(aid, {}))
            return HttpResult(200, path, json.dumps(activity))

        if clean == f"/course/{COURSE}/learning-activity":
            self.mgs_jwt_calls += 1
            html = ('<script>window.statisticsSettings = { userId: "119405", orgId: "1", '
                    'departmentId: "265", mgsJwt: "FAKEJWT" };</script>')
            return HttpResult(200, path, html, {"content-type": "text/html"})

        if clean.startswith("/statistics/"):
            self.stats_calls += 1
            return HttpResult(204, path, "")

        if clean.startswith("/api/course/") and clean.endswith("/entry/record"):
            return HttpResult(200, path, "{}")

        return HttpResult(404, path, "not found")

    def close(self) -> None:
        pass


def make_app(root: Path):
    (root / "accounts.yaml").write_text(
        "defaults:\n"
        "  course_ids: [29066]\n"
        "  semester_id: \"14\"\n"
        "  academic_year_id: \"5\"\n"
        "accounts:\n"
        "  - name: acc01\n    enabled: true\n"
        "  - name: acc02\n    enabled: true\n",
        encoding="utf-8",
    )
    (root / "config.yaml").write_text(
        "rps: 1000\nglobal_rps: 1000\ntelemetry: true\ngap_seconds: [0, 0]\nchunk_seconds: 600\n",
        encoding="utf-8",
    )
    return load_config(root)


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = make_app(self.root)
        activities = fixture("activities.json")["activities"]
        reads = {int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]}
        self.transport = FakeTransport(activities, reads)
        self._orig_make_client = scheduler.make_client

        def fake_make_client(app, account, limiter, global_limiter):
            client = TronClassClient(self.transport, app.runtime, limiter=limiter)
            return client, self.transport

        scheduler.make_client = fake_make_client  # type: ignore[assignment]

    def tearDown(self):
        scheduler.make_client = self._orig_make_client  # type: ignore[assignment]
        shutdown_loggers()          # Windows 下不关文件句柄会删不掉临时目录
        self.tmp.cleanup()

    def test_full_run_marks_everything(self):
        account = self.app.account("acc01")
        cache = scheduler.CourseCache(self.root / "cache.json", 1800)
        outcome = scheduler.run_one(self.app, account, cache, None)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertGreaterEqual(outcome.done, 40)
        self.assertEqual(outcome.failed, 0)

        # 视频：所有被处理的活动都到 100（已有的历史区间可能比时长多 1 秒，用 >= 比较）
        for aid, activity in self.transport.activities.items():
            if activity.get("type") != "online_video":
                continue
            item = self.transport.state.get(aid)
            if item is None:
                continue
            duration = self.transport._duration(aid)
            covered = sum(e - s for s, e in merge_ranges(item["ranges"]))
            self.assertGreaterEqual(covered, duration, f"活动 {aid} 未刷满：{item['ranges']}")

        # 资料：所有 upload 都点过
        for aid, activity in self.transport.activities.items():
            if activity.get("type") != "material":
                continue
            expected = set(self.transport._uploads(aid))
            self.assertEqual(set(self.transport.state[aid]["uploads"]), expected, f"资料 {aid} 未点全")

        # 埋点与 state 文件
        self.assertGreater(self.transport.stats_calls, 0)
        state = json.loads((self.root / "data" / "state.acc01.json").read_text(encoding="utf-8"))
        self.assertIn("92918", state["activities"])
        self.assertEqual(state["activities"]["92918"]["status"], "done")

    def test_dry_run_sends_no_writes(self):
        account = self.app.account("acc02")
        cache = scheduler.CourseCache(self.root / "cache.json", 1800)
        outcome = scheduler.run_one(self.app, account, cache, None, dry_run=True)
        self.assertTrue(outcome.ok)
        writes = [c for c in self.transport.calls if c[0] == "POST" and "activities-read" in c[1]]
        self.assertEqual(writes, [])

    def test_error_isolation(self):
        accounts = self.app.enabled_accounts()
        bad = FakeTransport({}, {})
        bad.session_ok = False

        def fake_make_client(app, account, limiter, global_limiter):
            transport = bad if account.name == "acc02" else self.transport
            return TronClassClient(transport, app.runtime, limiter=limiter), transport

        scheduler.make_client = fake_make_client  # type: ignore[assignment]
        cache = scheduler.CourseCache(self.root / "cache.json", 1800)
        outcomes = scheduler.run_all(self.app, accounts)
        by_name = {o.account: o for o in outcomes}
        self.assertTrue(by_name["acc01"].ok)
        self.assertFalse(by_name["acc02"].ok)
        self.assertIn("会话", by_name["acc02"].error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
