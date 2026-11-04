"""把课程结构翻译成可执行任务列表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import TronClassClient
from .config import RuntimeConfig


VIDEO_TYPES = {"online_video"}
MATERIAL_TYPES = {"material"}
MANUAL_TYPES = {
    "homework", "exam", "page", "scorm", "h5_courseware", "forum", "bulletin",
    "questionnaire", "interaction", "shared_resource", "personal_resource",
    "tencent_meeting", "welink", "lark_meeting", "dingtalk_live", "chatroom",
    "classroom", "lesson", "score", "calendar_event",
}

ACTION_VIDEO = "video"
ACTION_MATERIAL = "material"
ACTION_SKIP = "skip"
ACTION_MANUAL = "manual"


@dataclass
class ActivityTask:
    course_id: int
    activity_id: int
    type: str
    title: str
    module_id: int = 0
    module_name: str = ""
    duration: int = 0
    upload_ids: list[int] = field(default_factory=list)
    upload_names: list[str] = field(default_factory=list)
    criterion_key: str = ""
    criterion_value: str = ""
    action: str = ACTION_MANUAL
    reason: str = ""
    state: str = ""          # full / part / ""
    sort: int = 0

    @property
    def label(self) -> str:
        return f"{self.activity_id} {self.title[:26]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "activity_id": self.activity_id,
            "type": self.type,
            "title": self.title,
            "module_id": self.module_id,
            "module_name": self.module_name,
            "duration": self.duration,
            "upload_ids": self.upload_ids,
            "criterion": f"{self.criterion_key}={self.criterion_value}",
            "action": self.action,
            "reason": self.reason,
            "state": self.state,
        }


@dataclass
class CoursePlan:
    course_id: int
    name: str
    tasks: list[ActivityTask]
    total_activities: int = 0
    completed_before: int = 0
    study_completeness: float = 0.0

    def by_action(self, action: str) -> list[ActivityTask]:
        return [t for t in self.tasks if t.action == action]

    def actionable(self) -> list[ActivityTask]:
        return [t for t in self.tasks if t.action in (ACTION_VIDEO, ACTION_MATERIAL)]

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.tasks),
            "video": len(self.by_action(ACTION_VIDEO)),
            "material": len(self.by_action(ACTION_MATERIAL)),
            "skip": len(self.by_action(ACTION_SKIP)),
            "manual": len(self.by_action(ACTION_MANUAL)),
        }


def _uploads(activity: dict) -> list[dict]:
    uploads = activity.get("uploads") or []
    return [u for u in uploads if isinstance(u, dict)]


def _duration_of(uploads: list[dict]) -> int:
    best = 0.0
    for upload in uploads:
        for video in upload.get("videos") or []:
            try:
                best = max(best, float(video.get("duration") or 0))
            except (TypeError, ValueError):
                continue
    return int(best)


def _upload_type(uploads: list[dict]) -> str:
    for upload in uploads:
        if upload.get("type") == "video":
            return "video"
    return uploads[0].get("type", "") if uploads else ""


def build_plan(
    client: TronClassClient,
    course_id: int,
    rt: RuntimeConfig,
    activities: list[dict] | None = None,
    reads: dict[int, dict] | None = None,
    modules: list[dict] | None = None,
    completeness: dict | None = None,
    name: str = "",
    target: int = 100,
) -> CoursePlan:
    activities = activities if activities is not None else client.activities(course_id)
    reads = reads if reads is not None else client.activity_reads_for_user(course_id)
    modules = modules if modules is not None else client.modules(course_id)
    completeness = completeness if completeness is not None else client.my_completeness(course_id)

    module_names = {int(m.get("id", 0)): str(m.get("name", "")) for m in modules if isinstance(m, dict)}

    tasks: list[ActivityTask] = []
    for activity in activities:
        try:
            aid = int(activity.get("id"))
        except (TypeError, ValueError):
            continue
        atype = str(activity.get("type") or "")
        module_id = int(activity.get("module_id") or 0)
        uploads = _uploads(activity)
        upload_ids = [int(u["id"]) for u in uploads if str(u.get("id", "")).isdigit()]
        upload_names = [str(u.get("name") or "") for u in uploads]

        task = ActivityTask(
            course_id=course_id,
            activity_id=aid,
            type=atype,
            title=str(activity.get("title") or ""),
            module_id=module_id,
            module_name=module_names.get(module_id, ""),
            duration=_duration_of(uploads),
            upload_ids=upload_ids,
            upload_names=upload_names,
            criterion_key=str(activity.get("completion_criterion_key") or ""),
            criterion_value=str(activity.get("completion_criterion_value") or ""),
            sort=int(activity.get("sort") or 0),
        )

        read = reads.get(aid) or {}
        state = str(read.get("completeness") or "")
        task.state = state
        if state == "full":
            task.action = ACTION_SKIP
            task.reason = "已完成"
            tasks.append(task)
            continue

        # 目标低于 100% 时，已经达到目标的也算跳过 —— 和执行器里 VideoJob 的判定保持一致，
        # 否则卡片说「还能刷 N 个」、实际跑起来一个都不动（review-97cb993 #7）
        if target < 100:
            current = 100.0 if state == "full" else float(
                (read.get("data") or {}).get("completeness") or 0
            )
            if current >= target:
                task.action = ACTION_SKIP
                task.reason = f"已达目标 {target}%"
                tasks.append(task)
                continue

        if activity.get("published") is False:
            task.action = ACTION_SKIP
            task.reason = "未发布"
            tasks.append(task)
            continue

        if atype in VIDEO_TYPES:
            if task.duration <= 0:
                try:
                    detail = client.activity_detail(aid, cid=course_id)
                    detail_uploads = _uploads(detail)
                    task.duration = _duration_of(detail_uploads)
                    if not task.upload_ids:
                        task.upload_ids = [int(u["id"]) for u in detail_uploads if str(u.get("id", "")).isdigit()]
                except Exception:
                    task.duration = 0
            if task.duration > 0:
                task.action = ACTION_VIDEO
            else:
                task.action = ACTION_MANUAL
                task.reason = "拿不到视频时长"
        elif atype in MATERIAL_TYPES:
            if task.upload_ids:
                task.action = ACTION_MATERIAL
            else:
                task.action = ACTION_SKIP
                task.reason = "无附件"
        else:
            task.action = ACTION_MANUAL
            task.reason = "需人工（作业/考试等）"

        tasks.append(task)

    completed = (completeness or {}).get("completed_result") or {}
    return CoursePlan(
        course_id=course_id,
        name=name or f"课程 {course_id}",
        tasks=tasks,
        total_activities=int(completed.get("total_activities") or len(activities)),
        completed_before=int(completed.get("total_completed") or 0),
        study_completeness=float((completeness or {}).get("study_completeness") or 0.0),
    )
