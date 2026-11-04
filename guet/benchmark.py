"""性能自检 + 并行路数推荐。

realtime 档的瓶颈不是带宽（每路每 120 秒才发一个请求），而是
「一次请求要多久」和「同时开多少路才不算异常」。所以自检就两件事：

1. 打几发轻量接口，量出 p50 / p95 往返延迟和失败率；
2. 结合视频数量、总时长、想要多久刷完，算出建议路数。

推荐结果会同时给出三档预设，用户想自己调就直接填数字或 `all`。
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from .util import human_seconds

DEFAULT_MAX_LANES = 16
DEFAULT_TARGET_HOURS = 1.0
MIN_LANE_SECONDS = 300.0        # 单路至少能分到 5 分钟的活儿才值得开一路


@dataclass
class BenchResult:
    rounds: int = 0
    rtt_ms: list[float] = field(default_factory=list)
    failures: int = 0

    @property
    def loss_pct(self) -> float:
        return (self.failures * 100.0 / self.rounds) if self.rounds else 0.0

    @property
    def p50(self) -> float:
        return statistics.median(self.rtt_ms) if self.rtt_ms else 0.0

    @property
    def p95(self) -> float:
        if not self.rtt_ms:
            return 0.0
        ordered = sorted(self.rtt_ms)
        index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
        return ordered[index]

    def summary(self) -> str:
        return (
            f"往返延迟 p50 {self.p50:.0f}ms / p95 {self.p95:.0f}ms，"
            f"失败 {self.failures}/{self.rounds}"
        )


@dataclass
class Advice:
    lanes: int
    hard_cap: int
    presets: dict[str, int]
    eta: dict[int, float]
    bench: BenchResult
    video_count: int
    total_seconds: float
    longest_seconds: float
    reasons: list[str] = field(default_factory=list)

    def eta_for(self, lanes: int) -> float:
        if lanes <= 0:
            return 0.0
        return max(self.longest_seconds, math.ceil(self.total_seconds / lanes))

    def render(self) -> str:
        lines = [f"性能自检：{self.bench.summary()}"]
        lines.append(
            f"任务规模：{self.video_count} 个视频，合计 {human_seconds(self.total_seconds)}，"
            f"最长单个 {human_seconds(self.longest_seconds)}"
        )
        parts = []
        for name, lanes in self.presets.items():
            parts.append(f"{name} {lanes} 路 ≈ {human_seconds(self.eta_for(lanes))}")
        lines.append("路数预设：" + " · ".join(parts))
        lines.append(f"建议：{self.lanes} 路（上限 {self.hard_cap} 路，= 视频数量/配置上限）")
        for reason in self.reasons:
            lines.append(f"  - {reason}")
        return "\n".join(lines)


def measure(client, rounds: int = 5) -> BenchResult:
    """连打几次轻量接口量延迟；不会写入任何进度。"""
    result = BenchResult()
    for _ in range(max(1, int(rounds))):
        started = time.perf_counter()
        try:
            client.my_semesters()
            ok = True
        except Exception:
            ok = False
        elapsed = (time.perf_counter() - started) * 1000.0
        result.rounds += 1
        result.rtt_ms.append(elapsed)
        if not ok:
            result.failures += 1
    return result


def recommend(
    bench: BenchResult,
    video_count: int,
    total_seconds: float,
    *,
    max_lanes: int = DEFAULT_MAX_LANES,
    eta_seconds: float = 0.0,
    longest_seconds: float | None = None,
) -> Advice:
    """按实测延迟、任务规模、期望完成时间给出路数建议。"""
    video_count = max(0, int(video_count))
    total_seconds = max(0.0, float(total_seconds))
    longest = float(longest_seconds if longest_seconds is not None else 0.0)
    reasons: list[str] = []

    hard = max(1, min(int(max_lanes), video_count or 1))
    if video_count and hard < int(max_lanes):
        reasons.append(f"只有 {video_count} 个视频，最多也就开 {hard} 路")

    if bench.loss_pct >= 40:
        hard = max(1, hard // 2)
        reasons.append(f"失败率 {bench.loss_pct:.0f}%，路数砍半，先保稳定")
    elif bench.loss_pct >= 20:
        hard = max(1, int(hard * 0.7))
        reasons.append(f"失败率 {bench.loss_pct:.0f}%，路数下调 30%")

    if bench.p95 > 1500:
        hard = max(1, min(hard, 4))
        reasons.append(f"p95 延迟 {bench.p95:.0f}ms 偏高（校园网/网关慢），建议别超过 4 路")

    if eta_seconds and eta_seconds > 0:
        need = math.ceil(total_seconds / max(MIN_LANE_SECONDS, eta_seconds))
        reasons.append(f"想 {human_seconds(eta_seconds)} 内刷完，至少需要 {need} 路")
    else:
        target = max(MIN_LANE_SECONDS, DEFAULT_TARGET_HOURS * 3600)
        need = math.ceil(total_seconds / target)
        reasons.append(f"默认目标 {human_seconds(target)} 刷完 → 需要 {max(1, need)} 路")

    lanes = max(1, min(int(need), hard))
    presets = {}
    presets["省心"] = max(1, min(3, hard))
    presets["推荐"] = lanes
    presets["极速"] = hard

    advice = Advice(
        lanes=lanes,
        hard_cap=hard,
        presets=presets,
        eta={},
        bench=bench,
        video_count=video_count,
        total_seconds=total_seconds,
        longest_seconds=longest or (total_seconds if video_count <= 1 else 0.0),
        reasons=reasons,
    )
    advice.eta = {lanes_: advice.eta_for(lanes_) for lanes_ in set(presets.values()) | {lanes, hard}}
    return advice


def plan_scale(tasks) -> tuple[int, float, float]:
    """从任务列表算 (视频数, 总秒数, 最长单个)。"""
    videos = [t for t in tasks if getattr(t, "duration", 0)]
    total = float(sum(t.duration for t in videos))
    longest = float(max((t.duration for t in videos), default=0))
    return len(videos), total, longest
