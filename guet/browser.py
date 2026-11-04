"""Chromium 生命周期管理：持久化 profile、上下文并发槽、profile 独占锁。

注意：Playwright 的 sync API 不可跨线程共享，
所以每个 worker 线程自己 `start_playwright()` / `launch_context()` / 自己关。
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

from .config import RuntimeConfig
from .util import setup_logger

log = setup_logger("browser")

_sem_lock = threading.Lock()
_sem = threading.BoundedSemaphore(2)
LOCK_TTL_SECONDS = 6 * 3600


class BrowserUnavailable(RuntimeError):
    """Playwright / Chromium 不可用。"""


def configure_slots(count: int) -> None:
    """设置同时存在的浏览器上下文上限（跨账号）。"""
    global _sem
    with _sem_lock:
        _sem = threading.BoundedSemaphore(max(1, int(count)))


@contextmanager
def browser_slot() -> Iterator[None]:
    """占用一个浏览器上下文名额；名额满时阻塞等待。"""
    with _sem_lock:
        sem = _sem
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def start_playwright() -> Playwright:
    try:
        return sync_playwright().start()
    except Exception as exc:  # pragma: no cover - 环境相关
        raise BrowserUnavailable(f"Playwright 启动失败：{exc}") from exc


def launch_context(pw: Playwright, profile_dir: Path, rt: RuntimeConfig, headless: bool) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
    ]
    kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=args,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=rt.user_agent,
        accept_downloads=False,
        ignore_https_errors=False,
    )
    if rt.proxy:
        kwargs["proxy"] = {"server": rt.proxy}
    try:
        ctx = pw.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:  # pragma: no cover - 环境相关
        text = str(exc)
        if "Executable doesn't exist" in text or "playwright install" in text:
            raise BrowserUnavailable(
                "没有找到 Chromium，先执行：python -m playwright install chromium"
            ) from exc
        if "ProcessSingleton" in text or "already in use" in text or "user data directory" in text:
            raise BrowserUnavailable(
                f"profile 目录被占用（{profile_dir}）：先关掉用同一账号开着的浏览器或另一个本工具进程"
            ) from exc
        raise BrowserUnavailable(f"启动 Chromium 失败：{exc}") from exc
    ctx.set_default_timeout(int(rt.timeout * 1000))
    return ctx


@contextmanager
def profile_lock(profile_dir: Path) -> Iterator[None]:
    """同一 profile 只允许一个进程使用（Chromium 自己也会锁，这里给个更清楚的报错）。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_path = profile_dir / ".lock"
    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0
        pid_text = ""
        try:
            pid_text = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        pid = int(pid_text) if pid_text.isdigit() else 0
        if age < LOCK_TTL_SECONDS and pid and _pid_alive(pid):
            raise BrowserUnavailable(
                f"profile 正在被进程 {pid} 使用（{lock_path}）；确认没有别的实例在跑后删掉该文件再试"
            )
        log.warning("清理残留 profile 锁：%s", lock_path)
        try:
            lock_path.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError as exc:  # pragma: no cover - 竞态
        raise BrowserUnavailable(f"无法获取 profile 锁：{lock_path}") from exc
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
