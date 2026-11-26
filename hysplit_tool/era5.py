"""ERA5 acquisition adapters: CDS GRIB for native GRIB1 conversion, ARCO for NetCDF.

CDS supplies GRIB, Google ARCO supplies Zarr read into NetCDF. Before running
HYSPLIT an independent conversion into ARL (and verification) is required.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
from typing import Callable

ARCO_URL = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
# Complete horizontal ERA5 domain: no regional crop based on launch coordinates.
GLOBAL_ERA5_BBOX = (90.0, -180.0, -90.0, 180.0)
# Requested operating region: all longitudes, equator through North Pole.
# Keep the former global mode available to API callers, but not as GUI default.
NORTHERN_HEMISPHERE_BBOX = (90.0, -180.0, 0.0, 180.0)


def is_northern_hemisphere_domain(bbox: tuple[float, float, float, float]) -> bool:
    return tuple(float(x) for x in bbox) == NORTHERN_HEMISPHERE_BBOX


def is_full_longitude_domain(bbox: tuple[float, float, float, float]) -> bool:
    """The 0..360-degree ARCO longitude axis must remain entirely selected."""
    return abs((float(bbox[3]) - float(bbox[1])) - 360.0) < 1e-8


def is_global_domain(bbox: tuple[float, float, float, float]) -> bool:
    return tuple(float(x) for x in bbox) == GLOBAL_ERA5_BBOX


def _subset_arco_domain(dataset, bbox: tuple[float, float, float, float]):
    """Select a requested ERA5 grid from local or remote ARCO coordinates."""
    north, west, south, east = bbox
    selection = dataset
    if not is_global_domain(bbox):
        selection = selection.sel(latitude=selection.latitude.where(
            (selection.latitude >= south) & (selection.latitude <= north), drop=True,
        ))
        if not is_full_longitude_domain(bbox):
            longitude = selection.longitude
            if float(longitude.min()) >= 0:
                left, right = west % 360, east % 360
                selected_longitudes = (
                    (longitude >= left) & (longitude <= right)
                    if left <= right else (longitude >= left) | (longitude <= right)
                )
            else:
                selected_longitudes = (longitude >= west) & (longitude <= east)
            selection = selection.sel(longitude=longitude.where(selected_longitudes, drop=True))
    return selection


def _cleanup_partial_files(
    paths: list[Path], notify: Callable[[str], None] | None, reason: str,
) -> None:
    removed_bytes = 0
    removed_count = 0
    failures: list[str] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path in seen:
            continue
        seen.add(path)
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
            removed_bytes += size
            removed_count += 1
        except OSError as exc:
            failures.append(f"{path.name}: {exc}")
    if not notify:
        return
    if removed_count:
        try:
            notify(
                f"Obsolete ERA5 partial files removed {reason}: {removed_count} files, "
                f"{removed_bytes / (1024 ** 3):.2f} GiB freed"
            )
        except Exception:
            pass
    if failures:
        try:
            notify("ERA5 partial cleanup incomplete: " + "; ".join(failures))
        except Exception:
            pass


# All 16 pressure-level variables available from ERA5 CDS.
# Saved pressure GRIB is a scientific input archive; the HYSPLIT converter
# independently uses the fields it can process for its ARL file.
PRESSURE_FIELDS = [
    "potential_vorticity",
    "specific_rain_water_content",
    "specific_snow_water_content",
    "geopotential", "temperature", "u_component_of_wind",
    "v_component_of_wind", "specific_humidity", "vertical_velocity",
    "vorticity", "divergence", "relative_humidity",
    "ozone_mass_mixing_ratio", "specific_cloud_liquid_water_content",
    "specific_cloud_ice_water_content", "fraction_of_cloud_cover",
]
# Minimal fields required for ARL; never use this subset to crop raw archives.
HYSPLIT_PRESSURE_FIELDS = [
    "geopotential", "temperature", "u_component_of_wind",
    "v_component_of_wind", "vertical_velocity", "relative_humidity",
]
# All 37 official ERA5 pressure levels in hPa. No vertical thinning in
# acquisition/NetCDF archival or GRIB/ARL creation. See ECMWF ERA5 documentation.
PRESSURE_LEVELS = [
    "1000", "975", "950", "925", "900", "875", "850", "825", "800", "775",
    "750", "700", "650", "600", "550", "500", "450", "400", "350", "300",
    "250", "225", "200", "175", "150", "125", "100", "70", "50", "30",
    "20", "10", "7", "5", "3", "2", "1",
]
SURFACE_FIELDS = ["2m_temperature", "10m_u_component_of_wind",
                  "10m_v_component_of_wind", "surface_pressure",
                  "total_precipitation", "boundary_layer_height", "geopotential"]
# In the Google ARCO pressure/surface dataset, static surface geopotential has
# a distinct variable name from the three-dimensional pressure geopotential.
ARCO_SURFACE_FIELDS = [
    "2m_temperature", "10m_u_component_of_wind", "10m_v_component_of_wind",
    "surface_pressure", "total_precipitation", "boundary_layer_height",
    "geopotential_at_surface",
]
# ARCO does not necessarily contain pressure-level relative_humidity. It
# provides specific_humidity (kg/kg) in the 0.25-degree pressure-level archive.
# Keep the CDS request fields separate: CDS can supply relative_humidity itself.
ARCO_PRESSURE_FIELDS = [
    key for key in HYSPLIT_PRESSURE_FIELDS if key != "relative_humidity"
]
ARCO_HUMIDITY_ALTERNATIVES = ("relative_humidity", "specific_humidity")
ARCO_PRESSURE_LEVELS = [int(level) for level in PRESSURE_LEVELS]


def region_around(lat: float, lon: float, lat_radius: float = 25, lon_radius: float = 35):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid center coordinate")
    north, south = min(90, lat + lat_radius), max(-90, lat - lat_radius)
    west, east = max(-180, lon - lon_radius), min(180, lon + lon_radius)
    return north, west, south, east


def _month_days(earliest: datetime, latest: datetime):
    dates = defaultdict(set)
    day = earliest.date()
    while day <= latest.date():
        dates[(day.year, day.month)].add(f"{day.day:02d}")
        day += timedelta(days=1)
    return dict(sorted(dates.items()))


def _month_day_hours(earliest: datetime, latest: datetime):
    """CDS request days and hourly slots, including temporal interpolation.

    CDS accepts lists of days and times (cross-product). This avoids all 24
    hours for brief windows, without requesting unused days between launches.
    """
    if latest < earliest:
        raise ValueError("ERA5 request ends before it starts")
    slots: dict[tuple[int, int], dict[str, set[str]]] = defaultdict(
        lambda: {"days": set(), "hours": set()}
    )
    first = (earliest - timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    last = (latest + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    when = first
    while when <= last:
        item = slots[(when.year, when.month)]
        item["days"].add(f"{when.day:02d}")
        item["hours"].add(f"{when.hour:02d}:00")
        when += timedelta(hours=1)
    return dict(sorted(slots.items()))


def download_cds(earliest: datetime, latest: datetime, target_dir: Path,
                 bbox: tuple[float, float, float, float],
                 notify: Callable[[str], None] | None = None,
                 field_profile: str = "allvars", *, exact_hours: bool = False) -> list[Path]:
    """Download upper-air and surface ERA5 as GRIB; needs CDS credentials/license."""
    if field_profile not in ("hysplit", "allvars"):
        raise ValueError("ERA5 field_profile must be 'hysplit' or 'allvars'")
    try:
        import cdsapi  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ERA5 CDS requires: pip install -r requirements-era5.txt and ~/.cdsapirc") from exc
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    client = None
    result = []
    pressure_fields = (
        HYSPLIT_PRESSURE_FIELDS if field_profile == "hysplit" else PRESSURE_FIELDS
    )
    if exact_hours:
        if earliest.date() != latest.date() or earliest > latest or any(
                t.minute or t.second or t.microsecond for t in (earliest,latest)):
            raise ValueError("Exact CDS request must be a single UTC date at whole hours")
        slots={(earliest.year,earliest.month): {
            "days": {f"{earliest.day:02d}"},
            "hours": {f"{h:02d}:00" for h in range(earliest.hour,latest.hour+1)}}}
    else:
        slots=_month_day_hours(earliest,latest)
    for (year, month), selection in slots.items():
        days = sorted(selection["days"])
        hours = sorted(selection["hours"])
        for variant, dataset, fields in (
            ("pressure", "reanalysis-era5-pressure-levels", pressure_fields),
            ("single", "reanalysis-era5-single-levels", SURFACE_FIELDS),
        ):
            stamp = f"{min(days)}-{max(days)}"
            region_id = (
                "global" if is_global_domain(bbox)
                else "northern_hemisphere" if is_northern_hemisphere_domain(bbox)
                else hashlib.sha256(repr(tuple(bbox)).encode("ascii")).hexdigest()[:8]
            )
            # Dataset identities include all pressure levels and variable
            # selections. A cached old 12-level GRIB is NOT sufficient for
            # 37-level science, even if it covers all requested hours.
            times_key = hashlib.sha256(repr((
                tuple(days), tuple(hours),
                tuple(PRESSURE_LEVELS),
                tuple(pressure_fields),
                tuple(SURFACE_FIELDS),
            )).encode("ascii")).hexdigest()[:10]
            profile_suffix = "_hysplit" if field_profile == "hysplit" else ""
            target = target_dir / (
                f"era5_{variant}_{year}{month:02d}_{stamp}_{times_key}_{region_id}"
                f"{profile_suffix}.grib"
            )
            if target.is_file() and target.stat().st_size > 0:
                result.append(target)
                if notify:
                    notify(f"ERA5 CDS: reusing matching {field_profile} 37-level cache {target.name}")
                continue
            request = {
                "product_type": ["reanalysis"],
                "variable": fields,
                "year": [str(year)],
                "month": [f"{month:02d}"],
                "day": days,
                "time": hours,
                "data_format": "grib",
                "download_format": "unarchived",
                "grid": [0.25, 0.25],
            }
            if not is_global_domain(bbox):
                request["area"] = list(bbox)  # Backward compatible direct API.
            if variant == "pressure":
                request["pressure_level"] = PRESSURE_LEVELS
            if notify:
                notify(
                    f"ERA5 CDS: {dataset} {year}-{month:02d} "
                    f"({len(days)} days x {len(hours)} UTC hours)"
                )
            temp = target.with_suffix(".part")
            if temp.exists():
                raise RuntimeError(
                    f"Existing CDS partial file preserved at {temp}. "
                    "CDS cannot safely resume this file; inspect/move it before retry."
                )
            if client is None:
                # The portable Windows GUI has no console stdout/stderr.
                # cdsapi's default progress bar writes to stderr and can fail
                # with AttributeError when that stream is None.
                client = cdsapi.Client(progress=False)
            try:
                client.retrieve(dataset, request, str(temp))
                if not temp.is_file() or temp.stat().st_size == 0:
                    raise RuntimeError(f"CDS did not create a nonempty GRIB: {temp}")
                temp.replace(target)
            except Exception:
                # CDS downloads cannot resume a local partial file. Remove it
                # when this request ends so a failed transfer cannot fill the
                # work drive or block the next attempt.
                _cleanup_partial_files([temp], notify, "after CDS transfer ended")
                raise
            result.append(target)
    return result
