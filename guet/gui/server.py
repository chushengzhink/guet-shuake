"""GUI 后端：127.0.0.1 上的小型 HTTP 服务 + JSON API。

界面是一个单文件 HTML（`web/index.html`），交给系统 Edge 以 `--app` 模式打开，
所以 UI 由真正的浏览器渲染，Python 这边只负责 JSON。
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import cancel, report, scheduler, session
from ..benchmark import measure, plan_scale, recommend
from ..browser import BrowserUnavailable, find_edge_exe
from ..client import SessionExpired, TronClassError
from ..config import SPEEDS, AppConfig, ConfigError, _check_lanes, load_config
from ..planner import ACTION_MATERIAL, ACTION_VIDEO, MANUAL_TYPES, build_plan
from ..runner import resolve_lanes
from ..session import LoginRequired, SessionError
from ..transport import TransportError
from ..util import TokenBucket, setup_logger
from ..wizard import describe_plan

log = setup_logger("gui")

CONFIG_TEMPLATE = """\
# GUET 畅课助手 · 配置文件（GUI 会自动维护，也可手改）
base_url: https://courses.guet.edu.cn
engine: http
browser_channel: auto      # auto = 优先系统 Edge，其次 Chrome，最后内置 Chromium
speed: normal
chunk_seconds: 120
target_percent: 100
telemetry: true
realtime_lanes: auto
account_jobs: 1
per_account_jobs: 1
rps: 3.0
global_rps: 8.0
"""

ACCOUNTS_TEMPLATE = """\
defaults:
  course_ids: []
  speed: normal
  target_percent: 100
  engine: http
  realtime_lanes: auto

accounts: []
"""


def web_index() -> Path:
    """打包后前端在 _MEIPASS/guet/gui/web 下。"""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "guet" / "gui" / "web" / "index.html"
    return Path(__file__).resolve().parent / "web" / "index.html"


class _LogHandler(logging.Handler):
    def __init__(self, state: "AppState") -> None:
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover
            message = str(record.msg)
        self.state.push_log(record.levelname, record.name, message)


class AppState:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config_path = root / "config.yaml"
        self.accounts_path = root / "accounts.yaml"
        self.token = secrets.token_urlsafe(18)
        self.lock = threading.Lock()
        self.logs: deque[dict[str, Any]] = deque(maxlen=4000)
        self.log_seq = 0
        self.status = "idle"          # idle / logging-in / running / stopping
        self.error = ""
        self.events: list[dict[str, Any]] = []
        self.progress: dict[str, dict[str, int]] = {}
        self.planned: dict[str, int] = {}
        self.run_id = 0
        self.last_outcomes: list[dict[str, Any]] = []
        self.shutdown = threading.Event()
        self.run_thread: threading.Thread | None = None
        self.clients: dict[str, dict] = {}          # 账号 -> 复用的 client（少起几次浏览器）
        self.account_locks: dict[str, threading.Lock] = {}
        self.app: AppConfig | None = None
        self.server: ThreadingHTTPServer | None = None

    # ---- 基础 ----
    def ensure_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        if not self.accounts_path.exists():
            self.accounts_path.write_text(ACCOUNTS_TEMPLATE, encoding="utf-8")

    def push_log(self, level: str, name: str, message: str) -> None:
        with self.lock:
            self.log_seq += 1
            self.logs.append({
                "id": self.log_seq,
                "t": time.strftime("%H:%M:%S"),
                "level": level,
                "src": name,
                "msg": message,
            })

    def reload(self) -> AppConfig | None:
        try:
            self.app = load_config(self.root, self.config_path, self.accounts_path)
            self.error = ""
        except ConfigError as exc:
            self.app = None
            self.error = str(exc)
        return self.app

    # ---- 账号 ----
    def _validate_accounts_file(self, path: Path) -> None:
        """用真实加载流程校验候选 accounts 文件；不通过就抛 ConfigError。"""
        load_config(self.root, self.config_path, path)

    def _write_accounts(self, raw: dict) -> None:
        """先写临时文件并验证，通过后再原子替换 —— 非法账号绝不落盘。"""
        import os

        import yaml

        text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        tmp = self.accounts_path.with_name(self.accounts_path.name + ".pending")
        tmp.write_text(text, encoding="utf-8")
        try:
            self._validate_accounts_file(tmp)
        except ConfigError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp, self.accounts_path)
        self.reload()

    def add_account(self, name: str) -> None:
        import yaml

        name = (name or "").strip()
        if not name:
            raise ConfigError("账号标识不能为空")
        with self.lock:                       # 并发添加时别互相覆盖
            raw = yaml.safe_load(self.accounts_path.read_text(encoding="utf-8")) or {}
            accounts = raw.get("accounts") or []
            if any(str(item.get("name")) == name for item in accounts if isinstance(item, dict)):
                raise ConfigError(f"账号 {name} 已存在")
            accounts.append({"name": name, "enabled": True})
            raw["accounts"] = accounts
            self._write_accounts(raw)

    def delete_account(self, name: str) -> None:
        import yaml

        with self.lock:
            raw = yaml.safe_load(self.accounts_path.read_text(encoding="utf-8")) or {}
            raw["accounts"] = [a for a in (raw.get("accounts") or [])
                               if not (isinstance(a, dict) and str(a.get("name")) == name)]
            self._write_accounts(raw)

    # ---- 运行状态（F05：状态转换必须在锁内，且用 run_id 防覆盖）----
    def begin(self, target: str) -> int:
        with self.lock:
            if self.status != "idle":
                raise ConfigError(f"当前状态是「{self.status}」，请等它结束再操作")
            self.status = target
            self.run_id += 1
            return self.run_id

    def finish(self, run_id: int) -> None:
        with self.lock:
            if self.run_id == run_id:      # 只结束自己那次运行，不覆盖新的
                self.status = "idle"

    def account_lock(self, name: str) -> threading.Lock:
        """同一账号的浏览器操作串行化：刷新课程 / 登录 / 开始不要互相抢 profile。"""
        with self.lock:
            return self.account_locks.setdefault(name, threading.Lock())

    def drop_client(self, name: str) -> None:
        entry = self.clients.pop(name, None)
        if entry:
            try:
                entry["transport"].close()
            except Exception:  # pragma: no cover
                pass

    def mark_stopping(self) -> str:
        with self.lock:
            if self.status == "running":
                self.status = "stopping"
            return self.status

    # ---- 快照 ----
    def status_payload(self) -> dict[str, Any]:
        app = self.app
        accounts = []
        if app is not None:
            for account in app.accounts:
                profile = app.profile_path(account)
                accounts.append({
                    "name": account.name,
                    "enabled": account.enabled,
                    "course_ids": account.course_ids,
                    "logged_in": (profile / "Default").exists() or (profile / "Local State").exists(),
                })
        return {
            "status": self.status,
            "error": self.error,
            "configured": app is not None,
            "accounts": accounts,
            "browser": find_edge_exe() or "内置 Chromium",
            "progress": self.progress,
            "planned": self.planned,
            "events": self.events[-80:],
        }


# ------------------------------------------------------------------ API 实现
def api_status(state: AppState) -> dict:
    return state.status_payload()


def api_add_account(state: AppState, body: dict) -> dict:
    state.add_account(str(body.get("name") or ""))
    return state.status_payload()


def api_delete_account(state: AppState, body: dict) -> dict:
    state.delete_account(str(body.get("name") or ""))
    return state.status_payload()


def api_login(state: AppState, body: dict) -> dict:
    app = state.app
    if app is None:
        raise ConfigError(state.error or "配置未就绪")
    account = app.account(str(body.get("name") or ""))
    # F05：只有空闲状态才能登录，且用 run_id 保证只结束自己这次
    run_id = state.begin("logging-in")
    try:
        with state.account_lock(account.name):
            state.drop_client(account.name)          # 登录会换会话，旧 client 作废
            session.login(app, account, headless=False)
        return {"ok": True, "account": account.name}
    finally:
        state.finish(run_id)


class _NoopClose:
    """缓存命中时返回它，调用方的 finally 里 close() 不会真的关掉会话。"""

    def close(self) -> None:
        pass


CLIENT_TTL = 300.0


def _client_for(state: AppState, name: str):
    app = state.app
    if app is None:
        raise ConfigError(state.error or "配置未就绪")
    account = app.account(name)

    with state.account_lock(name):
        entry = state.clients.get(name)
        if entry and time.time() - entry["ts"] < CLIENT_TTL:
            client = entry["client"]
            try:
                if client.check_session():
                    return account, client, _NoopClose()      # 复用，不再起浏览器
            except Exception:
                pass
            state.drop_client(name)

        limiter = TokenBucket(app.runtime.rps, burst=max(1.0, app.runtime.rps))
        client, transport = scheduler.make_client(app, account, limiter, None)
        state.clients[name] = {"ts": time.time(), "client": client, "transport": transport}
        return account, client, transport


def _course_cache(state: AppState):
    cache = getattr(state, "_course_cache", None)
    if cache is None:
        cache = scheduler.CourseCache(state.root / "course-cache.json", 1800)
        state._course_cache = cache
    return cache


def _course_breakdown(cache, client, cid: int) -> dict:
    """统计一门课里「还能自动刷」和「只能人工」的数量（课程卡上直接展示）。"""
    activities = (cache.bundle(cid, client) or {}).get("activities") or []
    reads = client.activity_reads_for_user(cid)
    todo_video = todo_material = 0
    manual = 0
    for activity in activities:
        atype = str(activity.get("type") or "")
        try:
            aid = int(activity.get("id"))
        except (TypeError, ValueError):
            continue
        finished = (reads.get(aid) or {}).get("completeness") == "full"
        if atype == "online_video":
            todo_video += 0 if finished else 1
        elif atype == "material":
            todo_material += 0 if finished else 1
        elif atype in MANUAL_TYPES:
            manual += 0 if finished else 1      # 只算还没做的作业/考试
    return {
        "todo_video": todo_video,
        "todo_material": todo_material,
        "manual": manual,
        "auto": todo_video + todo_material,
    }


def api_courses(state: AppState, body: dict) -> dict:
    account, client, transport = _client_for(state, str(body.get("name") or ""))
    try:
        if not client.check_session():
            raise LoginRequired(f"{account.name} 还没登录，先点「登录」")
        rows = []
        for course in client.my_courses(account.semester_id, account.academic_year_id):
            try:
                cid = int(course.get("id"))
            except (TypeError, ValueError):
                continue
            completed = total = 0
            percent = float(course.get("course_avg_completeness") or 0.0)
            try:
                data = client.my_completeness(cid)
                result = data.get("completed_result") or {}
                completed = int(result.get("total_completed") or 0)
                total = int(result.get("total_activities") or 0)
                percent = float(data.get("study_completeness") or percent)
            except Exception:
                pass
            rows.append({
                "id": cid,
                "name": str(course.get("display_name") or course.get("name") or cid),
                "code": str(course.get("course_code") or ""),
                "completed": completed,
                "total": total,
                "percent": round(percent, 1),
                "todo_video": None,      # None = 还没统计
                "todo_material": None,
                "manual": None,
                "auto": None,
            })

        # 并发补构成统计：activities 走缓存，第二次刷新几乎是瞬间
        cache = _course_cache(state)
        if rows:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="course") as pool:
                futures = {pool.submit(_course_breakdown, cache, client, row["id"]): row
                           for row in rows}
                for future, row in futures.items():
                    try:
                        row.update(future.result(timeout=60))
                    except Exception as exc:  # 单门课统计失败不影响列表
                        log.debug("统计课程 %s 构成失败：%s", row["id"], exc)
                        row.update({"todo_video": 0, "todo_material": 0, "manual": 0, "auto": 0})

        # 平台口径里还有「其它类型」的未完成活动（考试常不在 activities 列表里）
        for row in rows:
            remain = max(0, int(row.get("total") or 0) - int(row.get("completed") or 0))
            known = int(row.get("auto") or 0) + int(row.get("manual") or 0)
            row["other"] = max(0, remain - known)
        return {"ok": True, "courses": rows, "account": account.name}
    finally:
        transport.close()


def api_plan(state: AppState, body: dict) -> dict:
    app = state.app
    if app is None:
        raise ConfigError(state.error or "配置未就绪")
    account = app.account(str(body.get("name") or ""))
    course_ids = [int(c) for c in (body.get("courses") or [])]
    if not course_ids:
        raise ConfigError("先选课程")
    _, client, transport = _client_for(state, account.name)
    plans = []
    try:
        if not client.check_session():
            raise LoginRequired(f"{account.name} 还没登录，先点「登录」")
        for cid in course_ids:
            plan = build_plan(client, cid, app.runtime)
            info = describe_plan(plan, app.runtime, account)
            info["estimate"] = info.pop("eta")
            plans.append(info)
    finally:
        transport.close()
    return {"ok": True, "plans": plans}


def api_bench(state: AppState, body: dict) -> dict:
    app = state.app
    if app is None:
        raise ConfigError(state.error or "配置未就绪")
    account = app.account(str(body.get("name") or ""))
    course_ids = [int(c) for c in (body.get("courses") or [])]
    _, client, transport = _client_for(state, account.name)
    try:
        if not client.check_session():
            raise LoginRequired(f"{account.name} 还没登录，先点「登录」")
        videos = []
        for cid in course_ids:
            plan = build_plan(client, cid, app.runtime)
            videos.extend(t for t in plan.tasks if t.action == ACTION_VIDEO)
        count, total, longest = plan_scale(videos)
        bench = measure(client, rounds=5)
        advice = recommend(
            bench, count, total,
            max_lanes=int(app.runtime.realtime_max_lanes),
            eta_seconds=float(app.runtime.realtime_eta_seconds or 0),
            longest_seconds=longest,
        )
        return {
            "ok": True,
            "lanes": advice.lanes,
            "presets": advice.presets,
            "render": advice.render(),
        }
    finally:
        transport.close()


def api_start(state: AppState, body: dict) -> dict:
    app = state.app
    if app is None:
        raise ConfigError(state.error or "配置未就绪")

    names = [str(n) for n in (body.get("accounts") or [])]
    if not names:
        raise ConfigError("先选账号")
    try:
        course_ids = [int(c) for c in (body.get("courses") or [])]
    except (TypeError, ValueError) as exc:
        raise ConfigError("课程 ID 必须是整数") from exc
    if not course_ids or any(cid <= 0 for cid in course_ids):
        raise ConfigError("先选课程")
    speed = str(body.get("speed") or "normal")
    if speed not in SPEEDS:
        raise ConfigError(f"节奏只能是 {SPEEDS}，收到 {speed!r}")
    try:
        target = int(body.get("target") or 100)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"目标百分比必须是整数，收到 {body.get('target')!r}") from exc
    if not 1 <= target <= 100:
        raise ConfigError("目标百分比必须在 1~100")
    lanes = body.get("lanes")
    if lanes in (None, ""):
        lanes = None
    elif str(lanes).strip().lower() in {"auto", "all", "max"}:
        lanes = str(lanes).strip().lower()
    else:
        try:
            lanes = int(str(lanes).strip())      # GUI 输入框给的是字符串（R04）
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"并行路数只接受数字、auto 或 all，收到 {lanes!r}") from exc
        _check_lanes(lanes, "并行路数")

    # R11：支持按账号给计划总数 {name: n}，也兼容单个数字
    raw_planned = body.get("planned")
    planned: dict[str, int] = {}
    try:
        if isinstance(raw_planned, dict):
            planned = {str(k): int(v) for k, v in raw_planned.items() if v is not None}
        elif raw_planned not in (None, ""):
            planned = {name: int(raw_planned) for name in names}
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"planned 必须是数字或 {{账号: 数字}}，收到 {raw_planned!r}") from exc

    accounts = []
    for name in names:
        account = app.account(name)
        if not account.enabled:                       # F04：禁用账号不得执行
            raise ConfigError(f"账号 {name} 已禁用")
        accounts.append(account)

    # F05：只有 idle 才能启动；run_id 保证旧 worker 不会把新运行置为 idle
    run_id = state.begin("running")
    for account in accounts:
        account.course_ids = list(course_ids)
        account.speed = speed
        account.target_percent = target
        if lanes not in (None, ""):
            account.realtime_lanes = lanes
    app.runtime.speed = speed
    app.runtime.target_percent = target

    cancel.reset()
    with state.lock:
        state.progress = {name: {"done": 0, "failed": 0, "pending": 0} for name in names}
        state.planned = {name: planned.get(name, 0) for name in names}
        state.events = []
        state.last_outcomes = []

    def on_result(account_name: str, result) -> None:
        with state.lock:
            slot = state.progress.setdefault(account_name, {"done": 0, "failed": 0, "pending": 0})
            if result.status == "done":
                slot["done"] += 1
            elif result.status == "failed":
                slot["failed"] += 1
            else:
                slot["pending"] += 1
            state.events.append({
                "account": account_name,
                "id": result.activity_id,
                "title": result.title,
                "status": result.status,
                "percent": result.completeness,
                "detail": result.detail,
            })

    def worker() -> None:
        try:
            outcomes = scheduler.run_all(app, accounts, on_result=on_result)
            # F10：保存最终结果（GUI 之前把返回值整个丢掉了）
            with state.lock:
                state.last_outcomes = [o.to_dict() for o in outcomes]
            failed = [o for o in outcomes if not o.ok]
            if failed:
                with state.lock:
                    state.error = "；".join(f"{o.account}: {o.error or '有失败项'}" for o in failed)
            try:
                report.write_result(state.root / "result.json", outcomes)
            except Exception:  # pragma: no cover - 报告失败不影响任务
                log.debug("写 result.json 失败", exc_info=True)
        except Exception:  # pragma: no cover
            log.error("执行异常：\n%s", traceback.format_exc())
        finally:
            state.finish(run_id)

    thread = threading.Thread(target=worker, name="gui-run", daemon=True)
    state.run_thread = thread
    thread.start()
    return {"ok": True}


def api_selftest(state: AppState) -> dict:
    """环境自检：真的拉起一次浏览器，确认打包后的 Playwright / 内核可用。"""
    import tempfile

    from ..browser import last_launch, launch_context, start_playwright
    from ..config import RuntimeConfig

    rt = state.app.runtime if state.app else RuntimeConfig()
    pw = start_playwright()
    try:
        with tempfile.TemporaryDirectory() as raw:
            ctx = launch_context(pw, Path(raw) / "profile", rt, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("about:blank", timeout=20000)
                user_agent = page.evaluate("() => navigator.userAgent")
            finally:
                ctx.close()
        info = last_launch()
        return {
            "ok": True,
            "user_agent": user_agent,
            "browser": info.get("label", ""),
            "channel": rt.browser_channel,
            "bundle": hasattr(sys, "_MEIPASS"),
        }
    finally:
        try:
            pw.stop()
        except Exception:  # pragma: no cover
            pass


def api_stop(state: AppState) -> dict:
    status = state.mark_stopping()
    if status == "stopping":
        cancel.request()
        log.warning("收到停止请求……")
    return {"ok": True, "status": status}


def api_logs(state: AppState, after: int) -> dict:
    with state.lock:
        lines = [item for item in state.logs if item["id"] > after]
        return {"next": state.log_seq, "lines": lines}


def api_quit(state: AppState) -> dict:
    cancel.request()
    state.shutdown.set()          # 通知启动器退出（F12）
    server = state.server

    def shutdown() -> None:
        time.sleep(0.2)
        if server is not None:
            server.shutdown()

    threading.Thread(target=shutdown, daemon=True).start()
    return {"ok": True}


# ------------------------------------------------------------------ HTTP 层
class Handler(BaseHTTPRequestHandler):
    state: AppState

    server_version = "GuetShuakeGUI/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 静音访问日志
        return

    # ---- 工具 ----
    def _send(self, code: int, payload: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": message}, code)

    def _authorized(self, query: dict) -> bool:
        token = (query.get("t") or [""])[0] or self.headers.get("X-Token", "")
        return bool(token) and secrets.compare_digest(token, self.state.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._fail("token 无效", 403)
            return
        if parsed.path in ("/", "/index.html"):
            try:
                html = web_index().read_bytes()
            except OSError as exc:
                self._fail(f"前端文件缺失：{exc}", 500)
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            self._json(api_status(self.state))
            return
        if parsed.path == "/api/logs":
            try:
                after = int((query.get("after") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                self._fail("after 必须是整数", 400)
                return
            self._json(api_logs(self.state, after))
            return
        self._fail("not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._fail("token 无效", 403)
            return
        body = self._body()
        routes = {
            "/api/accounts": lambda: api_add_account(self.state, body),
            "/api/accounts/delete": lambda: api_delete_account(self.state, body),
            "/api/login": lambda: api_login(self.state, body),
            "/api/courses": lambda: api_courses(self.state, body),
            "/api/plan": lambda: api_plan(self.state, body),
            "/api/bench": lambda: api_bench(self.state, body),
            "/api/start": lambda: api_start(self.state, body),
            "/api/stop": lambda: api_stop(self.state),
            "/api/selftest": lambda: api_selftest(self.state),
            "/api/quit": lambda: api_quit(self.state),
        }
        handler = routes.get(parsed.path)
        if handler is None:
            self._fail("not found", 404)
            return
        try:
            self._json(handler())
        except ConfigError as exc:
            self._fail(str(exc), 400)
        except LoginRequired as exc:
            self._fail(str(exc), 401)
        except (TransportError, TronClassError, SessionExpired) as exc:
            self._fail(f"接口调用失败：{exc}", 502)
        except (SessionError, BrowserUnavailable) as exc:
            self._fail(str(exc), 500)
        except Exception as exc:  # pragma: no cover
            log.error("处理 %s 出错：\n%s", parsed.path, traceback.format_exc())
            self._fail(f"{type(exc).__name__}: {exc}", 500)


def make_handler(state: AppState):
    return type("BoundHandler", (Handler,), {"state": state})


def build_server(state: AppState, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    state.server = server
    return server
