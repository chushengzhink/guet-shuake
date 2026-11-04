"""会话：浏览器登录、从 profile 取 cookie、按引擎打开传输层。

设计要点
--------
* 登录永远交给真实 Chromium 走 CAS + Keycloak，不自己实现密码加密。
* run 阶段默认 headless：**后台起浏览器进程**，但所有业务请求都直接打接口，不模拟点击。
* engine=http 时浏览器只在开头用一次（验证会话 + 刷新 cookie），随后全程 requests，多账号更省资源。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .browser import (
    BrowserUnavailable,
    browser_slot,
    launch_context,
    profile_lock,
    start_playwright,
)
from .config import AccountConfig, AppConfig
from .transport import BrowserTransport, HttpTransport, Transport
from .util import TokenBucket, setup_logger

log = setup_logger("session")

CONFIG_PROBE = "/api/config?no-intercept=true"


class SessionError(RuntimeError):
    """会话相关错误。"""


class LoginRequired(SessionError):
    """profile 里没有可用登录态，需要先跑 `run.py login`。"""


def _context_cookies(context, base_url: str) -> dict[str, str]:
    host = base_url.split("://", 1)[-1].split("/")[0]
    cookies: dict[str, str] = {}
    for item in context.cookies():
        domain = (item.get("domain") or "").lstrip(".")
        if host.endswith(domain) or domain.endswith(host):
            cookies[item["name"]] = item["value"]
    return cookies


def _session_ok(page, base_url: str) -> bool:
    try:
        url = page.url or ""
    except Exception:  # pragma: no cover
        return False
    if "authserver" in url or "identity.guet.edu.cn" in url:
        return False
    if not url.startswith(base_url):
        return False
    try:
        status = page.evaluate(
            "async () => { const r = await fetch('/api/config?no-intercept=true',"
            " {headers: {'X-Requested-With': 'XMLHttpRequest'}}); return r.status; }"
        )
        return int(status) == 200
    except Exception:  # pragma: no cover
        return False


def _try_form_login(page, account: AccountConfig, logger) -> bool:
    """尽力用账号密码登录 CAS（可选，默认关闭）。选择器按 CAS 页面常见结构逐个尝试。"""
    user_selectors = ["#username", "input[name='username']", "#un", "input#username"]
    pass_selectors = ["#password", "input[name='password']", "#pd", "input#password"]
    submit_selectors = ["#login_submit", "button[type='submit']", ".auth_login_btn", "#login_submit_btn"]

    def fill(selectors, value: str) -> bool:
        for sel in selectors:
            try:
                locator = page.locator(sel)
                if locator.count() and locator.first.is_visible():
                    locator.first.fill(value)
                    return True
            except Exception:
                continue
        return False

    if "authserver" not in (page.url or ""):
        return False
    if not fill(user_selectors, account.username):
        logger.warning("CAS 页面没找到用户名字段，转为手动登录")
        return False
    if not fill(pass_selectors, account.password):
        logger.warning("CAS 页面没找到密码字段，转为手动登录")
        return False
    for sel in submit_selectors:
        try:
            locator = page.locator(sel)
            if locator.count() and locator.first.is_visible():
                locator.first.click()
                logger.info("已提交 CAS 登录表单")
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:  # pragma: no cover
        return False


def _wait_until_logged_in(page, app: AppConfig, account: AccountConfig, logger) -> bool:
    rt = app.runtime
    deadline = time.time() + max(30, int(rt.login_timeout))
    last_hint = 0.0

    while time.time() < deadline:
        if _session_ok(page, rt.base_url):
            return True
        now = time.time()
        if now - last_hint > 20:
            last_hint = now
            logger.info("等待登录完成……（当前页面：%s）", (page.url or "")[:90])
        page.wait_for_timeout(2000)
    return False


def login(app: AppConfig, account: AccountConfig, headless: bool = False) -> dict[str, str]:
    """打开有头浏览器完成一次登录，结果落在 profiles/<name>/ 里。返回 cookie。"""
    rt = app.runtime
    logger = app.logger(f"login.{account.name}", account)
    profile = app.profile_path(account)
    logger.info("登录 %s（profile=%s）", account.name, profile)

    with browser_slot(), profile_lock(profile):
        pw = start_playwright()
        ctx = None
        try:
            ctx = launch_context(pw, profile, rt, headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{rt.base_url}/user/index", wait_until="domcontentloaded", timeout=60000)
            if rt.auto_login and account.username and account.password:
                _try_form_login(page, account, logger)
            if not _wait_until_logged_in(page, app, account, logger):
                raise LoginRequired(
                    "登录没有完成（超时）。请在打开的窗口里完成 CAS 登录/扫码，或检查 accounts.yaml 的账号密码。"
                )
            cookies = _context_cookies(ctx, rt.base_url)
            if not cookies.get("session"):
                raise SessionError("登录后没有拿到 session cookie，登录未真正生效")
            logger.info("登录成功，已拿到 %d 个 cookie（%s）", len(cookies), ", ".join(sorted(cookies)))
            return cookies
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # pragma: no cover
                    pass
            try:
                pw.stop()
            except Exception:  # pragma: no cover
                pass


def cookies_from_profile(app: AppConfig, account: AccountConfig) -> dict[str, str]:
    """headless 起一次浏览器，验证会话并读出最新 cookie。"""
    rt = app.runtime
    logger = app.logger(f"session.{account.name}", account)
    profile = app.profile_path(account)
    if not profile.exists():
        raise LoginRequired(f"{account.name} 还没有 profile（{profile}），先执行：python run.py login --account {account.name}")

    with browser_slot(), profile_lock(profile):
        pw = start_playwright()
        ctx = None
        try:
            ctx = launch_context(pw, profile, rt, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{rt.base_url}{CONFIG_PROBE}", wait_until="domcontentloaded", timeout=60000)
            if "authserver" in (page.url or "") or not _session_ok(page, rt.base_url):
                raise LoginRequired(
                    f"{account.name} 的登录态已失效，重新执行：python run.py login --account {account.name}"
                )
            cookies = _context_cookies(ctx, rt.base_url)
            logger.debug("从 profile 取到 cookie：%s", ", ".join(sorted(cookies)))
            return cookies
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # pragma: no cover
                    pass
            try:
                pw.stop()
            except Exception:  # pragma: no cover
                pass


class _SlotBrowserTransport(BrowserTransport):
    """浏览器传输层：持有上下文并发名额，close 时一并关掉 Playwright 与名额。"""

    def __init__(self, rt, context, limiter, slot, playwright=None):
        super().__init__(rt, context, limiter)
        self._slot = slot
        self._playwright = playwright

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:  # pragma: no cover
                    pass
                self._playwright = None
            self._slot.release()


class _HeldSlot:
    def __init__(self):
        self._cm = None

    def acquire(self) -> None:
        self._cm = browser_slot()
        self._cm.__enter__()

    def release(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            finally:
                self._cm = None


def open_transport(
    app: AppConfig,
    account: AccountConfig,
    limiter: TokenBucket | None = None,
) -> Transport:
    """按账号配置打开传输层（http / browser）。"""
    rt = app.runtime
    engine = account.effective_engine(rt)
    if engine == "http":
        cookies = cookies_from_profile(app, account)
        return HttpTransport(rt, cookies, limiter=limiter)

    profile = app.profile_path(account)
    if not profile.exists():
        raise LoginRequired(f"{account.name} 还没有 profile，先执行：python run.py login --account {account.name}")
    slot = _HeldSlot()
    slot.acquire()
    pw = None
    try:
        pw = start_playwright()
        ctx = launch_context(pw, profile, rt, headless=rt.headless)
        return _SlotBrowserTransport(rt, ctx, limiter, slot, playwright=pw)
    except Exception:
        slot.release()
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # pragma: no cover
                pass
        raise
