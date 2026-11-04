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
from guet.benchmark import measure, plan_scale, recommend  # noqa: E402
from guet.browser import BrowserUnavailable, configure_slots  # noqa: E402
from guet.client import SessionExpired, TronClassError  # noqa: E402
from guet.config import AppConfig, ConfigError, load_config, validate  # noqa: E402
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

    bench = sub.add_parser("bench", help="性能自检：实测延迟并推荐 realtime 并行路数")
    add_common(bench, all_flag=False)
    bench.add_argument("--course", type=int, required=True)
    bench.add_argument("--eta", type=float, default=None, help="希望多少分钟刷完（可选）")
    bench.add_argument("--rounds", type=int, default=5, help="延迟采样次数")

    sched = sub.add_parser("schedule", help="生成/安装 Windows 计划任务：每 N 分钟跑一轮 tick")
    add_common(sched, all_flag=False)
    sched.add_argument("--every", type=int, default=2, help="每隔几分钟跑一轮（默认 2）")
    sched.add_argument("--name", default=None, help="计划任务名，默认 guet-shuake-<账号>")
    sched.add_argument("--install", action="store_true", help="真的创建计划任务")
    sched.add_argument("--delete", action="store_true", help="删除该计划任务")

    run = sub.add_parser("run", help="开始刷课")
    add_common(run)
    run.add_argument("--course", type=int, action="append", default=[], help="指定课程 id，可重复")
    run.add_argument("--only", default=None,
                     help="只处理这些活动 id（逗号分隔）；不写就是全部可执行活动")
    run.add_argument("--speed", choices=["probe", "normal", "human", "realtime", "tick"], default=None)
    run.add_argument("--chunk-seconds", type=int, default=None, help="normal/realtime/tick 档的段长秒数")
    run.add_argument("--interval", type=float, default=None,
                     help="tick 档每轮间隔秒数（默认=段长，即 120s）")
    run.add_argument("--once", action="store_true",
                     help="tick 档只跑一轮就退出（配合计划任务）")
    run.add_argument("--lane-threads", type=int, default=None,
                     help="tick 档同一轮内并发上报数（默认 1；>1 是同账号并发写，有顶号风险）")
    run.add_argument("--lanes", type=str, default=None,
                     help="realtime 并行路数：数字 / all（有多少开多少）/ auto（实测推荐）")
    run.add_argument("--eta", type=float, default=None, help="realtime 希望多少分钟刷完（配合 --lanes auto）")
    run.add_argument("--gap", type=float, default=None,
                     help="覆盖段间隔秒数（如 0.5 极速刷；默认跟随档位）")
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
            if attr in ("speed", "engine", "target_percent", "per_account_jobs"):
                for acc in app.accounts:  # 命令行优先于账号级覆盖
                    setattr(acc, attr, None)
    lanes = getattr(args, "lanes", None)
    if lanes is not None:
        rt.realtime_lanes = _parse_lanes_arg(lanes)
        for acc in app.accounts:
            acc.realtime_lanes = None
    eta = getattr(args, "eta", None)
    if eta:
        rt.realtime_eta_seconds = int(float(eta) * 60)
    gap = getattr(args, "gap", None)
    if gap is not None:
        rt.gap_min = rt.gap_max = max(0.0, float(gap))
        rt.gap_explicit = True
    interval = getattr(args, "interval", None)
    if interval is not None:
        rt.tick_interval = int(float(interval))
    if getattr(args, "once", False):
        rt.tick_once = True
    lane_threads = getattr(args, "lane_threads", None)
    if lane_threads is not None:
        rt.tick_threads = max(1, int(lane_threads))
    if getattr(args, "no_telemetry", False):
        rt.telemetry = False
    if getattr(args, "headed", False):
        rt.headless = False
    if getattr(args, "headless", None):
        rt.headless = True


def _parse_lanes_arg(value: str):
    text = str(value).strip().lower()
    if text in {"auto", "all", "max"}:
        return text
    try:
        return int(text)
    except ValueError:
        raise ConfigError(f"--lanes 只接受数字、all 或 auto，收到 {value!r}")


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


def cmd_bench(app: AppConfig, args) -> int:
    account = pick_accounts(app, args)[0]
    client, transport = _make_client(app, account)
    try:
        if not client.check_session():
            raise LoginRequired(f"{account.name} 会话不可用，先跑 login")
        plan = build_plan(client, int(args.course), app.runtime)
        videos = [t for t in plan.tasks if t.action == "video"]
        count, total, longest = plan_scale(videos)
        bench = measure(client, rounds=args.rounds)
        eta_seconds = (float(args.eta) * 60 if args.eta else float(app.runtime.realtime_eta_seconds or 0))
        advice = recommend(
            bench, count, total,
            max_lanes=int(app.runtime.realtime_max_lanes),
            eta_seconds=eta_seconds,
            longest_seconds=longest,
        )
        report.info(advice.render())
        report.info(f"\n写入配置：realtime_lanes: {advice.lanes}（或 auto，让每次运行自己实测再算）")
        return EXIT_OK
    finally:
        transport.close()


def cmd_schedule(app: AppConfig, args) -> int:
    """把「每 N 分钟跑一轮 tick」做成 Windows 计划任务，关机断网都能续。"""
    import os
    import subprocess

    account = pick_accounts(app, args)[0]
    name = args.name or f"guet-shuake-{account.name}"

    if args.delete:
        cmd = ["schtasks", "/delete", "/tn", name, "/f"]
        print(subprocess.list2cmdline(cmd))
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            report.info(f"删除失败（返回码 {result.returncode}）：任务可能不存在，或需要管理员权限")
            return EXIT_ERROR
        return EXIT_OK

    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "guet-shuake"
    base.mkdir(parents=True, exist_ok=True)
    bat = base / f"tick-{account.name}.bat"
    log = app.root / app.runtime.state_dir / "logs" / f"tick-{account.name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    cfg_parts = []
    if app.config_path:
        cfg_parts.append(f'--config "{app.config_path}"')
    if app.accounts_path:
        cfg_parts.append(f'--accounts "{app.accounts_path}"')
    cfg_args = (" " + " ".join(cfg_parts)) if cfg_parts else ""

    body = "\r\n".join([
        "@echo off",
        # bat 用 GBK 存，这里先把代码页钉回 936，否则中文路径会被按 UTF-8 解成乱码
        "chcp 936 >nul",
        f'cd /d "{app.root}"',
        f'"{sys.executable}" "{ROOT / "run.py"}"{cfg_args} run --account {account.name} '
        f'--speed tick --once >> "{log}" 2>&1',
        "",
    ])
    bat.write_text(body, encoding="gbk", errors="replace")

    cmd = ["schtasks", "/create", "/tn", name, "/tr", str(bat),
           "/sc", "minute", "/mo", str(max(1, int(args.every))), "/f"]
    line = subprocess.list2cmdline(cmd)
    report.info(f"每 {args.every} 分钟跑一轮 tick（单轮模式，进度存 data/state.{account.name}.json）")
    report.info(f"包装脚本：{bat}")
    report.info(f"日志：{log}")
    report.info(f"\n命令：\n  {line}")
    if not args.install:
        report.info("\n加 --install 直接创建；想撤销加 --delete")
        return EXIT_OK
    result = subprocess.run(cmd, check=False)
    if result.returncode == 0:
        report.info(f"已创建计划任务 {name}；查看：schtasks /query /tn \"{name}\"")
        return EXIT_OK
    report.info(f"创建失败（返回码 {result.returncode}）：用管理员 PowerShell 重跑上面的命令")
    return EXIT_ERROR


def parse_only(value: str | None) -> set[int] | None:
    """解析 --only。

    只有「完全没给这个参数」（None）才表示不限活动；
    显式给了却解析不出任何 ID（`--only ""`、`--only ","`、`--only "，"`）一律报错，
    否则会静默退化成「处理所选课程的全部可执行活动」。
    """
    if value is None:
        return None
    picked: set[int] = set()
    for part in str(value).replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ConfigError(
                f"--only 只接受活动 ID（如 92918,92919），收到 {part!r}；"
                "不想限制就整个省略这个参数"
            )
        picked.add(int(part))
    if not picked:
        raise ConfigError(
            f"--only 给了空值（{value!r}）：要么写活动 ID，要么整个省略这个参数"
        )
    return picked


def cmd_run(app: AppConfig, args) -> int:
    accounts = pick_accounts(app, args)
    only = parse_only(args.only)
    for account in accounts:
        if args.course:
            account.course_ids = [int(c) for c in args.course]
    outcomes = scheduler.run_all(app, accounts, only=only, dry_run=args.dry_run)
    report.print_outcomes(outcomes)
    report.write_result(app.result_path(), outcomes)
    if all(o.ok for o in outcomes):
        return EXIT_OK
    if all(not o.ok and "另一个进程" in o.error for o in outcomes):
        return EXIT_ERROR
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
    try:
        apply_overrides(app, args)
        validate(app)          # 命令行覆盖后必须重新校验，否则 --target 101 之类能溜进去
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        return EXIT_ERROR

    handlers = {
        "login": cmd_login,
        "status": cmd_status,
        "list": cmd_list,
        "probe": cmd_probe,
        "bench": cmd_bench,
        "schedule": cmd_schedule,
        "run": cmd_run,
    }
    try:
        return handlers[args.command](app, args)
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        return EXIT_ERROR
    except LoginRequired as exc:
        log.error("%s", exc)
        return EXIT_NO_SESSION
    except (TransportError, TronClassError, SessionExpired) as exc:
        log.error("接口调用失败：%s", exc)
        return EXIT_ERROR
    except BrowserUnavailable as exc:
        log.error("浏览器不可用：%s", exc)
        return EXIT_ERROR
    except RuntimeError as exc:      # 探针等模块抛的运行时错误
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.warning("已中断（state.json 里保留了进度，重跑会接着做）")
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
