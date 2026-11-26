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


def _validate_arco_netcdf_cache(
    path: Path, *, earliest: datetime, latest: datetime, required_fields: list[str],
    global_area: bool, northern_area: bool,
) -> str:
    """Validate a completed ARCO cache and return its available humidity field."""
    import numpy as np
    import xarray as xr  # type: ignore

    with xr.open_dataset(path, engine="netcdf4") as cached:
        if not all(cached.sizes.get(c, 0) for c in ("time", "latitude", "longitude")):
            raise ValueError("No valid time/latitude/longitude in cached NetCDF")
        humidity_name = next(
            (name for name in ARCO_HUMIDITY_ALTERNATIVES if name in cached.data_vars), None
        )
        if humidity_name is None:
            raise ValueError("Missing humidity in cached NetCDF")
        expected = set(required_fields + [humidity_name])
        if not expected.issubset(cached.data_vars):
            raise ValueError("Missing required ERA5 fields in cached NetCDF")
        available_levels = set(int(x) for x in cached["level"].values)
        if not set(ARCO_PRESSURE_LEVELS).issubset(available_levels):
            missing_levels = sorted(set(ARCO_PRESSURE_LEVELS) - available_levels)
            raise ValueError(f"ERA5 cached data are missing pressure levels: {missing_levels}")
        earlier = earliest.replace(minute=0, second=0, microsecond=0)
        later = latest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        expected_count = int((later - earlier).total_seconds() // 3600) + 1
        actual_times = np.asarray(cached["time"].values).astype("datetime64[s]")
        wanted_times = np.asarray([
            np.datetime64((earlier + timedelta(hours=i)).replace(tzinfo=None), "s")
            for i in range(expected_count)
        ])
        if not set(wanted_times.tolist()).issubset(set(actual_times.tolist())):
            raise ValueError("Cached NetCDF does not contain every requested hourly record")
        if global_area or northern_area:
            lat = np.asarray(cached["latitude"].values)
            lon = np.asarray(cached["longitude"].values)
            wanted_south = -90.0 if global_area else 0.0
            if (not np.isclose(float(np.min(lat)), wanted_south)
                    and not (northern_area and np.min(lat) < 0)):
                raise ValueError("ERA5 cached latitude range does not cover the requested domain")
            if (not np.isclose(float(np.max(lat)), 90.0)
                    or not np.isclose(float(lon.max() - lon.min()), 359.75)):
                raise ValueError("ERA5 cached domain grid does not match request")
            if not np.allclose(np.abs(np.diff(np.sort(lat))), 0.25, atol=1e-5, rtol=0):
                raise ValueError("ERA5 cached latitude resolution differs from 0.25 degrees")
            if not np.allclose(np.diff(np.sort(lon)), 0.25, atol=1e-5, rtol=0):
                raise ValueError("ERA5 cached longitude resolution differs from 0.25 degrees")
    return humidity_name

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
                 field_profile: str = "allvars") -> list[Path]:
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
    for (year, month), selection in _month_day_hours(earliest, latest).items():
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


def download_google_arco(earliest: datetime, latest: datetime, target_dir: Path,
                         bbox: tuple[float, float, float, float],
                         notify: Callable[[str], None] | None = None,
                         progress: Callable[[int, int], None] | None = None,
                         field_profile: str = "allvars") -> Path:
    """Export ARCO 0.25-degree NetCDF in the requested horizontal domain.

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
    if field_profile not in ("hysplit", "allvars"):
        raise ValueError("ARCO field_profile must be 'hysplit' or 'allvars'")
    global_area = is_global_domain(bbox)
    northern_area = is_northern_hemisphere_domain(bbox)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    region_base = (
        "global37" if global_area else "northern37" if northern_area
        else hashlib.sha256(repr(tuple(bbox)).encode("ascii")).hexdigest()[:8]
    )
    legacy_region_id = (
        f"{region_base}_allvars" if global_area or northern_area else region_base
    )
    region_id = (
        legacy_region_id if field_profile == "allvars"
        else f"{region_base}_hysplit"
    )
    result = target_dir / f"arco_era5_{earliest:%Y%m%dT%H%M}_{latest:%Y%m%dT%H%M}_{region_id}.nc"
    legacy_allvars = target_dir / (
        f"arco_era5_{earliest:%Y%m%dT%H%M}_{latest:%Y%m%dT%H%M}_"
        f"{legacy_region_id}.nc"
    )
    legacy_partial = legacy_allvars.with_suffix(".part")
    legacy_checkpoint = legacy_partial.with_name(legacy_partial.name + ".json")
    hysplit_partial = result.with_suffix(".part")
    hysplit_checkpoint = hysplit_partial.with_name(hysplit_partial.name + ".json")
    hysplit_migration_stage = hysplit_partial.with_name(
        hysplit_partial.name + ".migrating"
    )
    hysplit_migration_checkpoint = hysplit_checkpoint.with_name(
        hysplit_checkpoint.name + ".migrating"
    )
    completed_hysplit_partials = [
        legacy_partial, legacy_checkpoint,
        hysplit_partial, hysplit_checkpoint,
        hysplit_migration_stage, hysplit_migration_checkpoint,
    ]
    base_required_fields = ARCO_PRESSURE_FIELDS + ARCO_SURFACE_FIELDS
    # Check the local cache *before* opening multi-petabyte remote Zarr.
    # Opening the Zarr metadata can be slow or fail on another PC even when
    # the entire requested NetCDF is already present on disk.
    if result.is_file() and result.stat().st_size > 0:
        try:
            _validate_arco_netcdf_cache(
                result, earliest=earliest, latest=latest,
                required_fields=base_required_fields,
                global_area=global_area, northern_area=northern_area,
            )
            if field_profile == "hysplit":
                _cleanup_partial_files(
                    completed_hysplit_partials, notify,
                    "after verified HYSPLIT NetCDF cache",
                )
            if notify:
                notify(f"Reusing verified local ARCO NetCDF without network: {result.name}")
            return result
        except (OSError, ValueError, ImportError, KeyError) as exc:
            # Never delete a pre-existing scientific archive, even when invalid.
            raise RuntimeError(
                f"Existing ERA5 NetCDF cache cannot be reused: {result}. "
                f"File was preserved; inspect it or choose a new output folder. {exc}"
            ) from exc
    # Reuse a full all-variable archive locally when HYSPLIT-only fields were
    # requested. This avoids downloading the same Zarr chunks again.
    if (field_profile == "hysplit" and legacy_allvars.is_file()
            and legacy_allvars.stat().st_size > 0
            and not hysplit_partial.exists() and not hysplit_checkpoint.exists()):
        try:
            humidity_name = _validate_arco_netcdf_cache(
                legacy_allvars, earliest=earliest, latest=latest,
                required_fields=base_required_fields,
                global_area=global_area, northern_area=northern_area,
            )
            if notify:
                notify(
                    f"Deriving HYSPLIT-only NetCDF locally from verified all-variable "
                    f"cache without network: {legacy_allvars.name}"
                )
            from .arco_stream import export_hourly_netcdf
            selected_fields = base_required_fields + [humidity_name]
            with xr.open_dataset(legacy_allvars, engine="netcdf4") as cached:
                earlier = earliest.replace(minute=0, second=0, microsecond=0)
                later = latest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                local_selection = cached[selected_fields].sel(
                    time=slice(earlier.replace(tzinfo=None), later.replace(tzinfo=None))
                ).sel(level=ARCO_PRESSURE_LEVELS)
                local_selection = _subset_arco_domain(local_selection, bbox)
                exported = export_hourly_netcdf(
                    local_selection, result,
                    source=f"{ARCO_URL}|local-allvars-cache:{legacy_allvars.resolve()}",
                    notify=notify, progress=progress,
                )
            _cleanup_partial_files(
                completed_hysplit_partials, notify,
                "after HYSPLIT NetCDF was derived from the all-variable cache",
            )
            return exported
        except (OSError, ValueError, ImportError, KeyError) as exc:
            raise RuntimeError(
                f"The existing all-variable ERA5 cache cannot supply the requested "
                f"HYSPLIT fields: {legacy_allvars}. The archive was preserved and "
                f"no replacement download was started. {exc}"
            ) from exc
    if notify:
        notify(
            "Opening Google Cloud ARCO ERA5. The published pressure-field "
            "Zarr chunks are approximately 154 MB per variable and hour "
            "before geographic cropping; a short request can still read many GB."
        )
    # Google's documented anonymous ARCO store.
    try:
        ds = xr.open_zarr(ARCO_URL, storage_options={"token": "anon"}, chunks=None)
    except Exception as exc:
        raise RuntimeError(
            "Cannot open public Google ARCO ERA5 Zarr. Check internet access, "
            "HTTPS proxy/firewall, SSL certificates, and available disk space. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc
    # Interpolate hourly: preserve a neighboring hour at either edge.
    earlier = earliest.replace(minute=0, second=0, microsecond=0)
    later = latest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    selection = ds.sel(
        time=slice(earlier.replace(tzinfo=None), later.replace(tzinfo=None))
    )
    # The northern hemisphere retains every longitude; a local superset cache
    # follows the same selection without fetching Zarr again.
    selection = _subset_arco_domain(selection, bbox)
    # Global: neither axis cropped. Northern: latitude cropped, longitude intact.
    if not selection.sizes.get("time") or not selection.sizes.get("latitude") or not selection.sizes.get("longitude"):
        raise ValueError("Selected ERA5 window is empty (check dates and region)")
    required = list(base_required_fields)
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
    # Keep all available fields for the archival profile. HYSPLIT mode selects
    # only the required meteorology before any remote array values are read.
    selected = selection.sel(level=ARCO_PRESSURE_LEVELS)
    if field_profile == "hysplit":
        selected = selection[required].sel(level=ARCO_PRESSURE_LEVELS)
    from .arco_stream import export_hourly_netcdf
    pressure_variables = sum("level" in v.dims for v in selected.data_vars.values())
    surface_variables = sum("level" not in v.dims for v in selected.data_vars.values())
    hour_count = int(selected.sizes["time"])
    estimated_raw_gib = (hour_count * pressure_variables * 154 / 1024)
    if notify:
        # xarray DataArray.nbytes uses shape/dtype metadata and does NOT read
        # remote grid values. The actual NetCDF4 size is compression-dependent.
        selected_gib = sum(item.nbytes for item in selected.data_vars.values()) / 1024**3
        from shutil import disk_usage
        free_gib = disk_usage(target_dir).free / 1024**3
        scope_label = "all ARCO fields" if field_profile == "allvars" else "HYSPLIT-required fields"
        notify(
            f"Google ARCO: {hour_count} hours, {len(selected.level)} pressure "
            f"levels, {pressure_variables} pressure variables and "
            f"{surface_variables} surface variables ({scope_label}); "
            f"pressure-chunk uncompressed workload ≈ {estimated_raw_gib:.1f} GiB "
            "(not actual compressed transfer/storage)."
        )
        notify(
            f"ERA5 selected NetCDF data uncompressed size ≈ {selected_gib:.1f} GiB; "
            f"free space on output drive ≈ {free_gib:.1f} GiB. "
            "Final NetCDF and GRIB/ARL sizes depend on compression. "
            f"Selected profile: {field_profile}."
        )
        notify(
            "Exporting ERA5 to NetCDF hour by hour; progress and "
            "crash-safe resume checkpoints enabled. First hour may be slow."
        )
    try:
        exported = export_hourly_netcdf(
            selected, result, source=ARCO_URL, notify=notify, progress=progress,
            migrate_from=(legacy_partial, legacy_checkpoint)
            if field_profile == "hysplit" else None,
        )
        if field_profile == "hysplit":
            _cleanup_partial_files(
                completed_hysplit_partials, notify,
                "after HYSPLIT NetCDF export completed",
            )
        return exported
    finally:
        ds.close()
