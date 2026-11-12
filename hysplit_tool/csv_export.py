"""Consolidate a complete Windows HYSPLIT batch into one scientific CSV.

Every endpoint of every successful trajectory is written to trajectories.csv.
The source CSV, per-group HYSPLIT tdump and run logs remain for provenance.

Height conventions: SETUP.CFG sets KAGL=1 (trajectory tdump altitude AGL)
and TM_TERR=1 (TERR_MSL, the terrain elevation diagnostic). MSL height is
estimated from AGL + TERR_MSL. Geopotential height is *approximated* from the
MSL height assuming a spherical Earth and inverse-square gravity. These are
not the geopotential values sampled from ERA5's pressure-level analysis.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .met_diagnostics import MeteorologicalVariable, selected_variables

UTC = timezone.utc
JST = timezone(timedelta(hours=9), "JST")
EARTH_RADIUS_M = 6_371_008.8

OUTPUT_COLUMNS = (
    "run_group", "trajectory_number", "source_csv_row",
    "start_time_jst", "start_time_utc",
    "start_latitude_deg", "start_longitude_deg",
    "start_height_m", "start_height_reference",
    "endpoint_time_jst", "endpoint_time_utc", "age_hours",
    "latitude_deg", "longitude_deg", "pressure_hpa",
    "terrain_elevation_msl_m", "trajectory_height_agl_m",
    "trajectory_height_msl_m_est", "trajectory_geopotential_height_m_est",
)


@dataclass(frozen=True)
class ExportResult:
    path: Path
    trajectories: int
    endpoints: int
    missing_terrain_endpoints: int
    groups: int


def _year(value: int) -> int:
    return value if value >= 100 else (2000 + value if value < 70 else 1900 + value)


def _plain_number(value: float | None, decimals: int) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{decimals}f}"


def _valid(value: str) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= -9000:
        return None
    return v


def _geopotential_height_estimate(geometric_msl_m: float | None) -> float | None:
    if geometric_msl_m is None:
        return None
    denominator = EARTH_RADIUS_M + geometric_msl_m
    if denominator <= 0:
        return None
    return EARTH_RADIUS_M * geometric_msl_m / denominator


def _load_starts(group: Path) -> dict[int, dict[str, str]]:
    path = group / "starts.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        starts = {}
        for line in csv.DictReader(handle):
            num = int(line["trajectory_number"])
            if num in starts:
                raise ValueError(f"Duplicate HYSPLIT start number {num} in {path}")
            starts[num] = line
    if not starts:
        raise ValueError(f"Missing start metadata in {path}")
    return starts


def _tdump_rows(path: Path, starts: dict[int, dict[str, str]],
                height_reference: str, group_id: str,
                requested: tuple[MeteorologicalVariable, ...] = ()):
    """Yield endpoint rows with diagnostic names read from tdump record #5."""
    with path.open("r", encoding="ascii", errors="replace") as handle:
        header = handle.readline().split()
        if not header:
            raise ValueError(f"Empty HYSPLIT output: {path}")
        try:
            nmet = int(header[0])
            if not 0 <= nmet <= 100:
                raise ValueError("Invalid meteorological grid count")
            for _ in range(nmet):
                if not handle.readline():
                    raise ValueError("Truncated meteorology header")
            record3 = handle.readline().split()
            ntrajs = int(record3[0])
            if not 1 <= ntrajs <= 100_000:
                raise ValueError("Invalid trajectory count")
            for _ in range(ntrajs):
                if not handle.readline():
                    raise ValueError("Truncated start header")
            record5 = handle.readline().split()
            nvars = int(record5[0])
            labels = [name.strip().upper() for name in record5[1:]]
            if not 0 <= nvars <= 100 or len(labels) != nvars:
                raise ValueError("Invalid meteorological diagnostic header")
            if height_reference not in ("AGL", "MSL"):
                raise ValueError("Unknown source altitude convention")
            # NOAA defines output order, not the GUI. Match record #5 labels
            # by name and reject absent requested fields instead of creating
            # falsely valid empty columns.
            missing = [item.tdump_label for item in requested
                       if item.tdump_label not in labels]
            if missing:
                raise ValueError(
                    f"Requested meteorological output missing from tdump {path}: {missing}"
                )
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid HYSPLIT tdump header: {path}: {exc}") from exc

        for line_number, line in enumerate(handle, start=nmet+ntrajs+4):
            pieces = line.split()
            if len(pieces) < 12+nvars:
                continue
            try:
                idx = int(pieces[0])
                if idx not in starts:
                    raise ValueError("Unknown trajectory index")
                year, month, day, hour, minute = map(int, pieces[2:7])
                when = datetime(_year(year), month, day, hour, minute, tzinfo=UTC)
                age = float(pieces[8])
                lat, lon, alt_agl = (float(v) for v in pieces[9:12])
                if (not all(math.isfinite(v) for v in (age, lat, lon, alt_agl))
                        or not -90 <= lat <= 90 or not -360 <= lon <= 360):
                    raise ValueError("Invalid endpoint coordinates or height")
            except (ValueError, KeyError) as exc:
                raise ValueError(f"Invalid HYSPLIT endpoint at {path}:{line_number}: {exc}") from exc

            diagnostics = {key: _valid(value)
                           for key, value in zip(labels, pieces[12:12+nvars])}
            ground = diagnostics.get("TERR_MSL")
            if ground is None:
                ground = diagnostics.get("TERRAIN")
            msl = alt_agl + ground if ground is not None else None
            geo_est = _geopotential_height_estimate(msl)
            start = starts[idx]
            start_utc = datetime.fromisoformat(start["time_utc"].replace("Z", "+00:00"))
            if start_utc.tzinfo is None:
                raise ValueError(f"Start metadata has a naive timestamp: {group_id}/{idx}")
            start_utc = start_utc.astimezone(UTC)
            # TFMT=2 limits tdump's age (hours) to two decimal places.
            # Reconstruct age from actual UTC minute timestamps so 1-, 5-
            # and 10-minute endpoint intervals remain numerically faithful.
            precise_age_hours = (when - start_utc).total_seconds() / 3600.0
            if abs(precise_age_hours - age) > 0.03:
                raise ValueError(
                    f"Inconsistent trajectory endpoint age in {path}: "
                    f"tdump {age} h vs UTC timestamps {precise_age_hours:.6f} h"
                )
            origin = start.get("origin", "")
            source_row = origin.partition(":")[2] if origin.startswith("csv:") else ""
            row = {
                "run_group": group_id,
                "trajectory_number": idx,
                "source_csv_row": source_row,
                "start_time_jst": start_utc.astimezone(JST).isoformat(timespec="minutes"),
                "start_time_utc": start_utc.isoformat(timespec="minutes"),
                "start_latitude_deg": _plain_number(_valid(start["latitude"]), 6),
                "start_longitude_deg": _plain_number(_valid(start["longitude"]), 6),
                "start_height_m": _plain_number(_valid(start["height_m"]), 2),
                "start_height_reference": height_reference,
                "endpoint_time_jst": when.astimezone(JST).isoformat(timespec="minutes"),
                "endpoint_time_utc": when.isoformat(timespec="minutes"),
                "age_hours": _plain_number(precise_age_hours, 6),
                "latitude_deg": _plain_number(lat, 6),
                "longitude_deg": _plain_number(lon, 6),
                "pressure_hpa": _plain_number(diagnostics.get("PRESSURE"), 2),
                "terrain_elevation_msl_m": _plain_number(ground, 2),
                "trajectory_height_agl_m": _plain_number(alt_agl, 2),
                "trajectory_height_msl_m_est": _plain_number(msl, 2),
                "trajectory_geopotential_height_m_est": _plain_number(geo_est, 2),
            }
            for variable in requested:
                row[variable.csv_column] = _plain_number(
                    diagnostics.get(variable.tdump_label), 2
                )
            yield row


def export_run_csv(run_root: Path, *, height_reference: str = "MSL",
                   group_folders: list[Path] | None = None,
                   selected_diagnostics: Iterable[str] = ()) -> ExportResult:
    """Aggregate all tdump endpoints into one UTF-8 BOM CSV per batch.

    A missing required group or truncated tdump fails closed. The result
    CSV is written atomically so partial output cannot masquerade as a
    successful research calculation.
    """
    root = Path(run_root)
    requested = selected_variables(selected_diagnostics)
    column_names = OUTPUT_COLUMNS + tuple(
        item.csv_column for item in requested
    )
    groups = sorted(map(Path, group_folders)) if group_folders is not None else sorted(
        folder for folder in root.iterdir() if folder.is_dir() and (folder / "tdump").is_file())
    if not groups:
        raise ValueError(f"No HYSPLIT outputs to export: {root}")

    path = root / "trajectories.csv"
    partial = root / "trajectories.csv.part"
    partial.unlink(missing_ok=True)
    endpoints = 0
    missing = 0
    unique_tracks = set()
    try:
        with partial.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=column_names)
            writer.writeheader()
            for group in groups:
                starts = _load_starts(group)
                seen = set()
                dump = group / "tdump"
                if not dump.is_file():
                    raise FileNotFoundError(f"Missing tdump for HYSPLIT group: {group}")
                for item in _tdump_rows(
                    dump, starts, height_reference, group.name, requested
                ):
                    index = item["trajectory_number"]
                    seen.add(index)
                    writer.writerow(item)
                    endpoints += 1
                    unique_tracks.add((group.name, index))
                    if not item["terrain_elevation_msl_m"]:
                        missing += 1
                if not set(starts).issubset(seen):
                    raise ValueError(f"Missing trajectories in {dump}: {sorted(set(starts) - seen)}")
            if not endpoints:
                raise ValueError("No usable HYSPLIT trajectory endpoints")
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return ExportResult(path, len(unique_tracks), endpoints, missing, len(groups))
