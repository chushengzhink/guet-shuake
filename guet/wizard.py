"""一键向导：登录 → 自动拉课程 → 选课 → 开刷。

给 `start.py` 调用；所有交互都走可注入的 `ask_fn`，方便离线测试。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import report, scheduler, session
from .browser import BrowserUnavailable, configure_slots, resolve_chromium, start_playwright
from .client import SessionExpired, TronClassClient, TronClassError
from .config import AppConfig, ConfigError, RuntimeConfig, load_config
from .planner import (
    ACTION_MANUAL,
    ACTION_MATERIAL,
    ACTION_SKIP,
    ACTION_VIDEO,
    CoursePlan,
    build_plan,
)
from .runner import SPEED_PROFILES, resolve_lanes
from .session import LoginRequired, SessionError
from .transport import TransportError
from .util import TokenBucket, human_seconds, setup_logger

log = setup_logger("wizard")

AskFn = Callable[[str], str]

DEFAULT_ACCOUNTS_YAML = """\
defaults:
  course_ids: []
  semester_id: ""
  academic_year_id: ""
  speed: normal
  target_percent: 100
  engine: http

accounts:
  - name: {name}
    enabled: true
"""


# ------------------------------------------------------------------ 纯函数（可测）
def parse_selection(text: str, total: int) -> list[int] | None:
    """解析 `1,3` / `all` 这类输入，返回 0 基索引列表；`all`/空返回 None。"""
    raw = (text or "").strip().lower()
    if not raw or raw in {"all", "a", "*", "全部"}:
        return None
    picked: list[int] = []
    for part in raw.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"看不懂的选择：{part}")
        index = int(part)
        if not 1 <= index <= total:
            raise ValueError(f"序号超出范围：{index}（1~{total}）")
        if index - 1 not in picked:
            picked.append(index - 1)
    if not picked:
        return None
    return sorted(picked)


def estimate_seconds(plan: CoursePlan, rt: RuntimeConfig, account, lanes: int | None = None) -> float:
    """粗略估算一个账号刷完这门课要多久（realtime 档按路数折算）。"""
    speed = account.effective_speed(rt)
    if speed == "realtime":
        videos = [t for t in plan.tasks if t.action == ACTION_VIDEO]
        if not videos:
            return 0.0
        total = sum(max(1, t.duration) for t in videos)
        longest = max(max(1, t.duration) for t in videos)
        if lanes is None:
            raw = account.realtime_lanes if account.realtime_lanes is not None else rt.realtime_lanes
            lanes = resolve_lanes(raw, len(videos))
        return max(float(longest), math.ceil(total / max(1, int(lanes))))

    profile = SPEED_PROFILES.get(speed, SPEED_PROFILES["normal"])
    if speed == "normal":
        chunk = int(rt.chunk_seconds or profile["chunk"])
    elif profile["chunk"] is None:
        chunk = None
    else:
        chunk = int(profile["chunk"])
    gap = tuple(rt.gap) if rt.gap_explicit else tuple(profile["gap"] or (0.0, 0.0))
    parallel = max(1, int(account.per_account_jobs or rt.per_account_jobs))
    gap_avg = (sum(gap) / 2.0) / parallel

    videos = [t for t in plan.tasks if t.action == ACTION_VIDEO]
    materials = [t for t in plan.tasks if t.action == ACTION_MATERIAL]
    chunks = 0
    for task in videos:
        if chunk is None:
            chunks += 1
        else:
            chunks += max(1, math.ceil(max(1, task.duration) / chunk))
    uploads = sum(len(t.upload_ids) for t in materials)
    writes = chunks + uploads
    reads = 2 * (len(videos) + len(materials))
    rps = max(0.5, float(rt.rps))
    return writes * gap_avg + (writes + reads) / rps


def choose_accounts_from_list(app: AppConfig, ask: AskFn) -> list:
    """从 accounts.yaml 里挑要跑的账号。"""
    enabled = app.enabled_accounts()
    if len(enabled) == 1:
        return enabled
    lines = [f"{i + 1}. {acc.name}" for i, acc in enumerate(enabled)]
    report.info("可用账号：\n  " + "\n  ".join(lines))
    answer = ask(f"用哪些账号（如 1 / 1,2 / all，回车=全部）: ").strip()
    try:
        picked = parse_selection(answer, len(enabled))
    except ValueError as exc:
        report.info(f"{exc}，默认用全部")
        picked = None
    if picked is None:
        return enabled
    return [enabled[i] for i in picked]


def describe_plan(plan: CoursePlan, rt: RuntimeConfig, account) -> dict[str, Any]:
    videos = [t for t in plan.tasks if t.action == ACTION_VIDEO]
    materials = [t for t in plan.tasks if t.action == ACTION_MATERIAL]
    uploads = sum(len(t.upload_ids) for t in materials)
    return {
        "course_id": plan.course_id,
        "name": plan.name,
        "videos": len(videos),
        "video_seconds": sum(t.duration for t in videos),
        "materials": len(materials),
        "uploads": uploads,
        "done": len([t for t in plan.tasks if t.action == ACTION_SKIP]),
        "manual": len([t for t in plan.tasks if t.action == ACTION_MANUAL]),
        "eta": estimate_seconds(plan, rt, account),
        "completeness": plan.study_completeness,
    }


# ------------------------------------------------------------------ 环境与配置
def browser_ready() -> tuple[bool, str]:
    try:
        pw = start_playwright()
        try:
            executable, detail = resolve_chromium(pw)
        finally:
            pw.stop()
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, str(exc)
    path = executable or detail
    if not path or not Path(path).exists():
        return False, f"没有找到 Chromium（{path}）"
    return True, path


def ensure_accounts_file(root: Path, ask: AskFn) -> Path:
    path = root / "accounts.yaml"
    if path.exists():
        return path
    report.info("没有找到 accounts.yaml，先建一个。")
    name = ask("账号标识（字母数字，回车=acc01）: ").strip() or "acc01"
    path.write_text(DEFAULT_ACCOUNTS_YAML.format(name=name), encoding="utf-8")
    report.info(f"已写入 {path}（之后可以在里面加更多账号）")
    return path


def ensure_config_file(root: Path) -> None:
    example = root / "config.example.yaml"
    target = root / "config.yaml"
    if target.exists() or not example.exists():
        return
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    report.info(f"已复制 {example.name} → {target.name}")


# ------------------------------------------------------------------ 会话
class Preview:
    """向导阶段用的临时客户端（看完就关，执行阶段交给 scheduler 自己建）。"""

    def __init__(self, app: AppConfig, account) -> None:
        self.app = app
        self.account = account
        self.client: TronClassClient | None = None
        self.transport = None

    def __enter__(self) -> "Preview":
        limiter = TokenBucket(self.app.runtime.rps, burst=max(1.0, self.app.runtime.rps))
        self.client, self.transport = scheduler.make_client(self.app, self.account, limiter, None)
        return self

    def __exit__(self, *exc) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        self.client = None


def ensure_sessions(app: AppConfig, accounts: Sequence, ask: AskFn) -> list:
    configure_slots(1)
    usable = []
    for account in accounts:
        try:
            session.cookies_from_profile(app, account)
            report.info(f"[green]{account.name}[/green] 会话有效")
            usable.append(account)
            continue
        except LoginRequired:
            pass
        except BrowserUnavailable as exc:
            report.info(f"[red]{account.name}[/red] 浏览器不可用：{exc}")
            continue

        answer = ask(f"{account.name} 需要登录，现在打开浏览器？(Y/n) ").strip().lower()
        if answer not in {"", "y", "yes", "是"}:
            report.info(f"跳过 {account.name}")
            continue
        report.info(f"[yellow]{account.name}[/yellow] 打开浏览器，请完成 CAS 登录/扫码……")
        try:
            session.login(app, account, headless=False)
            usable.append(account)
            report.info(f"[green]{account.name}[/green] 登录成功")
        except (LoginRequired, SessionError, BrowserUnavailable) as exc:
            report.info(f"[red]{account.name}[/red] 登录失败：{exc}")
    return usable


# ------------------------------------------------------------------ 课程
@dataclass
class CourseRow:
    index: int
    course_id: int
    name: str
    code: str
    completed: int
    total: int
    completeness: float


def fetch_courses(client: TronClassClient, account) -> list[CourseRow]:
    raw = client.my_courses(account.semester_id, account.academic_year_id)
    rows: list[CourseRow] = []
    for idx, course in enumerate(raw, start=1):
        try:
            cid = int(course.get("id"))
        except (TypeError, ValueError):
            continue
        completed = total = 0
        percent = float(course.get("course_avg_completeness") or 0.0)
        try:
            data = client.my_completeness(cid)
            result = data.get("completed_result") or {}
            completed = int(result.get("total_completed") or 0)
            total = int(result.get("total_activities") or 0)
            percent = float(data.get("study_completeness") or percent)
        except Exception:
            pass
        rows.append(CourseRow(
            index=idx,
            course_id=cid,
            name=str(course.get("display_name") or course.get("name") or f"课程 {cid}"),
            code=str(course.get("course_code") or ""),
            completed=completed,
            total=total,
            completeness=percent,
        ))
    return rows


def show_courses(rows: Sequence[CourseRow]) -> None:
    try:
        from rich.table import Table

        table = Table(title="我的课程", header_style="bold")
        table.add_column("#", justify="right", width=3)
        table.add_column("课程")
        table.add_column("课程号", width=10)
        table.add_column("ID", justify="right", width=8)
        table.add_column("完成/总数", justify="right")
        table.add_column("学习完整度", justify="right")
        for row in rows:
            table.add_row(
                str(row.index), row.name, row.code, str(row.course_id),
                f"{row.completed}/{row.total}", f"{row.completeness:.1f}%",
            )
        report.console().print(table)
    except Exception:  # pragma: no cover - rich 缺失
        for row in rows:
            print(f"{row.index}. [{row.course_id}] {row.name} {row.completed}/{row.total} {row.completeness:.1f}%")


def choose_courses(rows: Sequence[CourseRow], ask: AskFn) -> list[int]:
    show_courses(rows)
    while True:
        answer = ask(f"选课程（如 1 / 1,3 / all，回车=1）: ").strip()
        try:
            picked = parse_selection(answer or "1", len(rows))
        except ValueError as exc:
            report.info(str(exc))
            continue
        if picked is None:
            return [row.course_id for row in rows]
        return [rows[i].course_id for i in picked]


def show_plans(plans: Sequence[CoursePlan], rt: RuntimeConfig, account) -> None:
    try:
        from rich.table import Table

        table = Table(title=f"待处理内容（进度以 {account.name} 为准）", header_style="bold")
        table.add_column("课程")
        table.add_column("视频", justify="right")
        table.add_column("视频总时长", justify="right")
        table.add_column("资料", justify="right")
        table.add_column("附件", justify="right")
        table.add_column("已完成", justify="right")
        table.add_column("需人工", justify="right")
        table.add_column("预计", justify="right")
        for plan in plans:
            info = describe_plan(plan, rt, account)
            table.add_row(
                f"{info['name']}（{plan.course_id}）",
                str(info["videos"]), human_seconds(info["video_seconds"]),
                str(info["materials"]), str(info["uploads"]),
                str(info["done"]), str(info["manual"]), human_seconds(info["eta"]),
            )
        report.console().print(table)
    except Exception:  # pragma: no cover
        for plan in plans:
            info = describe_plan(plan, rt, account)
            print(f"{info['name']}: 视频 {info['videos']} / 资料 {info['materials']} / 预计 {info['eta']:.0f}s")


def parse_lanes_input(text: str, default: int) -> int:
    """`8` / `all` / 空。all 返回 0（= 有多少视频开多少路）。"""
    raw = (text or "").strip().lower()
    if not raw:
        return default
    if raw in {"all", "max", "全部"}:
        return 0
    if raw.isdigit():
        value = int(raw)
        return value if value > 0 else 0
    return default


def choose_speed(rt: RuntimeConfig, ask: AskFn, bench_provider=None) -> tuple[str, int, int | None]:
    """返回 (speed, target_percent, lanes)。"""
    report.info(
        "节奏：\n"
        "  1) probe    125s 一段顶格上报，最快（实测 5 段 17 秒刷完一个 9 分钟视频）\n"
        "  2) normal   120s 一段，间隔 1.5~3s，默认\n"
        "  3) human    30s 一段 + 长间隔，慢但请求密度低\n"
        "  4) realtime 按真实时间推进，多路并行；平台不校验真实时间，这个只是更低调，不快\n"
        "  5) tick     单线程定时：每 120s 一轮，所有视频各推一段（可挂计划任务，关机也不怕）"
    )
    answer = ask("选节奏（1/2/3/4/5 或 p/n/h/r/t，回车=2）: ").strip().lower()
    mapping = {
        "1": "probe", "p": "probe", "probe": "probe",
        "3": "human", "h": "human", "human": "human",
        "4": "realtime", "r": "realtime", "realtime": "realtime", "实时": "realtime",
        "5": "tick", "t": "tick", "tick": "tick", "定时": "tick",
    }
    speed = mapping.get(answer, "normal")
    target_raw = ask("目标百分比（回车=100，课程只要求 80% 可填 80）: ").strip()
    target = int(target_raw) if target_raw.isdigit() and 1 <= int(target_raw) <= 100 else rt.target_percent
    lanes: int | None = None
    if speed == "realtime":
        lanes = _choose_lanes(rt, ask, bench_provider)
    return speed, target, lanes


def _choose_lanes(rt: RuntimeConfig, ask: AskFn, bench_provider) -> int:
    suggestion = rt.realtime_lanes if isinstance(rt.realtime_lanes, int) else 8
    suggestion = int(suggestion) if int(suggestion) > 0 else 8
    if bench_provider is not None:
        report.info("先量一下网络和任务规模……")
        try:
            advice = bench_provider()
            report.info(advice.render())
            suggestion = advice.lanes
        except Exception as exc:  # 自检失败不影响主流程
            report.info(f"性能自检失败（{exc}），先用默认 {suggestion} 路")
    raw = ask(f"同时刷几路（回车={suggestion}，all=有多少视频开多少路）: ")
    return parse_lanes_input(raw, suggestion)


# ------------------------------------------------------------------ 主流程
def run_wizard(
    root: Path,
    *,
    config_path: Path | None = None,
    accounts_path: Path | None = None,
    assume_yes: bool = False,
    ask: AskFn | None = None,
) -> int:
    root = root.resolve()
    ask_fn: AskFn = ask or input

    def safe_ask(prompt: str) -> str:
        # --yes：所有提问一律取默认值，绝不停下来等人
        if assume_yes:
            return ""
        try:
            return ask_fn(prompt)
        except EOFError:
            return ""

    def ask_yes(prompt: str, default: bool = True) -> bool:
        if assume_yes:
            return default
        answer = safe_ask(f"{prompt} ").strip().lower()
        if not answer:
            return default
        return answer in {"y", "yes", "是"}

    report.info("[bold]GUET 畅课后台刷课向导[/bold]")

    ok, detail = browser_ready()
    if not ok:
        report.info(f"[red]浏览器不可用：{detail}[/red]\n先执行：python -m playwright install chromium")
        return 1

    ensure_config_file(root)
    if accounts_path is not None:
        if not accounts_path.exists():
            report.info(f"[red]指定的账号文件不存在：{accounts_path}[/red]")
            return 1
        accounts_file = accounts_path
    else:
        accounts_file = ensure_accounts_file(root, safe_ask)
    try:
        app = load_config(root, config_path, accounts_file)
    except ConfigError as exc:
        report.info(f"[red]配置错误：{exc}[/red]")
        return 1

    accounts = choose_accounts_from_list(app, safe_ask)
    accounts = ensure_sessions(app, accounts, safe_ask)
    if not accounts:
        report.info("[red]没有可用账号，先跑 python run.py login --all[/red]")
        return 2

    outcomes: list = []
    while True:
        try:
            with Preview(app, accounts[0]) as preview:
                assert preview.client is not None
                if not preview.client.check_session():
                    raise LoginRequired(f"{accounts[0].name} 会话不可用，先 login")
                cache = scheduler.CourseCache(app.course_cache_path(), app.runtime.course_cache_ttl)
                rows = fetch_courses(preview.client, accounts[0])
                if not rows:
                    report.info("[yellow]名下没有课程：检查 accounts.yaml 的 semester_id 或学期设置[/yellow]")
                    return 1
                picked_ids = choose_courses(rows, safe_ask)
                plans = []
                for cid in picked_ids:
                    name = next((r.name for r in rows if r.course_id == cid), f"课程 {cid}")
                    plan = build_plan(preview.client, cid, app.runtime, name=name)
                    plans.append(plan)
                show_plans(plans, app.runtime, accounts[0])

                def bench_provider():
                    from .benchmark import measure, plan_scale, recommend

                    videos = [t for p in plans for t in p.tasks if t.action == ACTION_VIDEO]
                    count, total, longest = plan_scale(videos)
                    bench = measure(preview.client)
                    return recommend(
                        bench, count, total,
                        max_lanes=int(app.runtime.realtime_max_lanes),
                        eta_seconds=float(app.runtime.realtime_eta_seconds or 0),
                        longest_seconds=longest,
                    )

                speed, target, lanes = choose_speed(app.runtime, safe_ask, bench_provider)
        except LoginRequired as exc:
            report.info(f"[red]{exc}[/red]")
            return 2
        except (TronClassError, SessionExpired, TransportError) as exc:
            report.info(f"[red]拉课程失败：{exc}[/red]")
            return 1

        app.runtime.speed = speed
        app.runtime.target_percent = target
        if lanes is not None:
            app.runtime.realtime_lanes = lanes
        for account in accounts:
            account.course_ids = list(picked_ids)
            account.speed = speed
            account.target_percent = target
            if lanes is not None:
                account.realtime_lanes = lanes

        if not ask_yes(f"开始刷这 {len(picked_ids)} 门课？(Y/n)", True):
            report.info("已取消，没有发出任何写请求（重新跑 start.py 可以再选）。")
            return 1

        outcomes = scheduler.run_all(app, accounts, only=None, dry_run=False)
        report.print_outcomes(outcomes)
        report.write_result(app.result_path(), outcomes)

        if not ask_yes("继续处理其它课程？(y/N)", False):
            break

    ok_accounts = sum(1 for o in outcomes if o.ok)
    return 0 if outcomes and ok_accounts == len(outcomes) else (3 if outcomes else 1)
