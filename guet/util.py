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


def _lock_handle(fh) -> bool:
    """非阻塞地对文件首字节加独占锁；拿不到返回 False。

    加锁前后都不要写这个文件：Windows 上把字节范围锁住后，连 close() 时的
    flush 都会 PermissionError，所以 pid 是在拿到锁之后才写到后面去。
    """
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_handle(fh) -> None:
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - 释放失败无所谓，进程退出系统会回收
        pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """进程级独占锁：同一账号 / 同一 profile 只允许一个进程在跑。

    用操作系统的文件锁（msvcrt / flock），**不用文件年龄判断**：
    进程崩溃时系统自动释放，进程活多久锁就有效多久，不存在「跑满 6 小时锁失效」的问题。
    锁文件本身保留不删（内容只是 pid，方便排查），靠锁而不是靠存在性。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        try:
            path.write_bytes(b"#")          # 首字节留给 OS 锁
        except OSError:  # pragma: no cover
            pass
    fh = open(path, "r+b")
    try:
        if not _lock_handle(fh):
            holder = ""
            try:
                fh.seek(1)                     # 跳过被 OS 锁管住的首字节
                holder = fh.read(32).decode("utf-8", "ignore").strip("\0 \r\n#")
            except OSError:
                pass
            raise LockBusy(f"已被进程 {holder or '?'} 占用（{path}）")
    except BaseException:
        fh.close()
        raise

    try:
        try:
            fh.truncate(1)                     # 保留首字节的占位，pid 写在后面
            fh.seek(1)
            fh.write(str(os.getpid()).encode())
            fh.flush()
        except OSError:  # pragma: no cover
            pass
        yield
    finally:
        _unlock_handle(fh)
        fh.close()          # 只关一次


def human_seconds(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
