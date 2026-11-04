"""统计埋点镜像：把真实播放行为补到 /statistics/api/*（可关）。

TronClass 前端通过 navigator.sendBeacon 往 statistics 服务上报
`POST /statistics/api/online-videos?jwt=<mgsJwt>`（jwt 由页面服务端渲染注入），
教师端的“学习时长/行为轨迹”就吃这份数据。刷课只改进度不补埋点会显得很干，所以这里同步一份。
"""

from __future__ import annotations

import time
from typing import Any

from .client import TronClassClient
from .config import RuntimeConfig


class Telemetry:
    def __init__(self, client: TronClassClient, rt: RuntimeConfig, logger, enabled: bool = True) -> None:
        self.client = client
        self.rt = rt
        self.logger = logger
        self.enabled = enabled
        self._cache: dict[int, tuple[float, dict]] = {}
        self.sent = 0
        self.failed = 0

    def _settings(self, course_id: int) -> dict:
        now = time.time()
        cached = self._cache.get(course_id)
        if cached and now - cached[0] < 600:
            return cached[1]
        try:
            settings = self.client.page_settings(course_id)
        except Exception as exc:
            self.logger.debug("取 mgsJwt 失败（埋点跳过）：%s", exc)
            settings = {}
        self._cache[course_id] = (now, settings)
        return settings

    def _payload(self, course_id: int, module_id: int, activity_id: int,
                 upload_id: int | None, action: str, meeting_type: str,
                 settings: dict, extra: dict[str, Any] | None = None) -> dict:
        payload: dict[str, Any] = {
            "user_id": int(settings.get("userId") or 0),
            "org_id": int(settings.get("orgId") or 0),
            "course_id": course_id,
            "module_id": module_id,
            "activity_id": activity_id,
            "upload_id": upload_id,
            "reply_id": None,
            "comment_id": None,
            "forum_type": "",
            "action_type": action,
            "is_teacher": False,
            "is_student": True,
            "ts": int(time.time() * 1000),
            "user_agent": self.rt.user_agent,
            "meeting_type": meeting_type,
        }
        if settings.get("departmentId"):
            payload["dep_id"] = settings["departmentId"]
        if extra:
            payload.update(extra)
        return payload

    def _send_video(self, action: str, course_id: int, module_id: int, activity_id: int,
                    upload_id: int | None, start: int, end: int) -> None:
        if not self.enabled:
            return
        settings = self._settings(course_id)
        if not settings.get("mgsJwt"):
            self.enabled = False
            self.logger.info("页面里没找到 mgsJwt，关闭埋点镜像")
            return
        extra = None
        if action == "play":
            extra = {"start_at": int(start), "end_at": int(end),
                     "duration": max(1, int(end) - int(start))}
        payload = self._payload(course_id, module_id, activity_id, upload_id, action,
                                "online_video", settings, extra)
        try:
            res = self.client.stats_online_video(settings["mgsJwt"], payload, course_id)
            if res.ok:
                self.sent += 1
            else:
                self.failed += 1
                self.logger.debug("埋点 online-videos(%s) 返回 %s", action, res.status)
        except Exception as exc:  # 埋点失败不影响主流程
            self.failed += 1
            self.logger.debug("埋点 online-videos(%s) 异常：%s", action, exc)

    def video_view(self, course_id: int, module_id: int, activity_id: int,
                   upload_id: int | None) -> None:
        """进入活动时的一次 view（抓包里每个活动只有一条）。"""
        self._send_video("view", course_id, module_id, activity_id, upload_id, 0, 0)

    def video(self, course_id: int, module_id: int, activity_id: int,
              upload_id: int | None, start: int, end: int) -> None:
        """每段播放的 play 事件（抓包里就是按段上报的）。"""
        self._send_video("play", course_id, module_id, activity_id, upload_id, start, end)

    def material(self, course_id: int, module_id: int, activity_id: int, upload_id: int) -> None:
        if not self.enabled:
            return
        settings = self._settings(course_id)
        if not settings.get("mgsJwt"):
            self.enabled = False
            return
        payload = self._payload(course_id, module_id, activity_id, upload_id, "view", "material", settings)
        try:
            res = self.client.stats_material(settings["mgsJwt"], payload, course_id)
            if res.ok:
                self.sent += 1
            else:
                self.failed += 1
        except Exception as exc:  # pragma: no cover
            self.failed += 1
            self.logger.debug("埋点 materials 异常：%s", exc)
