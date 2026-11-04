"""单账号执行器：视频分段上报、资料逐附件已读、断点续跑、自动降档。

一个账号一个 AccountRunner，内部**同一时刻只有一个请求在飞**（用交错轮转而不是并发写），
这样既不会因为 session cookie 每响应轮换而互相顶号，又能把段间隔的空等摊薄掉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .client import SessionExpired, TronClassClient, TronClassError
from .config import AccountConfig, AppConfig, RuntimeConfig
from .planner import (
    ACTION_MANUAL,
    ACTION_MATERIAL,
    ACTION_SKIP,
    ACTION_VIDEO,
    ActivityTask,
    CoursePlan,
    build_plan,
)
from .progress import coverage_percent, covered_seconds, merge_ranges, plan_chunks
from .telemetry import Telemetry
from .transport import TransportError
from .util import TokenBucket, human_seconds, jitter_sleep, read_json, atomic_write_json, setup_logger

log = setup_logger("runner")

SPEED_PROFILES: dict[str, dict[str, Any]] = {
    "probe": {"chunk": None, "gap": (0.3, 0.8)},
    "normal": {"chunk": 120, "gap": (1.5, 3.0)},
    "human": {"chunk": 30, "gap": (5.0, 15.0)},
}

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_MANUAL = "manual"
STATUS_DRYRUN = "dry-run"


@dataclass
class TaskResult:
    course_id: int
    activity_id: int
    type: str
    title: str
    action: str
    status: str
    completeness: int | None = None
    detail: str = ""
    chunks: int = 0
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "activity_id": self.activity_id,
            "type": self.type,
            "title": self.title,
            "action": self.action,
            "status": self.status,
            "completeness": self.completeness,
            "detail": self.detail,
            "chunks": self.chunks,
            "elapsed": round(self.elapsed, 1),
        }


class StateStore:
    """断点续跑状态：data/state.<account>.json"""

    def __init__(self, path: Path, account: str) -> None:
        self.path = path
        self.account = account
        raw = read_json(path, default={}) or {}
        self.data: dict[str, Any] = {
            "account": account,
            "updated_at": None,
            "activities": raw.get("activities", {}),
            "courses": raw.get("courses", {}),
        }

    def activity(self, aid: int) -> dict:
        return self.data["activities"].get(str(aid), {})

    def update_activity(self, aid: int, **fields: Any) -> None:
        item = self.data["activities"].setdefault(str(aid), {})
        item.update(fields)
        item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save()

    def update_course(self, cid: int, **fields: Any) -> None:
        item = self.data["courses"].setdefault(str(cid), {})
        item.update(fields)
        item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        atomic_write_json(self.path, self.data)


def _video_percent(resp: dict) -> int | None:
    """从 activities-read 响应里取视频百分比。"""
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        value = data.get("completeness")
        if value is not None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    return None


def _read_state(read: dict | None) -> tuple[str, int, list]:
    if not read:
        return "", 0, []
    data = read.get("data") or {}
    state = str(read.get("completeness") or "")
    percent = 0
    try:
        percent = int(float(data.get("completeness") or 0))
    except (TypeError, ValueError):
        percent = 0
    ranges = data.get("ranges") or []
    return state, percent, ranges


class VideoJob:
    """一个视频活动的分段上报状态机，`step()` 每次只发一段。"""

    def __init__(self, runner: "AccountRunner", task: ActivityTask, chunk: int | None,
                 target: int) -> None:
        self.runner = runner
        self.task = task
        self.chunk = chunk
        self.target = target
        self.total = int(task.duration)
        self.percent = 0
        self.ranges: list = []
        self.pieces: list[tuple[int, int]] = []
        self.rounds = 0
        self.sent = 0
        self.stalled = 0
        self.done = False
        self.failed = False
        self.detail = ""
        self.started = time.time()

    # -- 初始化：读服务端已有进度，算出待补区间
    def prepare(self) -> None:
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, ranges = _read_state(read)
        self.percent = percent
        self.ranges = ranges
        if state == "full" or percent >= self.target:
            self.done = True
            return
        self.runner.telemetry.video_view(
            self.task.course_id, self.task.module_id, self.task.activity_id,
            self.task.upload_ids[0] if self.task.upload_ids else None,
        )
        self._replan()

    def _replan(self) -> None:
        self.pieces = plan_chunks(self.ranges, self.total, self.chunk)
        if not self.pieces and self.percent < self.target:
            # 极端情况：服务端算出的已看区间对不上，直接整段补一次
            self.pieces = [(0, int(self.total))]

    def step(self) -> str:
        """返回 done / failed / progress / idle。"""
        if self.done:
            return "done"
        if self.failed:
            return "failed"
        if not self.pieces:
            self._refresh()
            if self.percent >= self.target:
                self.done = True
                return "done"
            self.stalled += 1
            if self.stalled > 2:
                self.failed = True
                self.detail = self.detail or f"停在 {self.percent}%"
                return "failed"
            self._degrade()
            return "progress"

        start, end = self.pieces.pop(0)
        before_percent = self.percent
        before_covered = covered_seconds(self.ranges)
        self.runner.request_count += 1
        try:
            resp = self.runner.client.report_video(self.task.activity_id, start, end, self.task.course_id)
        except SessionExpired:
            raise
        except (TronClassError, TransportError) as exc:
            self.stalled += 1
            self.detail = f"{start}-{end}s 上报异常：{exc}"
            if self.stalled > 3:
                self.failed = True
                return "failed"
            self._degrade()
            return "progress"

        self.sent += 1
        new_percent = _video_percent(resp)
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        has_ranges = isinstance(data.get("ranges"), list) and bool(data["ranges"])
        if has_ranges:
            self.ranges = merge_ranges(data["ranges"])
        self._update_percent(new_percent)

        progressed = (
            self.percent > before_percent
            or covered_seconds(self.ranges) > before_covered
            or (not has_ranges and new_percent is None)  # 服务端没回任何进度信息，按接受处理
        )
        if progressed:
            self.stalled = 0
        else:
            self.stalled += 1
            self.detail = f"{start}-{end}s 上报未被计入（仍 {self.percent}%）"
            if self.stalled >= 2:
                self._degrade()
            if self.stalled > 6:
                self.failed = True
                return "failed"

        self.runner.telemetry.video(
            self.task.course_id, self.task.module_id, self.task.activity_id,
            self.task.upload_ids[0] if self.task.upload_ids else None, start, end,
        )
        if self.percent >= self.target:
            self.done = True
            return "done"
        return "progress"

    def _update_percent(self, new_percent: int | None) -> None:
        before = self.percent
        if new_percent is not None and new_percent >= self.percent:
            self.percent = new_percent
        elif new_percent is None:
            # 服务端没回百分比：用本地区间估算
            self.percent = max(self.percent, int(coverage_percent(self.ranges, self.total)))
        if self.percent > before:
            self.stalled = 0

    def _refresh(self) -> None:
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, ranges = _read_state(read)
        if state == "full":
            self.percent = max(self.percent, 100)
        else:
            self.percent = max(self.percent, percent)
        if ranges:
            self.ranges = merge_ranges(ranges)

    def _degrade(self) -> None:
        """上不动就降档：段长减半、并把剩余区间重算一遍。"""
        current = self.chunk if self.chunk else 60
        self.chunk = max(15, int(current // 2))
        self.runner.log.warning(
            "  %s 降档：chunk->%ss（当前 %s%%）", self.task.label, self.chunk, self.percent
        )
        self._refresh()
        self._replan()

    def result(self) -> TaskResult:
        if self.done:
            self._refresh()
            status = STATUS_DONE
        elif self.failed:
            status = STATUS_FAILED
        else:
            status = STATUS_FAILED
        return TaskResult(
            course_id=self.task.course_id,
            activity_id=self.task.activity_id,
            type=self.task.type,
            title=self.task.title,
            action=ACTION_VIDEO,
            status=status,
            completeness=self.percent,
            detail=self.detail or ("已刷满" if status == STATUS_DONE else ""),
            chunks=self.sent,
            elapsed=time.time() - self.started,
        )


class MaterialJob:
    """一个资料活动：逐个 upload 上报已读。"""

    def __init__(self, runner: "AccountRunner", task: ActivityTask, target: int) -> None:
        self.runner = runner
        self.task = task
        self.target = target
        self.percent = 0
        self.pending: list[int] = []
        self.sent = 0
        self.done = False
        self.failed = False
        self.detail = ""
        self.started = time.time()

    def prepare(self) -> None:
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, _ = _read_state(read)
        data = (read or {}).get("data") or {}
        already = {int(x) for x in (data.get("upload_ids") or []) if str(x).isdigit()}
        self.percent = percent if state != "full" else 100
        if state == "full" or self.percent >= self.target:
            self.done = True
            return
        self.pending = [uid for uid in self.task.upload_ids if uid not in already]

    def step(self) -> str:
        if self.done:
            return "done"
        if self.failed:
            return "failed"
        if not self.pending:
            self._refresh()
            if self.percent >= self.target:
                self.done = True
                return "done"
            self.failed = True
            self.detail = f"停在 {self.percent}%（附件都点过了但服务端没给满）"
            return "failed"

        upload_id = self.pending.pop(0)
        self.runner.request_count += 1
        try:
            resp = self.runner.client.report_upload(self.task.activity_id, upload_id, self.task.course_id)
        except SessionExpired:
            raise
        except (TronClassError, TransportError) as exc:
            self.failed = True
            self.detail = f"upload {upload_id} 上报失败：{exc}"
            return "failed"

        self.sent += 1
        percent = _video_percent(resp)
        if percent is not None:
            self.percent = max(self.percent, percent)
        state = str((resp or {}).get("completeness") or "")
        if state == "full":
            self.percent = 100
        self.runner.telemetry.material(self.task.course_id, self.task.module_id,
                                       self.task.activity_id, upload_id)
        if self.percent >= self.target:
            self.done = True
            return "done"
        return "progress"

    def _refresh(self) -> None:
        read = self.runner.client.activity_read(self.task.activity_id, self.task.course_id)
        state, percent, _ = _read_state(read)
        self.percent = 100 if state == "full" else max(self.percent, percent)

    def result(self) -> TaskResult:
        status = STATUS_DONE if self.done else STATUS_FAILED
        return TaskResult(
            course_id=self.task.course_id,
            activity_id=self.task.activity_id,
            type=self.task.type,
            title=self.task.title,
            action=ACTION_MATERIAL,
            status=status,
            completeness=self.percent,
            detail=self.detail or ("已全部已读" if self.done else ""),
            chunks=self.sent,
            elapsed=time.time() - self.started,
        )


class AccountRunner:
    def __init__(
        self,
        app: AppConfig,
        account: AccountConfig,
        client: TronClassClient,
        client_factory: Callable[[], TronClassClient] | None = None,
        limiter: TokenBucket | None = None,
        state: StateStore | None = None,
        on_result: Callable[[Any], None] | None = None,
        bundle_provider: Callable[[int], dict] | None = None,
    ) -> None:
        self.app = app
        self.rt: RuntimeConfig = app.runtime
        self.account = account
        self.client = client
        self.client_factory = client_factory
        self.limiter = limiter
        self.bundle_provider = bundle_provider
        self.log = app.logger(f"run.{account.name}", account)
        self.state = state or StateStore(app.state_path(account), account.name)
        self.on_result = on_result
        self.request_count = 0
        self.telemetry = Telemetry(client, self.rt, self.log, enabled=bool(self.rt.telemetry))

    # ------------------------------------------------------------------ 账号信息
    def describe(self) -> dict:
        try:
            courses = self.client.my_courses(self.account.semester_id, self.account.academic_year_id)
            return {"courses": [{"id": c.get("id"), "name": c.get("display_name") or c.get("name")}
                                for c in courses]}
        except Exception as exc:
            return {"error": str(exc)}

    def _rebuild_client(self) -> bool:
        if self.client_factory is None:
            return False
        self.log.warning("会话失效，尝试重新取会话……")
        try:
            self.client.transport.close()
        except Exception:
            pass
        try:
            self.client = self.client_factory()
            self.telemetry.client = self.client
            return True
        except Exception as exc:
            self.log.error("重建会话失败：%s", exc)
            return False

    # ------------------------------------------------------------------ 主流程
    def run(
        self,
        course_ids: Iterable[int] | None = None,
        only: set[int] | None = None,
        dry_run: bool = False,
    ) -> dict[int, list[TaskResult]]:
        ids = list(course_ids or self.account.course_ids)
        results: dict[int, list[TaskResult]] = {}
        for cid in ids:
            results[cid] = self.run_course(cid, only=only, dry_run=dry_run)
        return results

    def run_course(self, cid: int, only: set[int] | None = None, dry_run: bool = False) -> list[TaskResult]:
        speed_name = self.account.effective_speed(self.rt)
        speed = SPEED_PROFILES.get(speed_name, SPEED_PROFILES["normal"])
        if speed["chunk"] is None:            # probe：整段一次
            chunk = None
        elif speed_name == "normal":          # normal：允许用配置里的 chunk_seconds 覆盖
            chunk = int(self.rt.chunk_seconds or speed["chunk"])
        else:                                 # human：固定小段，配置不参与
            chunk = int(speed["chunk"])
        gap = tuple(self.rt.gap) if self.rt.gap_explicit else tuple(speed["gap"])
        target = self.account.effective_target(self.rt)

        bundle: dict = {}
        if self.bundle_provider is not None:
            try:
                bundle = self.bundle_provider(cid) or {}
            except Exception as exc:  # 缓存不可用就退回各自拉取
                self.log.debug("课程结构缓存不可用：%s", exc)
        plan = build_plan(
            self.client,
            cid,
            self.rt,
            activities=bundle.get("activities"),
            modules=bundle.get("modules"),
            name=str(bundle.get("name") or ""),
        )
        counts = plan.summary()
        self.log.info(
            "课程 %s：共 %d 个活动（视频 %d / 资料 %d / 跳过 %d / 人工 %d），当前完整度 %.1f%%",
            plan.name, counts["total"], counts["video"], counts["material"],
            counts["skip"], counts["manual"], plan.study_completeness,
        )

        tasks: list[ActivityTask] = []
        for task in plan.tasks:
            if only and task.activity_id not in only:
                continue
            if task.action in (ACTION_SKIP, ACTION_MANUAL):
                continue
            tasks.append(task)

        if not tasks:
            self.log.info("课程 %s 没有需要处理的活动", plan.name)
            return []
        if dry_run:
            self.log.info("dry-run：将处理 %d 个活动（视频 %d / 资料 %d）", len(tasks),
                          sum(1 for t in tasks if t.action == ACTION_VIDEO),
                          sum(1 for t in tasks if t.action == ACTION_MATERIAL))
            return [
                TaskResult(t.course_id, t.activity_id, t.type, t.title, t.action, STATUS_DRYRUN,
                           completeness=None, detail=f"时长 {t.duration}s" if t.duration else "")
                for t in tasks
            ]

        started = time.time()
        results: list[TaskResult] = []
        results.extend(self._run_videos([t for t in tasks if t.action == ACTION_VIDEO], chunk, gap, target))
        results.extend(self._run_materials([t for t in tasks if t.action == ACTION_MATERIAL], target))

        done = sum(1 for r in results if r.status == STATUS_DONE)
        self.log.info(
            "课程 %s 处理结束：成功 %d / 失败 %d，用时 %s，请求 %d 次",
            plan.name, done, len(results) - done, human_seconds(time.time() - started), self.request_count,
        )
        self.state.update_course(cid, last_run=time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 results=[r.to_dict() for r in results])
        return results

    # ------------------------------------------------------------------ 视频
    def _run_videos(self, tasks: list[ActivityTask], chunk: int | None,
                    gap: tuple[float, float], target: int) -> list[TaskResult]:
        if not tasks:
            return []
        parallelism = max(1, int(self.account.per_account_jobs or self.rt.per_account_jobs))
        results: list[TaskResult] = []
        jobs: list[VideoJob] = []
        for task in tasks:
            job = VideoJob(self, task, chunk, target)
            try:
                self._guard(job.prepare)
            except SessionExpired as exc:
                self.log.error("会话失效，停止本账号：%s", exc)
                raise
            if job.done:
                self.log.info("  ○ %s 已是目标进度，跳过", task.label)
                continue
            jobs.append(job)
            self.log.info("  → %s 时长 %s，待补 %d 段", task.label,
                          human_seconds(job.total), len(job.pieces))

        step_gap = (gap[0] / parallelism, gap[1] / parallelism) if parallelism > 1 else gap
        while jobs:
            for job in list(jobs):
                try:
                    outcome = self._guard(job.step)
                except SessionExpired as exc:
                    self.log.error("会话失效，停止本账号：%s", exc)
                    raise
                if outcome == "done":
                    res = job.result()
                    results.append(res)
                    jobs.remove(job)
                    self._record(res)
                elif outcome == "failed":
                    res = job.result()
                    results.append(res)
                    jobs.remove(job)
                    self._record(res)
                jitter_sleep(step_gap)
        return results

    # ------------------------------------------------------------------ 资料
    def _run_materials(self, tasks: list[ActivityTask], target: int) -> list[TaskResult]:
        results: list[TaskResult] = []
        for task in tasks:
            job = MaterialJob(self, task, target)
            try:
                self._guard(job.prepare)
                while True:
                    outcome = self._guard(job.step)
                    if outcome in ("done", "failed"):
                        break
                    jitter_sleep((self.rt.gap_min / 2, self.rt.gap_max / 2))
            except SessionExpired as exc:
                self.log.error("会话失效，停止本账号：%s", exc)
                raise
            res = job.result()
            results.append(res)
            self._record(res)
        return results

    # ------------------------------------------------------------------ 工具
    def _guard(self, func: Callable[[], Any]) -> Any:
        """包一层会话失效重试：失效 -> 重建客户端 -> 再试一次。"""
        try:
            return func()
        except SessionExpired:
            if not self._rebuild_client():
                raise
            return func()

    def _record(self, res: TaskResult) -> None:
        self.state.update_activity(
            res.activity_id,
            course_id=res.course_id,
            type=res.type,
            status=res.status,
            completeness=res.completeness,
            detail=res.detail,
            chunks=res.chunks,
        )
        flag = "✓" if res.status == STATUS_DONE else "✗"
        self.log.info(
            "  %s %s %s → %s（%s%s）",
            flag, res.activity_id, res.title[:20], res.status,
            f"{res.completeness}%" if res.completeness is not None else "-",
            f"，{res.detail}" if res.detail else "",
        )
        if self.on_result is not None:
            try:
                self.on_result(res)
            except Exception:  # pragma: no cover - UI 回调不应影响主流程
                pass
