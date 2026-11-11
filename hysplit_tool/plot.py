"""Read HYSPLIT tdump files and create a lightweight GeoJSON + HTML map.

No Python plotting dependency. HTML uses Leaflet and OpenStreetMap online;
GeoJSON remains usable offline (e.g. in QGIS).
"""
from __future__ import annotations

import html
import json
import math
from collections import defaultdict
from pathlib import Path


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

    # Escape angle brackets in script-embedded JSON; never use untrusted data as HTML.
    inline_data = json.dumps(geo, ensure_ascii=False).replace("<", "\\u003c")
    title = html.escape(run_root.name, quote=True)
    note = (f"Showing {len(features):,} of {total:,} trajectories; "
            "the rest are omitted from this map and GeoJSON."
            if len(features) < total else f"Showing all {len(features):,} trajectories.")
    html_text = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HYSPLIT trajectories: __TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{font:14px system-ui, sans-serif;margin:0}header{padding:10px 14px}
#map{height:calc(100vh - 85px);min-height:300px}small{color:#666}</style></head>
<body><header><b>HYSPLIT backward trajectories</b> — __TITLE__<div>
<small>__NOTE__ Online access is required for map tiles and Leaflet; GeoJSON is saved beside this file.</small></div></header>
<div id="map"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const collection = __DATA__;
if (typeof L !== 'undefined') {
  const map = L.map('map').setView([35, 135], 4);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {attribution: '&copy; OpenStreetMap contributors', maxZoom: 18}).addTo(map);
  const colors = ['#1660a1','#d65c32','#237c58','#8a42a0','#aa8b18'];
  const all = L.featureGroup().addTo(map);
  collection.features.forEach((feature, index) => {
    const line = feature.geometry.coordinates.map(p => [p[1],p[0]]);
    const poly = L.polyline(line, {color: colors[index % colors.length], weight: 1.8,
      opacity: 0.7}).addTo(all);
    const props = feature.properties;
    const popup = document.createElement('div');
    popup.textContent = props.run + ' / track ' + props.trajectory +
      ' / start altitude ' + props.start_altitude_agl_m.toFixed(0) + ' m AGL';
    poly.bindPopup(popup);
  });
  if (all.getLayers().length) map.fitBounds(all.getBounds(), {padding: [20,20]});
}
</script></body></html>"""
    html_path = output / "index.html"
    html_path.write_text(html_text.replace("__TITLE__", title).replace("__NOTE__", html.escape(note))
                         .replace("__DATA__", inline_data), encoding="utf-8")
    return html_path, geo_path, len(features), total
