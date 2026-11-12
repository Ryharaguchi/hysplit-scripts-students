"""Read HYSPLIT tdump files and create a lightweight GeoJSON + HTML map.

No plotting dependency, online tile services, Leaflet or CDN.
HTML and GeoJSON are usable entirely offline (e.g. in QGIS).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from .offline_plot import offline_html


def read_tdump(path: Path) -> dict[int, list[tuple[float, float, float, float]]]:
    """Return trajectory -> (latitude, longitude, altitude AGL, age hours).

    Based on NOAA HYSPLIT trajectory endpoint format: first five records
    describe grids/starts/diagnostics, following lines contain the endpoints.
    """
    path = Path(path)
    with path.open(encoding="ascii", errors="replace") as file:
        first = file.readline().split()
        if not first:
            raise ValueError(f"Empty trajectory file: {path}")
        try:
            nmet = int(first[0])
            if not 0 <= nmet <= 100:
                raise ValueError()
            for _ in range(nmet):
                if not file.readline():
                    raise ValueError("Incomplete meteorological header")
            record = file.readline().split()
            ntraj = int(record[0])
            if not 1 <= ntraj <= 100000:
                raise ValueError("Implausible trajectory count")
            for _ in range(ntraj):
                if not file.readline():
                    raise ValueError("Incomplete trajectory header")
            diag = file.readline().split()
            if not diag or not diag[0].isdigit():
                raise ValueError("Missing diagnostic header")
        except (IndexError, ValueError) as exc:
            raise ValueError(f"Invalid HYSPLIT tdump header: {path}") from exc

        trajectories = defaultdict(list)
        for line in file:
            fields = line.split()
            if len(fields) < 12:
                continue
            try:
                traj = int(fields[0])
                age = float(fields[8])
                latitude, longitude, altitude = map(float, fields[9:12])
            except ValueError:
                continue
            if (not 1 <= traj <= ntraj or not all(map(math.isfinite,
                    (latitude, longitude, altitude, age))) or not -90 <= latitude <= 90
                    or not -360 <= longitude <= 360):
                continue
            trajectories[traj].append((latitude, longitude, altitude, age))
    if not trajectories:
        raise ValueError(f"No valid endpoints in {path}")
    return dict(trajectories)


def create_map(run_root: Path, *, max_trajectories: int = 500,
               max_points_per_trajectory: int = 500) -> tuple[Path, Path, int, int]:
    """Write a viewable HTML map and its GeoJSON. Returns paths, shown, total.

    Sampling is explicit and reported: this is a visual preview, not a full
    statistical or scientific aggregation.
    """
    if max_trajectories < 1 or max_points_per_trajectory < 2:
        raise ValueError("Invalid display limits")
    run_root = Path(run_root)
    files = sorted(run_root.glob("*/tdump"))
    if not files:
        raise FileNotFoundError(f"No HYSPLIT tdump results below {run_root}")
    features = []
    total = 0
    for path in files:
        try:
            runs = read_tdump(path)
        except ValueError:
            continue
        for ident, points in sorted(runs.items()):
            total += 1
            if len(features) >= max_trajectories:
                continue
            # Keep the last endpoint and the age=0 endpoint even when thinning.
            stride = max(1, (len(points) - 1 + max_points_per_trajectory - 2)
                         // (max_points_per_trajectory - 1))
            sampled = points[::stride]
            if sampled[-1] != points[-1]:
                sampled.append(points[-1])
            if len(sampled) < 2:
                continue
            coordinates = [[lon, lat, height] for lat, lon, height, _age in sampled]
            features.append({
                "type": "Feature",
                "properties": {
                    "run": path.parent.name, "trajectory": ident,
                    "start_altitude_agl_m": points[0][2],
                    "end_age_hours": points[-1][3],
                },
                "geometry": {"type": "LineString", "coordinates": coordinates},
            })
    if not features:
        raise ValueError("No trajectories with at least two endpoints were found")
    geo = {"type": "FeatureCollection", "features": features}
    output = run_root / "map"
    output.mkdir(exist_ok=True)
    geo_path = output / "trajectories.geojson"
    geo_path.write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")

    # Offline SVG preview: no OpenStreetMap tiles, Leaflet, or CDN required.
    title = run_root.name
    note = (f"Showing {len(features):,} of {total:,} trajectories; "
            "other trajectories remain in original tdump files."
            if len(features) < total else f"Showing all {len(features):,} trajectories.")
    html_path = output / "index.html"
    html_path.write_text(offline_html(features, title, note), encoding="utf-8")
    return html_path, geo_path, len(features), total
