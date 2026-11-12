"""Offline lat/lon SVG plot for HYSPLIT trajectories.

A self-contained HTML document. No JavaScript, CDN, tile server or network
access. Not intended for measuring geodesic distances.
"""
from __future__ import annotations

import html
import math

_PALETTE = ("#1768aa", "#d16b2d", "#18836a", "#794dbc", "#bd3d66", "#7a8318")


def _unwrap(longitudes: list[float], reference: float) -> list[float]:
    result = []
    previous = reference
    for lon in longitudes:
        value = lon + 360 * round((previous - lon) / 360)
        result.append(value)
        previous = value
    return result


def _lon_label(value: float) -> str:
    normalized = (value + 180) % 360 - 180
    return f"{abs(normalized):.2f}°{'E' if normalized >= 0 else 'W'}"


def _lat_label(value: float) -> str:
    return f"{abs(value):.2f}°{'N' if value >= 0 else 'S'}"


def offline_html(features: list[dict], title: str, note: str) -> str:
    if not features:
        raise ValueError("No trajectories to plot")
    first_lon = float(features[0]["geometry"]["coordinates"][0][0])
    series = []
    all_latitudes = []
    for f in features:
        pts = f["geometry"]["coordinates"]
        lons = _unwrap([float(p[0]) for p in pts], first_lon)
        lats = [float(p[1]) for p in pts]
        series.append((f, lons, lats))
        all_latitudes.extend(lats)
    coslat = max(0.05, math.cos(math.radians((min(all_latitudes) + max(all_latitudes))/2)))
    xs = [lon * coslat for _, lons, _ in series for lon in lons]
    xm = (min(xs) + max(xs)) / 2
    ym = (min(all_latitudes) + max(all_latitudes)) / 2
    dx = max(max(xs) - min(xs), coslat * 0.1)
    dy = max(max(all_latitudes) - min(all_latitudes), 0.1)
    width, height = 860.0, 480.0
    left, top = 104.0, 40.0
    pixels = min(width/(dx*1.16), height/(dy*1.16))

    def position(lon: float, lat: float) -> tuple[float, float]:
        return (left + width/2 + (lon*coslat - xm)*pixels,
                top + height/2 - (lat - ym)*pixels)

    lines = [f'<rect x="{left}" y="{top}" width="{width}" height="{height}" '
             f'fill="#fcfdff" stroke="#a6bbcb"/>']
    for i in range(5):
        projected_lon = xm + (i-2) * width / (4*pixels)
        x, _ = position(projected_lon/coslat, ym)
        lines.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top+height}" '
                     'stroke="#d7e3ef" stroke-dasharray="3 5"/>')
        lines.append(f'<text x="{x:.1f}" y="{top+height+25}" font-size="12" '
                     f'text-anchor="middle">{_lon_label(projected_lon/coslat)}</text>')
        lat = ym + (2-i)*height/(4*pixels)
        _, y = position(xm/coslat, lat)
        lines.append(f'<line x1="{left}" x2="{left+width}" y1="{y:.1f}" y2="{y:.1f}" '
                     'stroke="#d7e3ef" stroke-dasharray="3 5"/>')
        lines.append(f'<text x="{left-10}" y="{y+4:.1f}" font-size="12" '
                     f'text-anchor="end">{_lat_label(lat)}</text>')
    lines.append('<g clip-path="url(#plot-area)">')
    for index, (feature, lons, lats) in enumerate(series):
        color = _PALETTE[index % len(_PALETTE)]
        points = [position(lo, la) for lo, la in zip(lons, lats)]
        route = " ".join(("M" if k == 0 else "L") + f"{x:.2f},{y:.2f}"
                         for k, (x, y) in enumerate(points))
        props = feature.get("properties", {})
        tooltip = html.escape(
            f'{props.get("run", "")} / trajectory {props.get("trajectory", index+1)} '
            f'/ start altitude {props.get("start_altitude_agl_m", "?")} m AGL',
            quote=True)
        lines.append(f'<path d="{route}" fill="none" stroke="{color}" '
                     f'stroke-width="1.8" opacity="0.8"><title>{tooltip}</title></path>')
        x0, y0 = points[0]
        lines.append(f'<circle cx="{x0:.2f}" cy="{y0:.2f}" r="3" fill="{color}">'
                     f'<title>{tooltip} (start)</title></circle>')
    lines.append("</g>")
    svg = "\n".join(lines)
    title = html.escape(title, quote=True)
    note = html.escape(note, quote=True)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HYSPLIT offline plot: {title}</title>
<style>
body {{font:14px system-ui,"Yu Gothic",sans-serif;background:#f4f7fa;color:#173047;margin:0}}
header {{background:white;padding:14px 20px;border-bottom:1px solid #dae2ea}}
h1 {{font-size:17px;margin:0 0 7px}}
p {{margin:4px 0;color:#536578}}
main {{max-width:1130px;margin:15px auto;padding:0 15px}}
.paper {{background:white;border:1px solid #d3e0eb;border-radius:8px;overflow:auto}}
svg {{display:block;max-width:100%;height:auto;margin:0 auto}}
small {{display:block;color:#516274;padding:12px 2px;line-height:1.6}}
</style></head><body><header>
<h1>HYSPLIT後方流跡線 — {title}</h1><p>{note}</p></header>
<main><div class="paper">
<svg xmlns="http://www.w3.org/2000/svg" width="1050" height="590"
 viewBox="0 0 1050 590" aria-label="Offline HYSPLIT trajectory map">
<defs><clipPath id="plot-area">
<rect x="{left}" y="{top}" width="{width}" height="{height}"/>
</clipPath></defs>
{svg}
<text x="104" y="570" font-size="13">● 開始点　― 後方流跡線　（{len(features)}本表示）</text>
</svg></div>
<small>この図は完全オフラインです。OpenStreetMap・Leaflet・CDNへの通信は発生しません。
海岸線、国境、地形は表示しません。緯度経度の簡易投影図のため、距離の測定には使用しないでください。
詳細な地理情報の解析は同じフォルダのtrajectories.geojsonをQGIS等で開いてください。</small>
</main></body></html>
""