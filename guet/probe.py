"""服务端接受度探针：实测「一次上报多大跨度视频进度才不被吃掉」。

用法见 `python run.py probe --account acc01 --activity 92918`。
它只对指定活动做几次上报，观察 completeness 是否真的上涨，从而推荐 speed 档：
    probe  —— 整段一次就能涨，最快
    normal —— 120s 一段能涨
    human  —— 只能小段慢慢喂
"""

from __future__ import annotations

import time
from typing import Any

from .client import TronClassClient
from .config import AccountConfig, AppConfig
from .progress import merge_ranges, plan_chunks
from .runner import _read_state
from .session import open_transport
from .util import TokenBucket, setup_logger

log = setup_logger("probe")

LADDER: list[int | None] = [None, 300, 120, 60, 30]


def probe_activity(
    app: AppConfig,
    account: AccountConfig,
    activity_id: int,
    course_id: int | None = None,
    max_rounds: int = 2,
) -> dict[str, Any]:
    rt = app.runtime
    cid = int(course_id or (account.course_ids[0] if account.course_ids else 0))
    limiter = TokenBucket(rt.rps, burst=max(1.0, rt.rps))
    transport = open_transport(app, account, limiter)
    client = TronClassClient(transport, rt, limiter=limiter)
    started = time.time()
    try:
        if not client.check_session():
            raise RuntimeError("会话不可用，先跑 login")
        detail = client.activity_detail(activity_id, cid=cid)
        duration = 0
        for upload in detail.get("uploads") or []:
            for video in upload.get("videos") or []:
                try:
                    duration = max(duration, int(float(video.get("duration") or 0)))
                except (TypeError, ValueError):
                    continue
        if duration <= 0:
            raise RuntimeError(f"活动 {activity_id} 不是可上报的视频（type={detail.get('type')}）")

        read = client.activity_read(activity_id, cid) or {}
        state, percent, ranges = _read_state(read)
        result: dict[str, Any] = {
            "account": account.name,
            "course_id": cid,
            "activity_id": activity_id,
            "title": detail.get("title") or "",
            "duration": duration,
            "before": {"state": state, "percent": percent},
            "steps": [],
            "recommend": "normal",
            "accepted_without_error": False,
        }
        if state == "full" or percent >= 100:
            result["recommend"] = "skip"
            result["note"] = "该活动已经是满进度，换一个未完成的活动探针"
            return result

        for chunk in LADDER:
            pieces = plan_chunks(ranges, duration, chunk)
            if not pieces:
                break
            start, end = pieces[0]
            before_percent = percent
            sent = 0
            for _ in range(max_rounds):
                resp = client.report_video(activity_id, start, end, cid)
                sent += 1
                data = (resp or {}).get("data") or {}
                if isinstance(data.get("ranges"), list) and data["ranges"]:
                    ranges = merge_ranges(data["ranges"])
                try:
                    percent = max(percent, int(float(data.get("completeness") or 0)))
                except (TypeError, ValueError):
                    pass
                if percent > before_percent:
                    break
            accepted = percent > before_percent
            result["steps"].append({
                "chunk": chunk if chunk else "full",
                "span": [start, end],
                "sent": sent,
                "percent_before": before_percent,
                "percent_after": percent,
                "accepted": accepted,
            })
            if accepted:
                result["accepted_without_error"] = True
                result["recommend"] = "probe" if chunk is None else ("normal" if chunk >= 120 else "human")
                break
            # 没涨：刷新一次真实进度，再换更小的段
            read = client.activity_read(activity_id, cid) or {}
            state, real_percent, ranges = _read_state(read)
            percent = max(percent, real_percent)
            if state == "full":
                result["recommend"] = "done"
                break

        result["after"] = {"state": state, "percent": percent}
        result["elapsed"] = round(time.time() - started, 1)
        if not result["accepted_without_error"] and result["recommend"] == "normal":
            result["recommend"] = "human"
            result["note"] = "所有跨度都没让进度上涨，建议 human 档并检查课程是否禁止快进/有额外风控"
        return result
    finally:
        try:
            transport.close()
        except Exception:
            pass
