#!/usr/bin/env python
"""GUET 畅课（TronClass）后台刷课工具 —— 命令行入口。

    python run.py login  [--account acc01] [--all]
    python run.py status [--all]
    python run.py list   --course 29066
    python run.py probe  --account acc01 --activity 92918 [--course 29066]
    python run.py run    [--all] [--course 29066] [--dry-run] ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet import report, scheduler, session  # noqa: E402
from guet.browser import BrowserUnavailable, configure_slots  # noqa: E402
from guet.client import SessionExpired, TronClassError  # noqa: E402
from guet.config import AppConfig, ConfigError, load_config  # noqa: E402
from guet.planner import build_plan  # noqa: E402
from guet.probe import probe_activity  # noqa: E402
from guet.session import LoginRequired, SessionError  # noqa: E402
from guet.transport import TransportError  # noqa: E402
from guet.util import TokenBucket, setup_logger  # noqa: E402

log = setup_logger("cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_SESSION = 2
EXIT_PARTIAL = 3


def _fix_console_encoding() -> None:
    """Windows 控制台默认 GBK，中文日志会花屏；统一切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# ---------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="GUET 畅课（TronClass）后台刷课工具：浏览器取会话 + 直接调接口刷进度",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python run.py login --all\n"
               "  python run.py status\n"
               "  python run.py run --all --dry-run\n"
               "  python run.py run --all --speed normal --target 100\n",
    )
    parser.add_argument("--config", type=Path, default=None, help="config.yaml 路径")
    parser.add_argument("--accounts", type=Path, default=None, help="accounts.yaml 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, all_flag: bool = True) -> None:
        p.add_argument("--account", action="append", default=[],
                       help="账号名，可逗号分隔或重复给多个")
        if all_flag:
            p.add_argument("--all", action="store_true", help="accounts.yaml 里所有 enabled 账号")

    login = sub.add_parser("login", help="用后台浏览器完成一次登录并保存 profile")
    add_common(login)
    login.add_argument("--show", action="store_true", help="显示浏览器窗口（默认就显示）")

    status = sub.add_parser("status", help="只读体检：会话是否有效 + 课程完整度")
    add_common(status)

    listing = sub.add_parser("list", help="打印课程结构与每个活动的处理计划")
    add_common(listing)
    listing.add_argument("--course", type=int, required=True, help="课程 id，如 29066")

    probe = sub.add_parser("probe", help="实测服务端接受度，推荐 speed 档")
    add_common(probe, all_flag=False)
    probe.add_argument("--activity", type=int, required=True)
    probe.add_argument("--course", type=int, default=None)

    run = sub.add_parser("run", help="开始刷课")
    add_common(run)
    run.add_argument("--course", type=int, action="append", default=[], help="指定课程 id，可重复")
    run.add_argument("--only", default="", help="只处理这些活动 id，逗号分隔")
    run.add_argument("--speed", choices=["probe", "normal", "human"], default=None)
    run.add_argument("--chunk-seconds", type=int, default=None, help="normal 档的段长秒数")
    run.add_argument("--target", dest="target_percent", type=int, default=None, help="目标百分比，默认 100")
    run.add_argument("--engine", choices=["http", "browser"], default=None)
    run.add_argument("--headless", action="store_true", default=None, help="无头运行（默认无头）")
    run.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    run.add_argument("--account-jobs", type=int, default=None, help="同时跑几个账号")
    run.add_argument("--per-account-jobs", type=int, default=None, help="单账号交错段数（>1 会摊薄间隔）")
    run.add_argument("--rps", type=float, default=None)
    run.add_argument("--global-rps", type=float, default=None)
    run.add_argument("--no-telemetry", action="store_true", help="不同步 /statistics 埋点")
    run.add_argument("--dry-run", action="store_true", help="只列计划，不发写请求")
    return parser


def pick_accounts(app: AppConfig, args) -> list:
    names: list[str] = []
    for chunk in getattr(args, "account", []) or []:
        names += [item for item in chunk.replace(",", " ").split() if item]
    if names:
        return [app.account(name) for name in names]
    return app.enabled_accounts()


def apply_overrides(app: AppConfig, args) -> None:
    rt = app.runtime
    simple = ["speed", "chunk_seconds", "engine", "account_jobs", "per_account_jobs",
              "rps", "global_rps", "target_percent"]
    for attr in simple:
        value = getattr(args, attr, None)
        if value is not None:
            setattr(rt, attr, value)
            if attr in ("speed", "engine", "target_percent"):
                for acc in app.accounts:  # 命令行优先于账号级覆盖
                    setattr(acc, {"speed": "speed", "engine": "engine",
                                  "target_percent": "target_percent"}[attr], None)
    if getattr(args, "no_telemetry", False):
        rt.telemetry = False
    if getattr(args, "headed", False):
        rt.headless = False
    if getattr(args, "headless", None):
        rt.headless = True


def _load(args) -> AppConfig:
    return load_config(ROOT, args.config, args.accounts)


# ---------------------------------------------------------------- 子命令
def cmd_login(app: AppConfig, args) -> int:
    accounts = pick_accounts(app, args)
    configure_slots(1)
    failures = 0
    for account in accounts:
        try:
            session.login(app, account, headless=False)
        except (LoginRequired, SessionError, BrowserUnavailable) as exc:
            failures += 1
            log.error("账号 %s 登录失败：%s", account.name, exc)
        except Exception as exc:  # pragma: no cover - 兜底
            failures += 1
            log.exception("账号 %s 登录异常：%s", account.name, exc)
    ok = len(accounts) - failures
    report.info(f"登录完成：成功 {ok} / 共 {len(accounts)}")
    return EXIT_OK if failures == 0 else EXIT_PARTIAL


def cmd_status(app: AppConfig, args) -> int:
    accounts = pick_accounts(app, args)
    status = scheduler.collect_status(app, accounts)
    report.print_status(status)
    if all(not payload.get("session") for payload in status.values()):
        return EXIT_NO_SESSION
    return EXIT_OK


def _make_client(app: AppConfig, account):
    limiter = TokenBucket(app.runtime.rps, burst=max(1.0, app.runtime.rps))
    client, transport = scheduler.make_client(app, account, limiter, None)
    return client, transport


def cmd_list(app: AppConfig, args) -> int:
    account = pick_accounts(app, args)[0]
    client, transport = _make_client(app, account)
    try:
        if not client.check_session():
            raise LoginRequired(f"{account.name} 会话不可用，先跑 login")
        plan = build_plan(client, int(args.course), app.runtime)
        report.print_plan(plan)
        report.print_plan_summary([plan])
        return EXIT_OK
    finally:
        transport.close()


def cmd_probe(app: AppConfig, args) -> int:
    account = pick_accounts(app, args)[0]
    result = probe_activity(app, account, int(args.activity), args.course)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_run(app: AppConfig, args) -> int:
    accounts = pick_accounts(app, args)
    only = {int(x) for x in args.only.replace(",", " ").split() if x.strip().isdigit()} or None
    for account in accounts:
        if args.course:
            account.course_ids = [int(c) for c in args.course]
    outcomes = scheduler.run_all(app, accounts, only=only, dry_run=args.dry_run)
    report.print_outcomes(outcomes)
    report.write_result(app.result_path(), outcomes)
    if all(o.ok for o in outcomes):
        return EXIT_OK
    if all(not o.ok and ("会话" in o.error or "login" in o.error) for o in outcomes):
        return EXIT_NO_SESSION
    return EXIT_PARTIAL


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        app = _load(args)
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        return EXIT_ERROR
    apply_overrides(app, args)

    handlers = {
        "login": cmd_login,
        "status": cmd_status,
        "list": cmd_list,
        "probe": cmd_probe,
        "run": cmd_run,
    }
    try:
        return handlers[args.command](app, args)
    except LoginRequired as exc:
        log.error("%s", exc)
        return EXIT_NO_SESSION
    except (TransportError, TronClassError, SessionExpired) as exc:
        log.error("接口调用失败：%s", exc)
        return EXIT_ERROR
    except BrowserUnavailable as exc:
        log.error("浏览器不可用：%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.warning("已中断（state.json 里保留了进度，重跑会接着做）")
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
