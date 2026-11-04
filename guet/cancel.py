"""全局取消标记：GUI 点「停止」时置位，各执行循环检查后收尾退出。

不做线程强杀（会把 state / 会话写坏），只让循环在下一个检查点停下，
已完成的进度照常写入 state，未完成的活动下次接着做。
"""

from __future__ import annotations

import threading


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    @property
    def requested(self) -> bool:
        return self._event.is_set()


_token = CancelToken()


def current() -> CancelToken:
    return _token


def requested() -> bool:
    return _token.requested


def request() -> None:
    _token.request()


def reset() -> None:
    _token.reset()
