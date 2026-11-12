"""Pure trajectory planning, HYSPLIT CONTROL generation and local execution."""

from __future__ import annotations

import csv
import math
import re
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .platform_guard import require_native_windows

UTC = timezone.utc
JST = timezone(timedelta(hours=9), name="JST")
EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class StartPoint:
    when: datetime  # timezone-aware UTC
    latitude: float
    longitude: float
    height_m: float
    origin: str = "manual"

    def __post_init__(self):
        if self.when.tzinfo is None or self.when.utcoffset() != timedelta(0):
            raise ValueError("StartPoint.when must use UTC (timezone-aware)")
        if not (-90 <= self.latitude <= 90):
            raise ValueError("latitude must be within [-90, 90]")
        if not (-180 <= self.longitude <= 180):
            raise ValueError("longitude must be within [-180, 180]")
        if not math.isfinite(self.height_m) or self.height_m <= 0:
            raise ValueError("initial height must be a positive finite number")


@dataclass(frozen=True)
class RunGroup:
    when: datetime
    points: tuple[StartPoint, ...]
    ordinal: int


def parse_utc(value: str) -> datetime:
    """Parse UTC ISO-style datetime. Naive timestamps are explicitly interpreted as UTC."""
    value = value.strip()
    if not value:
        raise ValueError("UTC time is empty")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid UTC time: {value}; use YYYY-MM-DD HH:MM") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_heights(spec: str) -> list[float]:
    """Heights in meters: '500:10000:100' (inclusive) or '500,1000,1500'."""
    spec = spec.strip()
    if not spec:
        raise ValueError("Height specification is empty")
    if ":" in spec:
        try:
            start, stop, step = (float(x.strip()) for x in spec.split(":"))
        except (ValueError, TypeError) as exc:
            raise ValueError("Height range must be start:stop:step in meters") from exc
        if not all(math.isfinite(x) for x in (start, stop, step)) or start <= 0 or step <= 0 or stop < start:
            raise ValueError("Height range needs 0 < start <= stop and step > 0")
        count = math.floor((stop - start) / step + 1e-10) + 1
        if count > 100_000:
            raise ValueError("Too many initial heights (limit 100000)")
        return [round(start + i * step, 6) for i in range(count)]
    try:
        values = [float(x.strip()) for x in spec.split(",")]
    except ValueError as exc:
        raise ValueError("Heights must be comma-separated numeric meters") from exc
    if not values or any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("All heights must be positive finite meters")
    return values


def parse_time_offsets(spec: str) -> list[float]:
    """Comma-separated hour offsets (fractional hours supported, e.g. -0.5)."""
    try:
        values = [float(x.strip()) for x in spec.split(",")]
    except ValueError as exc:
        raise ValueError("Time offsets must be numeric hours, separated by commas") from exc
    if not values or any(not math.isfinite(v) for v in values):
        raise ValueError("Time offsets must be finite numbers")
    return values


def parse_xy_offsets(spec: str) -> list[tuple[float, float]]:
    """Semicolon-separated east,north kilometer offsets."""
    pairs = []
    try:
        for chunk in spec.split(";"):
            x, y = (float(s.strip()) for s in chunk.split(","))
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError
            pairs.append((x, y))
    except ValueError as exc:
        raise ValueError("Offsets must be 'east_km,north_km;east_km,north_km'") from exc
    if not pairs:
        raise ValueError("Specify at least one spatial offset")
    return pairs


def shifted_location(latitude: float, longitude: float, east_km: float, north_km: float) -> tuple[float, float]:
    """Small-offset spherical approximation; not suitable close to the poles."""
    lat = latitude + math.degrees(north_km / EARTH_RADIUS_KM)
    if abs(lat) >= 89.9:
        raise ValueError("Position offsets too near a pole")
    lon = longitude + math.degrees(east_km / (EARTH_RADIUS_KM * math.cos(math.radians(latitude))))
    if not -90 <= lat <= 90:
        raise ValueError("Position offset exceeds latitude bounds")
    lon = (lon + 180) % 360 - 180
    return lat, lon


def parse_jst(value: str) -> datetime:
    """Read observer-supplied times as JST by default; return UTC.

    Supported:
      YYYY/MM/DD HH:MM[:SS], YYYY-MM-DD HH:MM[:SS],
      YYYY.MM.DD HH:MM[:SS], YYYYMMDDHHMM[SS],
      ISO-8601 with T and optional Z/+09:00, and optional JST/UTC suffix.
    A timezone offset always overrides the JST default. Seconds are accepted,
    but HYSPLIT start times are minute-resolution, so nonzero seconds are
    rejected rather than silently rounded.
    """
    original = value.strip()
    if not original:
        raise ValueError("JST observation time is blank")
    explicit = None
    suffix = re.search(r"\s+(JST|UTC)$", original, flags=re.IGNORECASE)
    if suffix:
        explicit = JST if suffix.group(1).upper() == "JST" else UTC
        original = original[:suffix.start()].strip()
    compact = re.fullmatch(r"(\d{12}|\d{14})", original)
    try:
        if compact:
            dt = datetime.strptime(original, "%Y%m%d%H%M%S" if len(original) == 14
                                   else "%Y%m%d%H%M")
        else:
            value_iso = re.sub(
                r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})",
                lambda m: f"{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}",
                original,
            ).replace("Z", "+00:00")
            dt = datetime.fromisoformat(value_iso)
    except ValueError as exc:
        raise ValueError(
            f"Invalid observation time {value!r}; expected YYYY/MM/DD HH:MM[:SS] (JST)"
        ) from exc
    if dt.second or dt.microsecond:
        raise ValueError("Observation seconds must be 00 (HYSPLIT starts on whole minutes)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=explicit or JST)
    elif explicit is not None:
        raise ValueError("Specify either a UTC offset or JST/UTC suffix, not both")
    return dt.astimezone(UTC)


def _csv_header(row: list[str]) -> bool:
    """Detect a one-line header by its column types, not its literal labels."""
    if len(row) != 4:
        return False
    try:
        parse_jst(row[0])
        return False
    except ValueError:
        pass
    nonnumeric = 0
    for field in row[1:]:
        try:
            float(field)
        except ValueError:
            nonnumeric += 1
    return nonnumeric >= 2


def load_starts_csv(path: Path) -> list[StartPoint]:
    """Four columns in fixed order: time (JST), latitude, longitude, height m.

    Optional header of arbitrary column names; UTF-8 BOM or Windows CSV (CP932).
    Every row creates one base trajectory. No column-name matching required.
    """
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp932")
    result = []
    first_row = True
    for row_number, row in enumerate(csv.reader(text.splitlines()), start=1):
        if not row or all(not str(field).strip() for field in row):
            continue
        if first_row and _csv_header(row):
            first_row = False
            continue
        first_row = False
        if len(row) != 4:
            raise ValueError(
                f"CSV row {row_number}: expected four columns: time,latitude,longitude,height_m"
            )
        try:
            result.append(StartPoint(
                parse_jst(row[0]), float(row[1]), float(row[2]), float(row[3]),
                f"csv:{row_number}",
            ))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid CSV row {row_number}: {exc}") from exc
    if not result:
        raise ValueError("CSV has no observation rows")
    if len(result) > 100_000:
        raise ValueError("CSV has more than 100,000 observation rows")
    return result


def destination_location(latitude: float, longitude: float,
                         bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Destination at a great-circle distance, with bearing clockwise from north."""
    if not math.isfinite(distance_km) or distance_km < 0:
        raise ValueError("Shift distance must be nonnegative finite km")
    if distance_km == 0:
        return latitude, longitude
    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    angle = math.radians(bearing_deg)
    arc = distance_km / EARTH_RADIUS_KM
    lat2 = math.asin(max(-1.0, min(1.0,
        math.sin(lat1)*math.cos(arc) + math.cos(lat1)*math.sin(arc)*math.cos(angle))))
    lon2 = lon1 + math.atan2(math.sin(angle)*math.sin(arc)*math.cos(lat1),
                             math.cos(arc) - math.sin(lat1)*math.sin(lat2))
    return math.degrees(lat2), (math.degrees(lon2) + 180) % 360 - 180


def expand_observations(bases: Iterable[StartPoint], distance_km: float,
                        time_shift_minutes: float, limit: int = 100_000) -> list[StartPoint]:
    """Center + 8 compass bearings (equal radius); observation time ± minutes.

    Positive distance produces 9 locations. Positive time_shift_minutes
    produces 3 start times (-shift, 0, +shift), all resolved in UTC.
    """
    if not math.isfinite(distance_km) or distance_km < 0 or distance_km > 10000:
        raise ValueError("Horizontal distance must be within 0..10000 km")
    if (not math.isfinite(time_shift_minutes) or time_shift_minutes < 0
            or time_shift_minutes > 10080 or not float(time_shift_minutes).is_integer()):
        raise ValueError("Time shift must be a whole number of minutes (0..10080)")
    bases = list(bases)
    shifts = [0.0] if not time_shift_minutes else [
        -time_shift_minutes, 0.0, time_shift_minutes]
    bearings = [None] if distance_km == 0 else [
        None, 0, 45, 90, 135, 180, 225, 270, 315]
    if len(bases) * len(shifts) * len(bearings) > limit:
        raise ValueError(f"Too many trajectories (limit {limit})")
    points = []
    for point in bases:
        for delta in shifts:
            when = point.when + timedelta(minutes=delta)
            if not 1970 <= when.year <= 2099:
                raise ValueError("Supported simulation years: 1970..2099")
            for bearing in bearings:
                lat, lon = (point.latitude, point.longitude) if bearing is None else \
                    destination_location(point.latitude, point.longitude, bearing, distance_km)
                points.append(StartPoint(when, lat, lon, point.height_m, point.origin))
    return points


def fixed_starts(
    first: datetime, last: datetime, step_hours: float,
    latitude: float, longitude: float, heights: Iterable[float],
) -> list[StartPoint]:
    if step_hours <= 0 or not math.isfinite(step_hours):
        raise ValueError("Start-time interval must be positive")
    if last < first:
        raise ValueError("End time must be no earlier than start time")
    count = math.floor((last - first).total_seconds() / (step_hours * 3600) + 1e-10) + 1
    if count > 100_000:
        raise ValueError("Too many start times (limit 100000)")
    heights = list(heights)
    if count * len(heights) > 100_000:
        raise ValueError("Too many initial points (limit 100000)")
    return [StartPoint(first + timedelta(hours=i * step_hours), latitude, longitude, h)
            for i in range(count) for h in heights]


def expand_starts(
    bases: Iterable[StartPoint],
    xy_offsets: Iterable[tuple[float, float]],
    time_offsets_hours: Iterable[float],
    limit: int = 100_000,
) -> list[StartPoint]:
    bases, offsets, time_offsets = list(bases), list(xy_offsets), list(time_offsets_hours)
    if len(bases) * len(offsets) * len(time_offsets) > limit:
        raise ValueError(f"Too many trajectories (limit {limit}); reduce settings")
    points = []
    for point in bases:
        for dt_hours in time_offsets:
            when = point.when + timedelta(hours=dt_hours)
            if when.year < 1970 or when.year > 2099:
                raise ValueError("Supported simulation years: 1970-2099")
            for east, north in offsets:
                lat, lon = shifted_location(point.latitude, point.longitude, east, north)
                points.append(StartPoint(when, lat, lon, point.height_m, point.origin))
    return points


def group_starts(starts: Iterable[StartPoint], max_points: int = 100) -> list[RunGroup]:
    if not 1 <= max_points <= 10_000:
        raise ValueError("Maximum starts per HYSPLIT run must be 1..10000")
    grouped: dict[datetime, list[StartPoint]] = defaultdict(list)
    for start in starts:
        grouped[start.when].append(start)
    result = []
    ordinal = 0
    for when, points in sorted(grouped.items()):
        for i in range(0, len(points), max_points):
            result.append(RunGroup(when, tuple(points[i:i + max_points]), ordinal))
            ordinal += 1
    return result


def render_control(group: RunGroup, met_paths: list[Path], back_hours: int, model_top_m: float) -> str:
    if back_hours <= 0 or back_hours > 9999:
        raise ValueError("Backward duration must be 1..9999 hours")
    if not met_paths:
        raise ValueError("No ARL meteorological files selected")
    if model_top_m <= 0 or any(p.height_m >= model_top_m for p in group.points):
        raise ValueError("Model top must exceed every starting height")
    # Per HYSPLIT CONTROL spec, minutes can be included in start time.
    lines = [group.when.strftime("%y %m %d %H %M"), str(len(group.points))]
    lines.extend(f"{p.latitude:.6f} {p.longitude:.6f} {p.height_m:.2f}" for p in group.points)
    lines.extend([str(-back_hours), "0", f"{model_top_m:.1f}", str(len(met_paths))])
    for met in met_paths:
        met = Path(met).resolve()
        lines.extend([met.parent.as_posix().rstrip("/") + "/", met.name])
    lines.extend(["./", "tdump"])
    return "\n".join(lines) + "\n"


def write_group(group: RunGroup, run_root: Path, met_paths: list[Path], back_hours: int,
                model_top_m: float, height_reference: str = "AGL",
                bdyfiles_dir: Path | None = None) -> Path:
    if height_reference not in ("AGL", "MSL"):
        raise ValueError("Height reference must be AGL or MSL")
    folder = Path(run_root) / f"{group.when:%Y%m%dT%H%MZ}_{group.ordinal:05d}"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "CONTROL").write_text(render_control(group, met_paths, back_hours, model_top_m), encoding="ascii")
    # HYSPLIT reads SETUP.CFG in cwd. Default output height remains AGL (KAGL=1).
    (folder / "SETUP.CFG").write_text(
        "&SETUP\n KMSL = " + ("1" if height_reference == "MSL" else "0") + ",\n/\n", encoding="ascii"
    )
    # Per NOAA, each independent cwd needs its own ASCDATA.CFG to locate
    # the installed HYSPLIT boundary/terrain tables (critical for MSL input).
    if bdyfiles_dir is not None:
        bdy = Path(bdyfiles_dir).resolve()
        if not bdy.is_dir():
            raise FileNotFoundError(f"HYSPLIT bdyfiles folder not found: {bdy}")
        (folder / "ASCDATA.CFG").write_text(
            "-90.0 -180.0\n1.0 1.0\n180 360\n2\n0.2\n"
            + "'" + bdy.as_posix().rstrip("/") + "/'\n", encoding="ascii"
        )
    with (folder / "starts.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["trajectory_number", "time_utc", "latitude", "longitude", "height_m", "origin"])
        for i, p in enumerate(group.points, start=1):
            writer.writerow([i, p.when.isoformat(), p.latitude, p.longitude, p.height_m, p.origin])
    return folder


def execute_groups(executable: Path, folders: list[Path], workers: int = 2,
                   notify: Callable[[str], None] | None = None) -> list[tuple[Path, bool]]:
    require_native_windows()
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"HYSPLIT executable not found: {executable}")
    if not 1 <= workers <= 32:
        raise ValueError("Parallel workers must be 1..32")

    def run(folder: Path) -> tuple[Path, bool]:
        with (folder / "run.log").open("w", encoding="utf-8", errors="replace") as log:
            try:
                result = subprocess.run([str(executable)], cwd=folder, stdout=log,
                                        stderr=subprocess.STDOUT, check=False)
                dump = folder / "tdump"
                success = result.returncode == 0 and dump.is_file() and dump.stat().st_size > 0
                if not success:
                    log.write(f"\nHYSPLIT return code: {result.returncode}; tdump missing/empty?\n")
            except OSError as exc:
                log.write(f"\nFailed to start HYSPLIT: {exc}\n")
                success = False
        if notify:
            notify(f"{'OK' if success else 'FAILED'}: {folder.name} (see run.log and MESSAGE)")
        return folder, success

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, folders))
