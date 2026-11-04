"""Chromium 生命周期管理：持久化 profile、上下文并发槽、profile 独占锁。

注意：Playwright 的 sync API 不可跨线程共享，
所以每个 worker 线程自己 `start_playwright()` / `launch_context()` / 自己关。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

from .config import RuntimeConfig
from .util import LockBusy, run_lock, setup_logger

log = setup_logger("browser")

_sem_lock = threading.Lock()
_sem = threading.BoundedSemaphore(2)


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


def _browsers_root() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home())
    if os.name == "nt":
        return Path(local) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(local) / "ms-playwright"


def resolve_chromium(pw: Playwright) -> tuple[str | None, str]:
    """返回 (要显式指定的 executable_path 或 None, 描述)。

    Playwright 1.6x 在 headless 模式默认去找 `chromium_headless_shell`，
    而 `executable_path` 属性给的是完整 chromium —— 两者不是一回事，
    所以这里**总是**返回完整 chromium 路径并显式传下去，
    避免「明明装了完整浏览器，它却去要没装的 headless shell」。

    另外，Playwright 与已装浏览器版本对不上时（pip 里是 1.51 要 chromium-1161，
    本机只有 chromium-1234），也退到本机已有的那份，省一次 150 MB 下载。
    """
    expected = ""
    try:
        expected = pw.chromium.executable_path or ""
    except Exception:  # pragma: no cover
        expected = ""
    if expected and Path(expected).exists():
        return expected, expected

    root = _browsers_root()
    patterns = [
        "chromium-*/chrome-win64/chrome.exe",   # 新版目录名
        "chromium-*/chrome-win/chrome.exe",     # 旧版目录名
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(sorted(root.glob(pattern)))
    if candidates:
        chosen = candidates[-1]
        log.info("Playwright 期望的 Chromium 不存在（%s），改用本机已有的 %s", expected or "-", chosen)
        return str(chosen), str(chosen)
    return None, expected or str(root)


def start_playwright() -> Playwright:
    try:
        return sync_playwright().start()
    except Exception as exc:  # pragma: no cover - 环境相关
        raise BrowserUnavailable(f"Playwright 启动失败：{exc}") from exc


# ------------------------------------------------------------------ 浏览器通道
# 优先用系统自带的 Edge：Windows 10/11 都有，不用额外下载 100+MB 的 Chromium
CHANNEL_ORDER: dict[str, tuple[str, ...]] = {
    "auto": ("msedge", "chrome", "chromium"),
    "msedge": ("msedge", "chrome", "chromium"),
    "edge": ("msedge", "chrome", "chromium"),
    "chrome": ("chrome", "msedge", "chromium"),
    "chromium": ("chromium",),
}


def find_edge_exe() -> str | None:
    """返回系统 Edge 的可执行文件路径（GUI 也要用它开 --app 窗口）。"""
    candidates: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def _is_missing_browser(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "is not found" in text
        or "executable doesn't exist" in text
        or "playwright install" in text
        or "distribution" in text
    )


def _launch_candidates(pw: Playwright, rt: RuntimeConfig) -> list[dict]:
    """按配置给出依次尝试的启动参数：系统 Edge → Chrome → 内置 Chromium。"""
    name = (getattr(rt, "browser_channel", "auto") or "auto").strip().lower()
    order = CHANNEL_ORDER.get(name, CHANNEL_ORDER["auto"])
    candidates: list[dict] = []
    for item in order:
        if item == "chromium":
            executable, _ = resolve_chromium(pw)
            candidates.append({"executable_path": executable} if executable else {})
        else:
            candidates.append({"channel": item})
    return candidates


def _friendly_launch_error(exc: Exception, profile_dir: Path) -> str:
    text = str(exc)
    if "ProcessSingleton" in text or "already in use" in text or "user data directory" in text:
        return f"profile 目录被占用（{profile_dir}）：先关掉用同一账号开着的浏览器或另一个本工具进程"
    return f"启动浏览器失败：{exc}"


_last_launch: dict[str, str] = {"label": "", "executable": ""}


def last_launch() -> dict[str, str]:
    """最近一次实际用到的浏览器（GUI 自检/排障用）。"""
    return dict(_last_launch)


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

    last_error: Exception | None = None
    for candidate in _launch_candidates(pw, rt):
        label = candidate.get("channel") or "内置 Chromium"
        try:
            ctx = pw.chromium.launch_persistent_context(**{**kwargs, **candidate})
        except Exception as exc:
            if _is_missing_browser(exc):
                log.info("浏览器通道 %s 不可用，换下一个", label)
                last_error = exc
                continue
            raise BrowserUnavailable(_friendly_launch_error(exc, profile_dir)) from exc
        ctx.set_default_timeout(int(rt.timeout * 1000))
        _last_launch.update({
            "label": label,
            "executable": candidate.get("channel") or candidate.get("executable_path", "") or "bundled",
        })
        log.info("浏览器：%s", label)
        return ctx

    raise BrowserUnavailable(
        "Edge / Chrome / 内置 Chromium 都启动失败；"
        f"装个 Edge（Windows 自带）或执行 python -m playwright install chromium。最后错误：{last_error}"
    )
    ctx.set_default_timeout(int(rt.timeout * 1000))
    return ctx


@contextmanager
def profile_lock(profile_dir: Path, wait: float = 45.0) -> Iterator[None]:
    """同一 profile 只允许一个浏览器实例。

    GUI 里「刷新课程」「登录」「开始刷课」都会开浏览器，并发时会撞锁 ——
    这里排队等待（最多 45 秒），而不是直接把任务判失败。
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        with run_lock(profile_dir / ".lock", wait=wait):
            yield
    except LockBusy as exc:
        raise BrowserUnavailable(
            f"这个账号的浏览器还被另一个操作占着（等了 {wait:.0f} 秒）：{exc}"
        ) from exc
