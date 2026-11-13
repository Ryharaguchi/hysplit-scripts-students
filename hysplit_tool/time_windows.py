"""Non-contiguous UTC meteorological windows for unattended trajectory batches.

All user-visible run identifiers are JST; meteorological validity and HYSPLIT
CONTROL times remain UTC. Each trajectory needs only its own backward window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

UTC = timezone.utc


@dataclass(frozen=True, order=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Meteorological time windows must have explicit UTC timezone")
        if self.start.utcoffset() != timedelta(0) or self.end.utcoffset() != timedelta(0):
            raise ValueError("Meteorological time windows must be UTC")
        if self.end < self.start:
            raise ValueError("Meteorological window end must not precede its start")

    def as_pair(self) -> tuple[datetime, datetime]:
        return self.start, self.end


def merge_windows(
    windows: Iterable[TimeWindow], *,
    max_gap: timedelta = timedelta(0),
) -> tuple[TimeWindow, ...]:
    """Return sorted disjoint coverage intervals; merge overlap and contact."""
    if max_gap < timedelta(0):
        raise ValueError("Gap tolerance must be nonnegative")
    ordered = sorted(set(windows))
    if not ordered:
        return ()
    result = [ordered[0]]
    for item in ordered[1:]:
        last = result[-1]
        if item.start <= last.end + max_gap:
            result[-1] = TimeWindow(last.start, max(last.end, item.end))
        else:
            result.append(item)
    return tuple(result)


def windows_for_starts(
    starts: Iterable[datetime], back_hours: int,
) -> tuple[TimeWindow, ...]:
    """Merge the required backward trajectory intervals, never their empty gaps."""
    if back_hours <= 0:
        raise ValueError("Backward duration must be positive")
    duration = timedelta(hours=back_hours)
    times = sorted(set(starts))
    if not times:
        raise ValueError("No trajectory starts")
    return merge_windows(TimeWindow(when-duration, when) for when in times)


def find_window(windows: Iterable[TimeWindow], start: datetime,
                back_hours: int) -> TimeWindow:
    """Find the one merged window containing a trajectory's full duration."""
    needed = TimeWindow(start-timedelta(hours=back_hours), start)
    for window in windows:
        if window.start <= needed.start and window.end >= needed.end:
            return window
    raise ValueError(f"No meteorological window covers {start.isoformat()}")


def split_window(
    window: TimeWindow,
    *,
    max_span: timedelta = timedelta(days=13),
    overlap: timedelta = timedelta(hours=1),
) -> tuple[TimeWindow, ...]:
    """Split long remote ARCO requests with overlap across chunk boundaries.

    Each Google Zarr export stays within its single-call temporal limit,
    while the full original meteorology interval remains covered.
    """
    if max_span <= timedelta(0) or overlap < timedelta(0) or overlap >= max_span:
        raise ValueError("Invalid chunk duration or overlap")
    if window.end - window.start <= max_span:
        return (window,)
    chunks: list[TimeWindow] = []
    start = window.start
    while start < window.end:
        end = min(start + max_span, window.end)
        chunks.append(TimeWindow(start, end))
        if end == window.end:
            break
        start = end - overlap
    return tuple(chunks)
