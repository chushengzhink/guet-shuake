"""多账号编排：账号级线程池 + 全局限速 + 课程结构缓存 + 错误隔离。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .browser import BrowserUnavailable, configure_slots
from .client import TronClassClient
from .config import AccountConfig, AppConfig
from .runner import (
    STATUS_DONE,
    STATUS_FAILED,
    AccountRunner,
    StateStore,
    TaskResult,
)
from .session import LoginRequired, SessionError, open_transport
from .transport import Transport, TransportError
from .util import LockBusy, TokenBucket, atomic_write_json, human_seconds, read_json, run_lock, setup_logger

log = setup_logger("scheduler")


class CourseCache:
    """课程结构（活动/模块）跨账号共享，避免 N 个账号各拉一遍 248 KB。"""

    def __init__(self, path: Path, ttl: int = 1800) -> None:
        self.path = path
        self.ttl = max(60, int(ttl))
        self._locks_guard = threading.Lock()
        self._course_locks: dict[str, threading.Lock] = {}
        self._save_lock = threading.Lock()
        self._data: dict[str, Any] = read_json(path, default={}) or {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._course_locks.setdefault(key, threading.Lock())

    def bundle(self, cid: int, client: TronClassClient) -> dict:
        # 按课程加锁：不同课程可以并行加载，同一课程只拉一次
        with self._lock_for(str(cid)):
            entry = self._data.get(str(cid)) or {}
            if entry.get("activities") and time.time() - float(entry.get("ts") or 0) < self.ttl:
                return entry
            meta: dict = {}
            try:
                meta = client.course_meta(cid)
            except Exception as exc:  # 拿不到元信息不影响主流程
                log.debug("course_meta(%s) 失败：%s", cid, exc)
            data = {
                "ts": time.time(),
                "name": meta.get("name") or f"课程 {cid}",
                "course_code": meta.get("course_code") or "",
                "activities": client.activities(cid),
                "modules": client.modules(cid),
            }
            self._data[str(cid)] = data
            with self._save_lock:
                try:
                    atomic_write_json(self.path, self._data)
                except Exception:  # pragma: no cover
                    pass
            return data

    def invalidate(self, cid: int | None = None) -> None:
        keys = [str(cid)] if cid is not None else list(self._data)
        with self._locks_guard:
            locks = [self._course_locks.setdefault(key, threading.Lock()) for key in keys]
        for lock in locks:
            lock.acquire()
        try:
            if cid is None:
                self._data.clear()
            else:
                self._data.pop(str(cid), None)
            with self._save_lock:
                try:
                    atomic_write_json(self.path, self._data)
                except Exception:  # pragma: no cover
                    pass
        finally:
            for lock in reversed(locks):
                lock.release()


@dataclass
class AccountOutcome:
    account: str
    ok: bool
    error: str = ""
    results: dict[int, list[TaskResult]] = field(default_factory=dict)
    before: dict[int, float] = field(default_factory=dict)
    after: dict[int, float] = field(default_factory=dict)
    requests: int = 0
    elapsed: float = 0.0

    @property
    def done(self) -> int:
        return sum(1 for items in self.results.values() for r in items if r.status == STATUS_DONE)

    @property
    def failed(self) -> int:
        return sum(1 for items in self.results.values() for r in items if r.status == STATUS_FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "ok": self.ok,
            "error": self.error,
            "requests": self.requests,
            "elapsed": round(self.elapsed, 1),
            "done": self.done,
            "failed": self.failed,
            "completeness": {
                str(cid): {"before": self.before.get(cid), "after": self.after.get(cid)}
                for cid in sorted(set(self.before) | set(self.after))
            },
            "courses": {
                str(cid): [r.to_dict() for r in items] for cid, items in self.results.items()
            },
        }


def make_client(app: AppConfig, account: AccountConfig, limiter: TokenBucket,
                global_limiter: TokenBucket | None) -> tuple[TronClassClient, Transport]:
    transport = open_transport(app, account, limiter)
    if global_limiter is not None:
        transport.limiter = limiter
    client = TronClassClient(transport, app.runtime, limiter=limiter, global_limiter=global_limiter)
    client.account_name = account.name
    return client, transport


def _completeness(client: TronClassClient, course_ids: Sequence[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    for cid in course_ids:
        try:
            data = client.my_completeness(cid)
            out[cid] = float(data.get("study_completeness") or 0.0)
        except Exception:
            continue
    return out


def run_one(
    app: AppConfig,
    account: AccountConfig,
    cache: CourseCache,
    global_limiter: TokenBucket | None,
    only: set[int] | None = None,
    dry_run: bool = False,
    on_result=None,
) -> AccountOutcome:
    """带账号级运行锁的入口：同一账号同时只允许一个进程在刷。"""
    lock_path = app.state_path(account).with_suffix(".run.lock")
    try:
        with run_lock(lock_path):
            return _run_one_locked(app, account, cache, global_limiter, only, dry_run, on_result)
    except LockBusy as exc:
        log.error("账号 %s 已在另一个进程里运行：%s", account.name, exc)
        return AccountOutcome(account=account.name, ok=False,
                              error=f"另一个进程正在刷这个账号（{exc}）")


def _run_one_locked(
    app: AppConfig,
    account: AccountConfig,
    cache: CourseCache,
    global_limiter: TokenBucket | None,
    only: set[int] | None = None,
    dry_run: bool = False,
    on_result=None,
) -> AccountOutcome:
    started = time.time()
    outcome = AccountOutcome(account=account.name, ok=False)
    limiter = TokenBucket(app.runtime.rps, burst=max(1.0, app.runtime.rps))
    logger = app.logger(f"acc.{account.name}", account)
    transports: list[Transport] = []
    holder: dict[str, TronClassClient] = {}

    def factory() -> TronClassClient:
        new_client, new_transport = make_client(app, account, limiter, global_limiter)
        transports.append(new_transport)
        holder["client"] = new_client
        return new_client

    def bundle_provider(cid: int) -> dict:
        return cache.bundle(cid, holder["client"])

    try:
        client, transport = make_client(app, account, limiter, global_limiter)
        transports.append(transport)
        holder["client"] = client
        if not client.check_session():
            raise LoginRequired(
                f"{account.name} 会话不可用，先执行：python run.py login --account {account.name}"
            )

        runner = AccountRunner(
            app, account, client,
            client_factory=factory,
            limiter=limiter,
            state=StateStore(app.state_path(account), account.name),
            bundle_provider=bundle_provider,
            on_result=on_result,
        )
        course_ids = list(account.course_ids) or _discover_courses(client, account)
        if not course_ids:
            raise SessionError(
                f"{account.name} 名下没有找到课程，检查 accounts.yaml 的 semester_id/course_ids"
            )

        outcome.before = _completeness(holder["client"], course_ids)
        # 逐课程累计：后面某门课抛异常时，前面已完成的结果也不会丢
        for cid in course_ids:
            try:
                outcome.results[cid] = runner.run_course(cid, only=only, dry_run=dry_run)
            except Exception as exc:
                logger.error("课程 %s 执行失败：%s", cid, exc)
                outcome.error = f"{type(exc).__name__}: {exc}"
                break
        outcome.after = _completeness(holder["client"], course_ids)
        outcome.requests = runner.request_count
        outcome.ok = all(
            r.status != STATUS_FAILED for items in outcome.results.values() for r in items
        )
        logger.info("结束：%s，成功 %d 失败 %d，%d 次请求",
                    "全部成功" if outcome.ok else "有失败项", outcome.done, outcome.failed,
                    outcome.requests)
        return outcome
    except (LoginRequired, SessionError) as exc:
        outcome.error = str(exc)
        logger.error("账号 %s 中止：%s", account.name, exc)
        return outcome
    except (TransportError, BrowserUnavailable) as exc:
        outcome.error = str(exc)
        logger.error("账号 %s 网络/浏览器错误：%s", account.name, exc)
        return outcome
    except Exception as exc:  # 单账号异常不影响其它账号
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.exception("账号 %s 出错：%s", account.name, exc)
        return outcome
    finally:
        outcome.elapsed = time.time() - started
        for item in transports:
            try:
                item.close()
            except Exception:
                pass


def _discover_courses(client: TronClassClient, account: AccountConfig) -> list[int]:
    courses = client.my_courses(account.semester_id, account.academic_year_id)
    ids = []
    for course in courses:
        try:
            ids.append(int(course.get("id")))
        except (TypeError, ValueError):
            continue
    return ids


def run_all(
    app: AppConfig,
    accounts: Sequence[AccountConfig],
    only: set[int] | None = None,
    dry_run: bool = False,
    on_result=None,
) -> list[AccountOutcome]:
    configure_slots(app.runtime.max_browser_contexts)
    cache = CourseCache(app.course_cache_path(), app.runtime.course_cache_ttl)
    global_limiter = TokenBucket(app.runtime.global_rps, burst=max(2.0, app.runtime.global_rps))
    workers = max(1, min(len(accounts), int(app.runtime.account_jobs)))
    log.info("开始执行：%d 个账号，并发 %d，引擎 %s，节奏 %s",
             len(accounts), workers, app.runtime.engine, app.runtime.speed)

    outcomes: list[AccountOutcome] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acc") as pool:
        futures = {}
        for acc in accounts:
            # runner 只会回调 (result)，这里按账号补上名字，避免 GUI 的回调签名对不上
            callback = (
                (lambda result, _name=acc.name: on_result(_name, result))
                if on_result is not None else None
            )
            futures[pool.submit(
                run_one, app, acc, cache, global_limiter, only, dry_run, callback
            )] = acc
        for future in as_completed(futures):
            account = futures[future]
            try:
                outcomes.append(future.result())
            except Exception as exc:  # pragma: no cover - 兜底
                outcomes.append(AccountOutcome(account=account.name, ok=False, error=str(exc)))
    order = {acc.name: idx for idx, acc in enumerate(accounts)}
    outcomes.sort(key=lambda o: order.get(o.account, 999))
    return outcomes


def collect_status(app: AppConfig, accounts: Sequence[AccountConfig]) -> dict[str, dict[str, Any]]:
    """只读体检：每个账号的会话是否可用 + 课程完整度。"""
    result: dict[str, dict[str, Any]] = {}
    for account in accounts:
        limiter = TokenBucket(app.runtime.rps)
        transport: Transport | None = None
        try:
            client, transport = make_client(app, account, limiter, None)
            ok = client.check_session()
            info: dict[str, Any] = {"session": ok, "courses": []}
            if ok:
                course_ids = list(account.course_ids) or _discover_courses(client, account)
                for cid in course_ids:
                    try:
                        data = client.my_completeness(cid)
                        completed = (data.get("completed_result") or {})
                        info["courses"].append({
                            "id": cid,
                            "total": completed.get("total_activities"),
                            "completed": completed.get("total_completed"),
                            "percent": float(data.get("study_completeness") or 0.0),
                        })
                    except Exception as exc:
                        info["courses"].append({"id": cid, "error": str(exc)})
            result[account.name] = info
        except Exception as exc:
            result[account.name] = {"session": False, "error": str(exc), "courses": []}
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
    return result
