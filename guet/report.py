"""终端输出：计划表、状态表、执行汇总。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    _CONSOLE = Console()
except Exception:  # pragma: no cover - rich 缺失时降级
    Console = None
    Table = None
    Panel = None
    Text = None
    _CONSOLE = None


ACTION_STYLE = {
    "video": "cyan",
    "material": "blue",
    "skip": "green",
    "manual": "yellow",
    "dry-run": "magenta",
    "done": "green",
    "failed": "red",
}


def console():
    """返回 rich Console（没有 rich 时返回 None，调用方自行降级）。"""
    return _CONSOLE


def _plain(msg: str) -> None:
    print(msg)


def info(msg: str) -> None:
    _CONSOLE.print(msg) if _CONSOLE else _plain(msg)


def print_plan(plan) -> None:
    tasks = plan.tasks
    if not Table:
        _plain(f"== {plan.name} (id={plan.course_id}) 完整度 {plan.study_completeness:.1f}%")
        for t in tasks:
            _plain(f"  {t.activity_id} {t.type:14s} {t.action:9s} {t.duration:>5}s {t.title[:30]} {t.reason}")
        return
    table = Table(title=f"{plan.name}（id={plan.course_id}） 学习完整度 {plan.study_completeness:.1f}%",
                  header_style="bold")
    table.add_column("活动ID", justify="right", width=8)
    table.add_column("类型", width=13)
    table.add_column("动作", width=9)
    table.add_column("时长", justify="right", width=7)
    table.add_column("进度", justify="right", width=6)
    table.add_column("标题", overflow="fold")
    for task in tasks:
        style = ACTION_STYLE.get(task.action, "white")
        table.add_row(
            str(task.activity_id), task.type, task.action,
            f"{task.duration}s" if task.duration else "-",
            task.state or "-",
            Text(task.title, style="") if Text else task.title,
            style=style,
        )
    _CONSOLE.print(table)


def print_plan_summary(plans: Sequence[Any]) -> None:
    for plan in plans:
        counts = plan.summary()
        info(
            f"[bold]{plan.name}[/bold]（id={plan.course_id}） 共 {counts['total']} 活动："
            f"[cyan]视频 {counts['video']}[/cyan] / [blue]资料 {counts['material']}[/blue] / "
            f"[green]已完成 {counts['skip']}[/green] / [yellow]需人工 {counts['manual']}[/yellow]"
        )


def print_status(status: dict[str, dict[str, Any]]) -> None:
    if not Table:
        for name, info_ in status.items():
            _plain(f"{name}: session={info_.get('session')} courses={info_.get('courses')}")
        return
    table = Table(title="账号状态", header_style="bold")
    table.add_column("账号")
    table.add_column("会话", justify="center")
    table.add_column("课程")
    table.add_column("完成/总数", justify="right")
    table.add_column("学习完整度", justify="right")
    for name, payload in status.items():
        session_ok = payload.get("session")
        courses = payload.get("courses") or []
        if not courses:
            table.add_row(name, "[green]OK[/green]" if session_ok else "[red]失效[/red]",
                          payload.get("error", "-"), "-", "-")
            continue
        for idx, course in enumerate(courses):
            table.add_row(
                name if idx == 0 else "",
                "[green]OK[/green]" if session_ok else "[red]失效[/red]",
                str(course.get("id", "")),
                f"{course.get('completed', '-')}/{course.get('total', '-')}",
                f"{course.get('percent', 0):.1f}%",
            )
    _CONSOLE.print(table)


def print_outcomes(outcomes: Iterable[Any]) -> None:
    outcomes = list(outcomes)
    if not Table:
        for o in outcomes:
            _plain(f"{o.account}: ok={o.ok} done={o.done} failed={o.failed} err={o.error}")
        return
    table = Table(title="执行汇总", header_style="bold")
    table.add_column("账号")
    table.add_column("结果", justify="center")
    table.add_column("成功", justify="right")
    table.add_column("失败", justify="right")
    table.add_column("请求", justify="right")
    table.add_column("用时", justify="right")
    table.add_column("完整度 前→后")
    table.add_column("备注", overflow="fold")
    for o in outcomes:
        before = " / ".join(f"{v:.0f}%" for v in o.before.values()) or "-"
        after = " / ".join(f"{v:.0f}%" for v in o.after.values()) or "-"
        table.add_row(
            o.account,
            "[green]OK[/green]" if o.ok else "[red]失败[/red]",
            str(o.done), str(o.failed), str(o.requests),
            f"{o.elapsed:.0f}s",
            f"{before} → {after}",
            o.error,
        )
    _CONSOLE.print(table)


def write_result(path: Path, outcomes: Iterable[Any]) -> None:
    payload = {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "accounts": [o.to_dict() for o in outcomes],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    info(f"结果已写入 {path}")
