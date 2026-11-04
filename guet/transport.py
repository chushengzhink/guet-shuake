"""传输层：一套接口，两种实现。

* HttpTransport    —— requests.Session，cookie 由浏览器 profile 导出的初始值 + 服务端 Set-Cookie 自动轮换
* BrowserTransport —— Playwright 后台 Chromium 的 context.request（cookie 与页面完全共享）

两者都返回 HttpResult，让上层的 client 完全不感知引擎差异。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

from .config import RuntimeConfig
from .util import TokenBucket

JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}


@dataclass
class HttpResult:
    status: int
    url: str = ""
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def data(self) -> Any:
        if not self.text:
            return None
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str) -> str:
        lowered = {k.lower(): v for k, v in self.headers.items()}
        return lowered.get(name.lower(), "")

    def brief(self, limit: int = 300) -> str:
        body = (self.text or "").replace("\n", " ")
        return f"{self.status} {body[:limit]}"


class TransportError(RuntimeError):
    """网络层错误（可重试）。"""


def looks_logged_out(result: HttpResult) -> bool:
    if result.status == 401:
        return True
    url = (result.url or "").lower()
    if "authserver" in url or "/login" in url.split("?")[0]:
        return True
    location = (result.header("location") or "").lower()
    if result.status in (301, 302, 303, 307, 308) and "authserver" in location:
        return True
    if result.status == 200 and "text/html" in result.header("content-type"):
        head = (result.text or "")[:4000].lower()
        if "authserver/login" in head or "请登录" in head:
            return True
    return False


class Transport:
    """统一请求接口。"""

    engine = "base"

    def __init__(self, rt: RuntimeConfig, limiter: TokenBucket | None = None):
        self.rt = rt
        self.limiter = limiter

    # --- 子类实现 ---
    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        body: str | None,
        headers: dict[str, str],
    ) -> HttpResult:
        raise NotImplementedError

    # --- 公共入口 ---
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        body: str | None = None,
        content_type: str | None = None,
        referer: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        if self.limiter is not None:
            self.limiter.take()

        hdrs = {
            "User-Agent": self.rt.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.rt.base_url,
        }
        if referer:
            hdrs["Referer"] = referer
        else:
            # 站点 WAF 对「没有 Referer 的 XHR」会直接 403（实测 /api/config 就是这样），
            # 所以任何请求都补一个来源页，跟真实浏览器一致。
            hdrs["Referer"] = f"{self.rt.base_url}/user/courses"
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":"))
            hdrs.update(JSON_HEADERS)
        elif content_type:
            hdrs["Content-Type"] = content_type
        if headers:
            hdrs.update(dict(headers))

        url = path if path.startswith("http") else f"{self.rt.base_url.rstrip('/')}{path}"
        try:
            return self._send(method.upper(), url, params=params, body=body, headers=hdrs)
        except requests.RequestException as exc:
            raise TransportError(f"{method.upper()} {path} 网络异常：{exc}") from exc
        except Exception as exc:  # playwright 抛出的 Error
            name = type(exc).__name__
            if name in {"TargetClosedError", "Error", "TimeoutError"}:
                raise TransportError(f"{method.upper()} {path} 浏览器请求失败：{exc}") from exc
            raise

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw) -> HttpResult:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, json_body: Any = None, **kw) -> HttpResult:
        return self.request("POST", path, json_body=json_body, **kw)

    def put(self, path: str, json_body: Any = None, **kw) -> HttpResult:
        return self.request("PUT", path, json_body=json_body, **kw)

    def close(self) -> None:  # pragma: no cover - 空实现
        pass


class HttpTransport(Transport):
    engine = "http"

    def __init__(self, rt: RuntimeConfig, cookies=None, limiter: TokenBucket | None = None):
        super().__init__(rt, limiter)
        self.session = requests.Session()
        # 必须走「请求级 proxies」：requests 会先把 HTTP(S)_PROXY 环境变量
        # setdefault 进请求参数，只有请求级显式给了才会覆盖掉环境变量。
        self._proxies = (
            {"http": rt.proxy, "https": rt.proxy} if rt.proxy else None
        )
        self.session.headers.update({
            "User-Agent": rt.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._import_cookies(cookies)

    def _import_cookies(self, cookies) -> None:
        """支持 dict(name→value) 与带 domain/path/secure 的列表两种形式。"""
        if not cookies:
            return
        if isinstance(cookies, Mapping):
            for name, value in cookies.items():
                self.session.cookies.set(name, value)
            return
        for item in cookies:
            self.session.cookies.set(
                item["name"], item["value"],
                domain=item.get("domain"),
                path=item.get("path") or "/",
                secure=bool(item.get("secure")),
            )
    def _send(self, method, url, *, params, body, headers) -> HttpResult:
        resp = self.session.request(
            method,
            url,
            params=params,
            data=body.encode("utf-8") if body is not None else None,
            headers=headers,
            proxies=self._proxies,
            timeout=self.rt.timeout,
            allow_redirects=True,
        )
        return HttpResult(resp.status_code, resp.url, resp.text, dict(resp.headers))

    def cookie_dict(self) -> dict[str, str]:
        return {c.name: c.value for c in self.session.cookies}

    def close(self) -> None:
        self.session.close()


class BrowserTransport(Transport):
    engine = "browser"

    def __init__(self, rt: RuntimeConfig, context, limiter: TokenBucket | None = None):
        super().__init__(rt, limiter)
        self.context = context

    def _send(self, method, url, *, params, body, headers) -> HttpResult:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        full = f"{url}{query}"
        api = self.context.request
        if body is None:
            resp = api.fetch(full, method=method, headers=headers,
                             timeout=int(self.rt.timeout * 1000))
        else:
            resp = api.fetch(full, method=method, headers=headers, data=body,
                             timeout=int(self.rt.timeout * 1000))
        # APIResponse 会占着请求上下文里的资源，取完字段立刻 dispose，
        # 否则 tick/realtime 这种长时间运行会一直累积。
        try:
            status = resp.status
            final_url = resp.url
            resp_headers = dict(resp.headers)
            try:
                text = resp.text()
            except Exception:  # 204 之类无正文
                text = ""
        finally:
            try:
                resp.dispose()
            except Exception:  # pragma: no cover
                pass
        return HttpResult(status, final_url, text, resp_headers)

    def cookies(self) -> dict[str, str]:
        return {c["name"]: c["value"] for c in self.context.cookies()}

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:  # pragma: no cover
            pass
