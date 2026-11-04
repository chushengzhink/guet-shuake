"""GUI 启动器：起本地服务 → 用系统 Edge 的 --app 窗口打开 → 等窗口关闭。"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from ..browser import find_edge_exe
from ..util import add_global_sink, fix_console_encoding, setup_logger
from .server import AppState, _LogHandler, build_server

log = setup_logger("gui.launcher")


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "GuetShuake"


def prepare_bundled_browser() -> str | None:
    """安装包自带浏览器内核时，让 Playwright 直接用它（用户无需联网下载）。"""
    if not hasattr(sys, "_MEIPASS"):
        return None
    bundled = Path(sys._MEIPASS) / "browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
        return str(bundled)
    return None


def open_window(url: str, data_dir: Path) -> subprocess.Popen | None:
    """优先用 Edge 的 app 模式（无地址栏，像原生窗口）；没有 Edge 就退回默认浏览器。"""
    edge = find_edge_exe()
    if not edge:
        webbrowser.open(url)
        return None
    return subprocess.Popen(
        [
            edge,
            f"--app={url}",
            f"--user-data-dir={data_dir / 'ui-profile'}",
            "--window-size=1200,880",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _attach_log_handler(state: AppState) -> None:
    handler = _LogHandler(state)
    handler.setLevel(logging.INFO)

    class _Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            name = record.name
            return not (name.startswith("playwright") or name.startswith("urllib3"))

    handler.addFilter(_Filter())
    # 挂到全局 sink：各模块 logger 是 propagate=False，只挂 root 是收不到的
    add_global_sink(handler)

    # 打包成无控制台的 exe 后，屏幕上看不到 stderr，这里落一份文件日志便于排查
    try:
        log_dir = state.root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "gui.log", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
        file_handler.addFilter(_Filter())
        add_global_sink(file_handler)
    except OSError:  # pragma: no cover
        pass


def main(argv: list[str] | None = None) -> int:
    fix_console_encoding()
    parser = argparse.ArgumentParser(prog="guet-shuake", description="GUET 畅课助手（图形界面）")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认 %%LOCALAPPDATA%%\\GuetShuake）")
    parser.add_argument("--port", type=int, default=0, help="本地端口，0 = 随机")
    parser.add_argument("--no-window", action="store_true", help="不开窗口，只打印访问地址")
    parser.add_argument("--url-file", type=Path, default=None,
                        help="把访问地址写到指定文件（自动化测试用；打包成无控制台 exe 时也能拿到 URL）")
    args = parser.parse_args(argv)

    data_dir = (args.data_dir or default_data_dir()).resolve()
    bundled = prepare_bundled_browser()
    state = AppState(data_dir)
    state.ensure_files()
    state.reload()
    _attach_log_handler(state)
    if bundled:
        log.info("使用安装包内置浏览器内核：%s", bundled)

    server = build_server(state, args.port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={state.token}"
    thread = threading.Thread(target=server.serve_forever, name="gui-http", daemon=True)
    thread.start()
    log.info("GUI 已启动：%s", url)
    log.info("数据目录：%s", data_dir)
    if args.url_file:
        try:
            args.url_file.write_text(url + "\n", encoding="utf-8")
        except OSError:
            pass

    if args.no_window:
        print(url, flush=True)
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0

    window = open_window(url, data_dir)
    try:
        if window is not None:
            # 关窗、页面里点退出、Ctrl-C，三种都走 shutdown 事件
            while not state.shutdown.is_set() and window.poll() is None:
                state.shutdown.wait(0.3)
        else:  # 没有 Edge：等 /api/quit 或 Ctrl-C
            while not state.shutdown.is_set():
                state.shutdown.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        from .. import cancel

        cancel.request()
        state.shutdown.set()
        worker = state.run_thread
        if worker is not None and worker.is_alive():
            log.info("等待后台任务收尾……")
            worker.join(timeout=8)          # 有界等待，不无限挂
        server.shutdown()
        log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
