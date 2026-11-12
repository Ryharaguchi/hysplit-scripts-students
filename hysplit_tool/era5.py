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
PRESSURE_FIELDS = ["geopotential", "temperature", "u_component_of_wind",
                   "v_component_of_wind", "vertical_velocity", "relative_humidity"]
# Twelve pressure levels, matching the tested Windows grib2arl -k13 setup.
PRESSURE_LEVELS = ["1000", "925", "850", "700", "600", "500",
                   "400", "300", "250", "200", "150", "100"]
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
ARCO_PRESSURE_FIELDS = [key for key in PRESSURE_FIELDS if key != "relative_humidity"]
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


def download_cds(earliest: datetime, latest: datetime, target_dir: Path,
                 bbox: tuple[float, float, float, float],
                 notify: Callable[[str], None] | None = None) -> list[Path]:
    """Download upper-air and surface ERA5 as GRIB; needs CDS credentials/license."""
    try:
        import cdsapi  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ERA5 CDS requires: pip install -r requirements-era5.txt and ~/.cdsapirc") from exc
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    client = cdsapi.Client()
    result = []
    # Add a 1-day buffer, which also accommodates a number of boundary files.
    for (year, month), days in _month_days(earliest - timedelta(days=1), latest + timedelta(days=1)).items():
        for variant, dataset, fields in (
            ("pressure", "reanalysis-era5-pressure-levels", PRESSURE_FIELDS),
            ("single", "reanalysis-era5-single-levels", SURFACE_FIELDS),
        ):
            stamp = f"{min(days)}-{max(days)}"
            region_id = hashlib.sha256(repr(tuple(bbox)).encode("ascii")).hexdigest()[:8]
            target = target_dir / f"era5_{variant}_{year}{month:02d}_{stamp}_{region_id}.grib"
            if target.is_file() and target.stat().st_size > 0:
                result.append(target)
                continue
            request = {
                "product_type": ["reanalysis"],
                "variable": fields,
                "year": [str(year)],
                "month": [f"{month:02d}"],
                "day": sorted(days),
                "time": [f"{hour:02d}:00" for hour in range(24)],
                "data_format": "grib",
                "download_format": "unarchived",
                "area": list(bbox),  # north, west, south, east
                "grid": [0.25, 0.25],
            }
            if variant == "pressure":
                request["pressure_level"] = PRESSURE_LEVELS
            if notify:
                notify(f"ERA5 CDS: {dataset} {year}-{month:02d} ({len(days)} days)")
            temp = target.with_suffix(".part")
            try:
                client.retrieve(dataset, request, str(temp))
                if not temp.is_file() or temp.stat().st_size == 0:
                    raise RuntimeError(f"CDS did not create GRIB: {temp}")
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            result.append(target)
    return result


def download_google_arco(earliest: datetime, latest: datetime, target_dir: Path,
                         bbox: tuple[float, float, float, float],
                         notify: Callable[[str], None] | None = None) -> Path:
    """Export an ARL-convertible ARCO 0.25-degree NetCDF regional subset.

    Zarr chunks can be whole global fields despite small-area selection;
    downloads may be substantial. This adapter requires xarray/zarr/gcsfs/netCDF.
    """
    try:
        import xarray as xr  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Google ARCO requires optional xarray, gcsfs, zarr, netCDF4 dependencies") from exc
    north, west, south, east = bbox
    if north <= south or west >= east:
        raise ValueError("Google ARCO needs bounding box with north>south and west<east; no dateline crossing")
    if (latest - earliest).total_seconds() > 14 * 86400:
        raise ValueError("Google ARCO export capped at 14 days per request; split into smaller jobs")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if notify:
        notify("Opening Google Cloud ARCO ERA5: a tiny spatial crop may still read very large Zarr chunks")
    # Google's documented anonymous ARCO store.
    ds = xr.open_zarr(ARCO_URL, storage_options={"token": "anon"}, chunks=None)
    # Dataset longitudes may be 0..360; use coordinate masks to handle 0-degree crossings.
    lon = ds.longitude
    if float(lon.min()) >= 0:
        left, right = west % 360, east % 360
        select_lon = (lon >= left) & (lon <= right) if left <= right else (lon >= left) | (lon <= right)
    else:
        select_lon = (lon >= west) & (lon <= east)
    # ERA5 is hourly. Include both neighboring hours for later temporal interpolation.
    earlier = earliest.replace(minute=0, second=0, microsecond=0)
    later = latest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    selection = ds.sel(time=slice(earlier.replace(tzinfo=None), later.replace(tzinfo=None)))
    selection = selection.sel(latitude=selection.latitude.where(
        (selection.latitude >= south) & (selection.latitude <= north), drop=True))
    selection = selection.sel(longitude=selection.longitude.where(select_lon, drop=True))
    if not selection.sizes.get("time") or not selection.sizes.get("latitude") or not selection.sizes.get("longitude"):
        raise ValueError("Selected ERA5 window is empty (check dates and region)")
    required = ARCO_PRESSURE_FIELDS + ARCO_SURFACE_FIELDS
    missing = sorted(set(required) - set(selection.data_vars))
    if missing:
        raise ValueError(f"ARCO dataset lacks ARL conversion variables: {missing}")
    # Use direct relative humidity if supplied; otherwise retain specific
    # humidity and convert it in arco_convert.py after selecting each p level.
    humidity_name = next(
        (name for name in ARCO_HUMIDITY_ALTERNATIVES if name in selection.data_vars),
        None,
    )
    if humidity_name is None:
        raise ValueError(
            "ARCO humidity unavailable: expected specific_humidity (kg/kg) "
            "or relative_humidity (%) on pressure levels"
        )
    required = required + [humidity_name]
    if humidity_name == "specific_humidity" and notify:
        notify("ARCO provides specific_humidity: relative_humidity will be "
               "derived from q, temperature and pressure during GRIB1 conversion")
    if "level" not in selection.coords:
        raise ValueError("ARCO pressure-level coordinate missing")
    actual = set(int(x) for x in selection.level.values)
    absent = sorted(set(ARCO_PRESSURE_LEVELS) - actual)
    if absent:
        raise ValueError(f"ARCO missing pressure levels: {absent}")
    selected = selection[required].sel(level=ARCO_PRESSURE_LEVELS)
    region_id = hashlib.sha256(repr(tuple(bbox)).encode("ascii")).hexdigest()[:8]
    result = target_dir / f"arco_era5_{earliest:%Y%m%dT%H%M}_{latest:%Y%m%dT%H%M}_{region_id}.nc"
    if result.is_file() and result.stat().st_size:
        ds.close()
        if notify:
            notify(f"Reusing ARCO NetCDF cache: {result.name}")
        return result
    temp = result.with_suffix(".part")
    try:
        if notify:
            notify("Exporting 0.25-degree Google ARCO pressure/surface NetCDF for native ARL conversion")
        selected.to_netcdf(temp, engine="netcdf4")
        temp.replace(result)
    finally:
        temp.unlink(missing_ok=True)
        ds.close()
    return result
