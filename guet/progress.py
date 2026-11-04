"""已观看区间的数学运算（区间合并 / 求补 / 切段）。

TronClass 的视频进度就是一组 `[start, end]` 秒区间：
    POST /api/course/activities-read/{aid}  {"start": 0, "end": 4}
    -> {"data": {"completeness": 1, "ranges": [[0, 4]]}}
刷课的本质就是把这组区间补到 [0, floor(duration)]。
"""

from __future__ import annotations

from typing import Iterable, Sequence

Interval = tuple[int, int]


def _norm(pair: Sequence[float]) -> Interval | None:
    if pair is None or len(pair) < 2:
        return None
    try:
        start, end = int(float(pair[0])), int(float(pair[1]))
    except (TypeError, ValueError):
        return None
    if end < start:
        start, end = end, start
    if end <= start:
        return None
    return start, end


def merge_ranges(ranges: Iterable[Sequence[float]] | None) -> list[Interval]:
    """合并重叠/相邻区间，返回按起点排序的列表。"""
    items: list[Interval] = []
    for raw in ranges or []:
        norm = _norm(raw)
        if norm:
            items.append(norm)
    items.sort()
    merged: list[Interval] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def covered_seconds(ranges: Iterable[Sequence[float]] | None) -> int:
    return sum(end - start for start, end in merge_ranges(ranges))


def coverage_percent(ranges: Iterable[Sequence[float]] | None, total: float) -> float:
    if total <= 0:
        return 0.0
    return min(100.0, covered_seconds(ranges) * 100.0 / float(total))


def complement(ranges: Iterable[Sequence[float]] | None, total: float) -> list[Interval]:
    """[0, floor(total)] 内还没看过的区间。"""
    end_cap = int(float(total))
    if end_cap <= 0:
        return []
    gaps: list[Interval] = []
    cursor = 0
    for start, end in merge_ranges(ranges):
        if end <= 0 or start >= end_cap:
            continue
        start = max(0, start)
        end = min(end_cap, end)
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
        if cursor >= end_cap:
            break
    if cursor < end_cap:
        gaps.append((cursor, end_cap))
    return gaps


def split_segments(segments: Sequence[Interval], chunk: int | None) -> list[Interval]:
    """把待看区间按 chunk 秒切段；chunk 为 None/0 则整段一次上报。"""
    if not chunk or chunk <= 0:
        return list(segments)
    pieces: list[Interval] = []
    for start, end in segments:
        cursor = start
        while cursor < end:
            nxt = min(end, cursor + chunk)
            pieces.append((cursor, nxt))
            cursor = nxt
    return pieces


def plan_chunks(
    watched: Iterable[Sequence[float]] | None,
    total: float,
    chunk: int | None,
) -> list[Interval]:
    """根据已看区间和总时长，算出还需要上报的分段。"""
    return split_segments(complement(watched, total), chunk)
