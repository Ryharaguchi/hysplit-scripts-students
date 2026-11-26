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
