"""基础工具：日志、原子写 JSON、抖动休眠、令牌桶。"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def fix_console_encoding() -> None:
    """Windows 控制台默认 GBK，中文日志会花屏；统一切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def setup_logger(name: str, log_file: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if getattr(logger, "_dsh_ready", False):
        return logger
    fix_console_encoding()
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    logger.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(fh)
    logger._dsh_ready = True  # type: ignore[attr-defined]
    return logger


def shutdown_loggers() -> None:
    """关闭所有文件 handler（Windows 下不关会导致临时目录/日志文件删不掉）。"""
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)
        if hasattr(logger, "_dsh_ready"):
            delattr(logger, "_dsh_ready")


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON；临时文件带 pid/线程/uuid，多进程并发也不会互相踩。

    Windows 上 os.replace 撞到杀软/索引器临时占用时会抛 PermissionError，
    这里做几次短退避重试（实测多线程并发写同一文件时确实会碰到）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def jitter_sleep(gap: Sequence[float]) -> None:
    lo, hi = float(gap[0]), float(gap[-1])
    lo = max(0.0, lo)
    hi = max(lo, hi)
    if hi <= 0:
        return
    time.sleep(random.uniform(lo, hi))


class TokenBucket:
    """线程安全令牌桶，用 `take()` 做阻塞式限速。"""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(0.1, float(rate))
        self.capacity = max(1.0, float(burst if burst is not None else max(1.0, self.rate)))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        want = min(max(float(n), 0.1), self.capacity)
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= want:
                    self._tokens -= want
                    return
                wait = (want - self._tokens) / self.rate
            time.sleep(min(max(wait, 0.01), 2.0))


class AtomicCounter:
    """简单的进度计数，供进度条/汇总使用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0

    def incr(self, n: int = 1) -> int:
        with self._lock:
            self.value += n
            return self.value


class LockBusy(RuntimeError):
    """锁已被另一个进程持有。"""


def pid_alive(pid: int) -> bool:
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


@contextmanager
def run_lock(path: Path, ttl_seconds: int = 6 * 3600) -> Iterator[None]:
    """进程级独占锁（pid 文件）：同一账号/同一 profile 只允许一个进程在跑。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age = 0.0
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            pass
        pid = 0
        try:
            text = path.read_text(encoding="utf-8").strip()
            pid = int(text) if text.isdigit() else 0
        except OSError:
            pass
        if age < ttl_seconds and pid_alive(pid):
            raise LockBusy(f"已被进程 {pid} 占用（{path}）")
        try:
            path.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:  # pragma: no cover - 竞态
        raise LockBusy(f"无法获取锁（{path}）") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass


def human_seconds(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
