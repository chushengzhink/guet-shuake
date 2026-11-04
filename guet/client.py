"""TronClass（畅课）接口客户端 —— 全部端点由 HAR 实测反推。

端点对照见 README「接口清单」，这里只做薄封装 + 统一的会话失效判定与重试。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping

from .config import RuntimeConfig
from .transport import HttpResult, Transport, TransportError, looks_logged_out
from .util import TokenBucket, setup_logger

log = setup_logger("client")

MY_COURSES_FIELDS = (
    "id,name,course_code,display_name,course_attributes(teaching_class_name,graduate_method),"
    "department(id,name),instructors(id,name),start_date,end_date,is_started,is_closed,"
    "academic_year_id,semester_id,credit,compulsory"
)

STATS_JWT_RE = re.compile(r'mgsJwt:\s*"([^"]+)"')
STATS_UID_RE = re.compile(r'userId:\s*"([^"]*)"')
STATS_ORG_RE = re.compile(r'orgId:\s*"([^"]*)"')
STATS_DEP_RE = re.compile(r'departmentId:\s*"([^"]*)"')
MAX_SPAN_RE = re.compile(r"maximum is (\d+)")

# 服务端硬限制（实测 400 报文）：单次上报跨度最多 125 秒
# {"message":"Log failed, caused： The set time duration（538s） is too long, maximum is 125s"}
SERVER_MAX_SPAN_DEFAULT = 125


def parse_activity_read(read: dict | None) -> tuple[str, int, list]:
    """把 activity-read 的响应解析成 (state, percent, ranges)。"""
    if not read:
        return "", 0, []
    data = read.get("data") or {}
    state = str(read.get("completeness") or "")
    try:
        percent = int(float(data.get("completeness") or 0))
    except (TypeError, ValueError):
        percent = 0
    ranges = data.get("ranges") or []
    return state, percent, ranges


def parse_max_span(message: str) -> int | None:
    """从 400 报文里解析服务端允许的单段上限（秒）。"""
    match = MAX_SPAN_RE.search(message or "")
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 3600 else None


class TronClassError(RuntimeError):
    """接口返回了非预期结果。"""


class SessionExpired(TronClassError):
    """会话失效，需要重新登录。"""


class TronClassClient:
    def __init__(
        self,
        transport: Transport,
        rt: RuntimeConfig,
        limiter: TokenBucket | None = None,
        global_limiter: TokenBucket | None = None,
    ) -> None:
        self.transport = transport
        self.rt = rt
        self.limiter = limiter
        self.global_limiter = global_limiter
        self.request_count = 0
        self.account_name = ""

    # ------------------------------------------------------------------ 底层
    def _referer(self, cid: int | None, view: str = "learning-activity") -> str | None:
        if not cid:
            return None
        return f"{self.rt.base_url}/course/{cid}/{view}"

    def _call(self, method: str, path: str, *, referer_cid: int | None = None, **kw) -> HttpResult:
        attempts = max(1, int(self.rt.retries))
        referer = kw.pop("referer", None) or self._referer(referer_cid)
        last_error: Exception | None = None
        for attempt in range(attempts):
            if self.global_limiter is not None:
                self.global_limiter.take()
            try:
                res = self.transport.request(method, path, referer=referer, **kw)
            except TransportError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            self.request_count += 1
            if looks_logged_out(res):
                raise SessionExpired(f"{method} {path} 会话失效：{res.brief(120)}")
            if res.status in (429, 502, 503, 504) and attempt + 1 < attempts:
                log.warning("%s %s -> %s，%.1fs 后重试", method, path, res.status, 2.0 * (attempt + 1))
                time.sleep(2.0 * (attempt + 1))
                continue
            return res
        raise TransportError(str(last_error) if last_error else f"{method} {path} 失败")

    def _json(self, method: str, path: str, *, allow_missing: bool = False, **kw) -> Any:
        res = self._call(method, path, **kw)
        if allow_missing and res.status in (404, 204):
            return None
        if not res.ok:
            raise TronClassError(f"{method} {path} -> {res.status} {res.text[:180]}")
        data = res.data
        if data is None and res.text:
            raise TronClassError(f"{method} {path} 返回的不是 JSON：{res.text[:180]}")
        return data

    # 会话探测端点：/api/config 在缺 Referer 时会被 WAF 拦 403（实测），所以按顺序多试几个
    SESSION_PROBES = (
        "/api/my-semesters",
        "/api/todos?no-intercept=true",
        "/api/config?no-intercept=true",
    )

    # ------------------------------------------------------------------ 会话
    def check_session(self) -> bool:
        for path in self.SESSION_PROBES:
            try:
                res = self._call("GET", path, referer=f"{self.rt.base_url}/user/courses")
            except (TronClassError, TransportError):
                continue
            if res.ok and isinstance(res.data, dict):
                return True
        return False

    # ------------------------------------------------------------------ 课程
    def my_semesters(self) -> list[dict]:
        data = self._json("GET", "/api/my-semesters")
        return (data or {}).get("semesters") or []

    def my_academic_years(self) -> list[dict]:
        data = self._json("GET", "/api/my-academic-years?fields=id,name,sort,is_active")
        return (data or {}).get("academic_years") or []

    def resolve_term(self, semester_id: str = "", academic_year_id: str = "") -> tuple[str, str]:
        """没配学期时自动取当前活跃学期。"""
        if semester_id and academic_year_id:
            return semester_id, academic_year_id
        semesters = self.my_semesters()
        active = next((s for s in semesters if s.get("is_active")), semesters[0] if semesters else None)
        if active is None:
            return semester_id, academic_year_id
        return (
            semester_id or str(active.get("id")),
            academic_year_id or str(active.get("academic_year_id")),
        )

    def my_courses(self, semester_id: str = "", academic_year_id: str = "", page_size: int = 50) -> list[dict]:
        sem, ay = self.resolve_term(semester_id, academic_year_id)
        body = {
            "fields": MY_COURSES_FIELDS,
            "page": 1,
            "page_size": page_size,
            "conditions": {
                "semester_id": [str(sem)],
                "academic_year_id": [str(ay)],
                "keyword": "",
                "classify_type": "recently_started",
                "display_studio_list": False,
            },
            "showScorePassedStatus": False,
        }
        data = self._json("POST", "/api/my-courses", json_body=body)
        return (data or {}).get("courses") or []

    def course_meta(self, cid: int) -> dict:
        fields = (
            "id,name,start_date,end_date,course_code,subject_code,cover,credit,"
            "course_attributes(student_count),department(id,name)"
        )
        data = self._json("GET", f"/api/courses/{cid}?fields={fields}", referer_cid=cid)
        return data or {}

    def modules(self, cid: int) -> list[dict]:
        data = self._json("GET", f"/api/courses/{cid}/modules", referer_cid=cid)
        return (data or {}).get("modules") or []

    def activities(self, cid: int, sub_course_id: int | None = None) -> list[dict]:
        sub = self.rt.sub_course_id if sub_course_id is None else sub_course_id
        data = self._json("GET", f"/api/courses/{cid}/activities?sub_course_id={sub}", referer_cid=cid)
        return (data or {}).get("activities") or []

    def activity_detail(self, aid: int, sub_course_id: int | None = None, cid: int | None = None) -> dict:
        sub = self.rt.sub_course_id if sub_course_id is None else sub_course_id
        data = self._json("GET", f"/api/activities/{aid}?sub_course_id={sub}", referer_cid=cid)
        return data or {}

    # ------------------------------------------------------------------ 进度
    def my_completeness(self, cid: int) -> dict:
        return self._json("GET", f"/api/course/{cid}/my-completeness", referer_cid=cid) or {}

    def activity_reads_for_user(self, cid: int) -> dict[int, dict]:
        data = self._json("GET", f"/api/course/{cid}/activity-reads-for-user", referer_cid=cid) or {}
        reads: dict[int, dict] = {}
        for item in data.get("activity_reads") or []:
            try:
                reads[int(item.get("activity_id"))] = item
            except (TypeError, ValueError):
                continue
        return reads

    def activity_read(self, aid: int, cid: int | None = None) -> dict | None:
        data = self._json("GET", f"/api/course/activity-read/{aid}", referer_cid=cid, allow_missing=True)
        if not data:
            return None
        return data.get("activity_read") or data

    def report_video(self, aid: int, start: int, end: int, cid: int | None = None) -> dict:
        return self._json(
            "POST",
            f"/api/course/activities-read/{aid}",
            json_body={"start": int(start), "end": int(end)},
            referer_cid=cid,
        ) or {}

    def report_upload(self, aid: int, upload_id: int, cid: int | None = None) -> dict:
        return self._json(
            "POST",
            f"/api/course/activities-read/{aid}",
            json_body={"upload_id": int(upload_id)},
            referer_cid=cid,
        ) or {}

    def entry_record(self, cid: int) -> None:
        try:
            self._call("POST", f"/api/course/{cid}/entry/record", json_body={}, referer_cid=cid)
        except Exception:  # 纯装饰性调用，失败无所谓
            pass

    # ------------------------------------------------------------------ 统计埋点
    def page_settings(self, cid: int, view: str = "learning-activity") -> dict:
        res = self._call("GET", f"/course/{cid}/{view}", referer_cid=cid)
        if not res.ok:
            return {}
        html = res.text or ""
        jwt = STATS_JWT_RE.search(html)
        if not jwt:
            return {}
        def _pick(regex: re.Pattern) -> str:
            m = regex.search(html)
            return m.group(1) if m else ""
        return {"mgsJwt": jwt.group(1), "userId": _pick(STATS_UID_RE),
                "orgId": _pick(STATS_ORG_RE), "departmentId": _pick(STATS_DEP_RE)}

    def _stats(self, kind: str, jwt: str, payload: Mapping[str, Any], cid: int) -> HttpResult:
        return self._call(
            "POST",
            f"/statistics/api/{kind}?jwt={jwt}",
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="text/plain;charset=UTF-8",
            headers={"Accept": "*/*"},
            referer_cid=cid,
        )

    def stats_online_video(self, jwt: str, payload: Mapping[str, Any], cid: int) -> HttpResult:
        return self._stats("online-videos", jwt, payload, cid)

    def stats_material(self, jwt: str, payload: Mapping[str, Any], cid: int) -> HttpResult:
        return self._stats("materials", jwt, payload, cid)
