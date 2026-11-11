"""Conservative preflight checks for HYSPLIT ARL meteorological files.

filedates is NOAA's optional ARL inspection utility. Name-based planning is not
proof that the file contains the model times required by a trajectory.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
_DATE_LINE = re.compile(r"^\s*(\d{2,4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,3})\s*$")


@dataclass(frozen=True)
class CoverageReport:
    verified: bool
    messages: tuple[str, ...]
    first: datetime | None = None
    last: datetime | None = None
    time_count: int = 0


def check_signature(path: Path) -> None:
    """Reject common wrong download formats; not a full ARL format validator."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Missing or empty meteorological file: {path}")
    with path.open("rb") as stream:
        head = stream.read(128)
    lower = head.lstrip().lower()
    if (head.startswith((b"GRIB", b"CDF", b"\x89HDF\r\n\x1a\n", b"PK\x03\x04"))
            or lower.startswith((b"<!doctype", b"<html", b"<?xml", b"{\"error\""))):
        raise ValueError(f"Expected an ARL file, but received another format: {path.name}")


def parse_filedates(text: str) -> tuple[datetime, ...]:
    """Read NOAA 'filedates' YY MM DD HH FF records (FF = forecast hour)."""
    dates = set()
    for line in text.splitlines():
        found = _DATE_LINE.fullmatch(line)
        if not found:
            continue
        yy, mm, dd, hh, ff = map(int, found.groups())
        year = yy if yy >= 100 else (2000 + yy if yy <= 69 else 1900 + yy)
        try:
            dates.add(datetime(year, mm, dd, hh, tzinfo=UTC) + timedelta(hours=ff))
        except ValueError:
            continue
    return tuple(sorted(dates))


def check_coverage(met_paths: list[Path], earliest: datetime, latest: datetime,
                   filedates: Path | None = None, *, max_gap_hours: float = 6) -> CoverageReport:
    """Inspect all meteorological timestamps when NOAA filedates is available.

    Does not validate grid extent, altitude coverage, fields or trajectory
    physics; inspect HYSPLIT's MESSAGE and compare to a reference run.
    """
    if earliest.tzinfo is None or latest.tzinfo is None:
        raise ValueError("Coverage bounds must be timezone-aware UTC")
    earliest = earliest.astimezone(UTC)
    latest = latest.astimezone(UTC)
    if earliest > latest or max_gap_hours <= 0:
        raise ValueError("Invalid time window or maximum time gap")
    paths = [Path(p) for p in met_paths]
    if not paths:
        raise ValueError("No meteorological files selected")
    for path in paths:
        check_signature(path)
    if filedates is None or not Path(filedates).is_file():
        return CoverageReport(False, ("NOAA filedates utility not installed: ARL timestamps unverified.",))
    times = set()
    for path in paths:
        result = subprocess.run([str(Path(filedates).resolve()), str(path.resolve())],
                                capture_output=True, text=True, errors="replace", timeout=180)
        if result.returncode:
            raise RuntimeError(f"filedates failed for {path.name}: {result.stderr.strip()[:400]}")
        discovered = parse_filedates(result.stdout)
        if not discovered:
            raise ValueError(f"filedates could not identify any periods in {path.name}.")
        times.update(discovered)
    ordered = sorted(times)
    if ordered[0] > earliest or ordered[-1] < latest:
        raise ValueError(
            f"ARL time coverage insufficient: {ordered[0].isoformat()} to "
            f"{ordered[-1].isoformat()}; required {earliest.isoformat()} to {latest.isoformat()}"
        )
    useful = [t for t in ordered if earliest - timedelta(hours=max_gap_hours) <= t
              <= latest + timedelta(hours=max_gap_hours)]
    gaps = [(a, b) for a, b in zip(useful, useful[1:]) if
            (b - a).total_seconds() > max_gap_hours * 3600]
    if gaps:
        a, b = gaps[0]
        raise ValueError(f"Gap in ARL meteorology: {a.isoformat()} to {b.isoformat()}")
    return CoverageReport(True, (
        f"Verified {len(ordered)} ARL valid times: {ordered[0].isoformat()} to {ordered[-1].isoformat()}.",
    ), ordered[0], ordered[-1], len(ordered))
