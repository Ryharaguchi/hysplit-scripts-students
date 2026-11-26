"""Windows-only Google ARCO ERA5 NetCDF -> GRIB1 -> HYSPLIT ARL.

ARCO's 0.25-degree pressure/surface dataset is NOT a generic CF-NetCDF input.
This decoder explicitly validates the required ECMWF parameters, 37 pressure
levels, hourly steps and regular 0.25-degree grid. It then encodes GRIB1 using
ECMWF ecCodes, invokes the already-installed native grib2arl.exe, and refuses
to publish ARL until NOAA filedates.exe confirms complete temporal coverage.

Important: successful ARL packaging does not prove meteorological accuracy or
that a regional grid contains the entire backward trajectory. The Zarr source
can require large transfers even for a small regional NetCDF subset. Completed
GRIB inputs are retained after a failed conversion so retry does not repeat the
NetCDF scan; they are removed only after the ARL has passed timestamp validation.
Validated ARLs remain as the reusable cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .era5 import PRESSURE_LEVELS as ERA5_PRESSURE_LEVELS
from .native_era5 import (
    _grib2arl_is_running,
    check_arco_conversion_space,
    require_grib1,
    run_native_grib1_pair,
)
from .platform_guard import require_native_windows
from .validation import check_coverage, check_signature, read_arl_times

UTC = timezone.utc
ARCO_CONVERSION_VERSION = "arco-nc-v4-rh-q-floor-no-cutoff"
ARCO_RH_POLICY_MARKER = "no magnitude or prevalence cutoff is applied"
# The 37 official ERA5 pressure levels are preserved in HYSPLIT GRIB1.
# Conversion uses a separate surface level: grib2arl -k38.
PRESSURE_LEVELS = tuple(int(p) for p in ERA5_PRESSURE_LEVELS)
PRESSURE_PARAMS = (
    ("geopotential", 129, "m2 s-2"),
    ("temperature", 130, "K"),
    ("u_component_of_wind", 131, "m s-1"),
    ("v_component_of_wind", 132, "m s-1"),
    ("vertical_velocity", 135, "Pa s-1"),
    ("relative_humidity", 157, "%"),
)
SURFACE_PARAMS = (
    ("2m_temperature", 167, "K"),
    ("10m_u_component_of_wind", 165, "m s-1"),
    ("10m_v_component_of_wind", 166, "m s-1"),
    ("surface_pressure", 134, "Pa"),
    ("total_precipitation", 228, "m"),
    ("boundary_layer_height", 159, "m"),
    ("geopotential_at_surface", 129, "m2 s-2"),
)


def estimate_grib1_pair_size_bytes(time_count: int, latitude_count: int,
                                   longitude_count: int) -> int:
    """Conservatively estimate ARCO GRIB1 bytes from grid and packed precision."""
    if min(time_count, latitude_count, longitude_count) < 1:
        raise ValueError("ARCO GRIB size estimate needs positive dimensions")
    point_count = latitude_count * longitude_count
    pressure_bits = sum(24 if param == 129 else 16 for _, param, _ in PRESSURE_PARAMS)
    surface_bits = sum(24 if param == 129 else 16 for _, param, _ in SURFACE_PARAMS)
    bits_per_hour = point_count * (
        len(PRESSURE_LEVELS) * pressure_bits + surface_bits
    )
    payload_bytes = math.ceil(time_count * bits_per_hour / 8)
    message_count = time_count * (
        len(PRESSURE_LEVELS) * len(PRESSURE_PARAMS) + len(SURFACE_PARAMS)
    )
    # Add 5% for packing/padding and 4 KiB per message for GRIB metadata.
    return math.ceil(payload_bytes * 1.05) + message_count * 4096


def _cleanup_intermediates(
    paths: list[Path], notify: Callable[[str], None] | None, outcome: str,
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
        message = (
            f"ARCO conversion intermediates removed {outcome}: {removed_count} files, "
            f"{removed_bytes / (1024 ** 3):.2f} GiB freed"
        )
        try:
            notify(message)
        except Exception:
            pass
    if failures:
        try:
            notify("ARCO intermediate cleanup incomplete: " + "; ".join(failures))
        except Exception:
            pass


def has_current_arco_conversion_policy(metadata: dict[str, object]) -> bool:
    """Accept current ARL provenance and older metadata with the same RH rule."""
    if metadata.get("conversion_version") == ARCO_CONVERSION_VERSION:
        return True
    quality = metadata.get("humidity_quality_control")
    if not isinstance(quality, dict):
        return False
    policy = str(quality.get("policy", "")).casefold()
    return ARCO_RH_POLICY_MARKER in policy


def _cleanup_verified_cached_gribs(
    target_dir: Path, filedates: Path,
    notify: Callable[[str], None] | None,
) -> None:
    if _grib2arl_is_running():
        if notify:
            notify("ARCO GRIB cleanup skipped because grib2arl is still running")
        return
    obsolete: list[Path] = []
    for output in target_dir.glob("arco_era5_*.arl"):
        prefix = output.stem
        pressure = target_dir / f"{prefix}_pressure.grib"
        surface = target_dir / f"{prefix}_surface.grib"
        if pressure.with_suffix(".grib.part").exists() or surface.with_suffix(
            ".grib.part"
        ).exists():
            continue
        if not (pressure.is_file() or surface.is_file()):
            continue
        try:
            check_signature(output)
            times = read_arl_times([output], filedates)
            report = check_coverage(
                [output], times[0], times[-1], filedates, max_gap_hours=1.01
            )
            if not report.verified:
                continue
        except Exception:
            continue

        obsolete.extend((
            pressure, surface,
            pressure.with_suffix(".grib.part"),
            surface.with_suffix(".grib.part"),
            pressure.with_suffix(".quality.json"),
        ))
    _cleanup_intermediates(obsolete, notify, "for verified ARL caches")


def cleanup_verified_hysplit_netcdf_sources(
    arl_dir: Path, source_dir: Path, filedates: Path, *,
    notify: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Remove HYSPLIT-only NetCDF sources only when their ARL cache is verified."""
    arl_dir, source_dir, filedates = Path(arl_dir), Path(source_dir), Path(filedates)
    if not filedates.is_file():
        return 0, 0
    if _grib2arl_is_running():
        if notify:
            notify("ARCO NetCDF cleanup skipped because grib2arl is still running")
        return 0, 0
    source_root = source_dir.resolve()
    removed = 0
    freed = 0
    for metadata_path in arl_dir.glob("arco_era5_*.json"):
        output = metadata_path.with_suffix(".arl")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            raw_source = Path(str(metadata["source"]))
            if raw_source.is_symlink():
                continue
            source = raw_source.resolve()
            if (source.parent != source_root
                    or source.suffix.lower() != ".nc"
                    or not source.name.lower().endswith("_hysplit.nc")
                    or not source.is_file()
                    or not output.is_file() or output.stat().st_size == 0):
                continue
            pressure = arl_dir / f"{output.stem}_pressure.grib"
            surface = arl_dir / f"{output.stem}_surface.grib"
            if pressure.with_suffix(".grib.part").exists() or surface.with_suffix(
                ".grib.part"
            ).exists():
                # Another app instance may be exporting from this NetCDF.
                continue
            times = read_arl_times([output], filedates)
            report = check_coverage(
                [output], times[0], times[-1], filedates, max_gap_hours=1.01,
            )
            if not report.verified:
                continue
            size = source.stat().st_size
            source.unlink()
            removed += 1
            freed += size
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            continue
    if removed and notify:
        notify(
            f"Verified HYSPLIT-only ARCO NetCDF source removed: {removed} files, "
            f"{freed / (1024 ** 3):.2f} GiB freed; verified ARL cache retained"
        )
    return removed, freed


def _load_libraries():
    require_native_windows()
    try:
        import eccodes  # type: ignore
        import numpy as np  # type: ignore
        import xarray as xr  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ARCO NetCDF -> ARL requires Windows packages: "
            "py -3 -m pip install -r requirements-era5.txt "
            "(eccodes, numpy, xarray, netCDF4)"
        ) from exc
    return eccodes, np, xr


def _utc_times(ds, np) -> list[datetime]:
    if "time" not in ds.coords or not ds.sizes.get("time"):
        raise ValueError("NetCDF requires a nonempty time coordinate")
    times = []
    for item in ds.time.values:
        try:
            stamp = np.datetime_as_string(np.datetime64(item), unit="s")
            when = datetime.fromisoformat(str(stamp)).replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise ValueError("NetCDF time must be a UTC CF/ISO datetime") from exc
        if when.minute or when.second or when.microsecond:
            raise ValueError(f"NetCDF ERA5 must be on exact UTC hours: {when.isoformat()}")
        times.append(when)
    if len(times) != len(set(times)):
        raise ValueError("Duplicate NetCDF valid times")
    if any(b-a != timedelta(hours=1) for a,b in zip(times, times[1:])):
        raise ValueError("NetCDF hourly ERA5 records are not continuous and ascending")
    return times


def _axes(ds, np):
    if not all(key in ds.coords for key in ("latitude", "longitude", "level")):
        raise ValueError("ARCO NetCDF requires coordinates latitude, longitude and level")
    lat = np.asarray(ds.latitude.values, dtype=np.float64)
    lon = np.asarray(ds.longitude.values, dtype=np.float64)
    levels = np.asarray(ds.level.values, dtype=np.float64)
    if lat.ndim != 1 or lon.ndim != 1 or levels.ndim != 1 or lat.size < 2 or lon.size < 2:
        raise ValueError("NetCDF lat/lon/level coordinates must be 1D, with at least 2 grid points")
    if not np.all(np.isfinite(lat)) or not np.all(np.isfinite(lon)):
        raise ValueError("Nonfinite lat/lon coordinate")
    if np.any(np.abs(lat) > 90) or np.any(np.abs(lon) > 360):
        raise ValueError("Latitude/longitude outside valid range")
    if len(set(np.rint(levels).astype(int))) != len(levels):
        raise ValueError("Repeated or nonintegral pressure levels")
    expected = set(PRESSURE_LEVELS)
    present = set(int(lv) for lv in levels)
    if not expected.issubset(present):
        raise ValueError(f"NetCDF missing required ERA5 pressure levels: {sorted(expected-present)}")
    lat_order = np.argsort(-lat)
    lat_sorted = lat[lat_order]
    if not np.allclose(np.diff(lat_sorted), -0.25, atol=1e-5, rtol=0):
        raise ValueError("Latitude grid must be contiguous 0.25 degrees")
    lon_order = np.argsort(lon)
    lon_sorted = lon[lon_order]
    # Crops crossing the 0/360 meridian can have two blocks in index order.
    # Rotate once to preserve the geographic 0.25-degree neighborhood.
    gaps = np.diff(lon_sorted)
    if len(gaps) and float(np.max(gaps)) > 180:
        pivot = int(np.argmax(gaps)) + 1
        lon_order = np.concatenate([lon_order[pivot:], lon_order[:pivot]])
        lon_sorted = np.concatenate([lon_sorted[pivot:], lon_sorted[:pivot] + 360])
    if not np.allclose(np.diff(lon_sorted), 0.25, atol=1e-5, rtol=0):
        raise ValueError("Longitude grid must be contiguous 0.25 degrees, without gaps")
    if lon_sorted[-1] - lon_sorted[0] >= 360:
        raise ValueError("Longitude range must be narrower than a full wrap")
    return lat_order, lon_order, lat_sorted, lon_sorted


def _check_field(field, pressure: bool):
    required = {"latitude", "longitude", "time"}
    if pressure:
        required.add("level")
    elif field.name == "geopotential_at_surface" and "time" not in field.dims:
        required.remove("time")
    if not required.issubset(field.dims):
        raise ValueError(f"Variable {field.name!r} lacks required dimensions {required - set(field.dims)}")
    if not pressure and "level" in field.dims:
        raise ValueError(f"Surface variable {field.name!r} must not have a pressure-level axis")
    extra = set(field.dims) - required
    if extra:
        raise ValueError(f"Unsupported extra dimensions {extra} in {field.name!r}")


# ECMWF IFS CY41R2 saturation formulations: mixed water/ice phase. The
# 250.16 K to 273.16 K transition is consistent with Metview's q -> RH
# definition, which uses e(q,p) / e_mixed_saturation(T).
_EPSILON = 0.621981  # R_d / R_v, dry-air-to-water-vapour gas constant ratio
def _clip_negative_specific_humidity_for_rh(specific_humidity, np):
    """Floor negative q for derived RH only; leave the source data untouched.

    Specific humidity cannot be negative physically. ARCO NetCDF values are
    preserved as received; only the temporary array used to derive RH is floored.
    Counts and the minimum negative value are reported for provenance.
    """
    q = np.asarray(specific_humidity, dtype=np.float64)
    negative = np.isfinite(q) & (q < 0.0)
    count = int(np.count_nonzero(negative))
    if not count:
        return q, 0, None
    minimum = float(np.min(q[negative]))
    return np.maximum(q, 0.0), count, minimum


def relative_humidity_from_specific_humidity(
    temperature_kelvin, specific_humidity, pressure_hpa: float, np,
):
    """Return RH [%] from ERA5 q [kg/kg], T [K] and isobaric pressure [hPa].

    Saturation is with respect to water above 273.16 K, ice below 250.16 K
    and a squared mixed-phase blend between them. Supersaturation is retained;
    humidity values must not be silently clipped to [0,100] percent.
    """
    t = np.asarray(temperature_kelvin, dtype=np.float64)
    q = np.asarray(specific_humidity, dtype=np.float64)
    if t.shape != q.shape or not t.size:
        raise ValueError("Specific humidity and temperature grids are incompatible")
    invalid_t = (~np.isfinite(t)) | (t <= 0.0)
    invalid_q = (~np.isfinite(q)) | (q < 0.0) | (q >= 1.0)
    if np.any(invalid_t) or np.any(invalid_q):
        finite_t = t[np.isfinite(t)]
        finite_q = q[np.isfinite(q)]
        t_range = (f"[{float(finite_t.min()):.9g}, {float(finite_t.max()):.9g}] K"
                   if finite_t.size else "no finite values")
        q_range = (f"[{float(finite_q.min()):.9g}, {float(finite_q.max()):.9g}] kg/kg"
                   if finite_q.size else "no finite values")
        raise ValueError(
            f"Invalid ERA5 humidity/temperature at {float(pressure_hpa):g} hPa: "
            f"temperature range={t_range}, nonpositive/nonfinite={int(np.count_nonzero(invalid_t))}/{t.size}; "
            f"specific_humidity range={q_range}, invalid={int(np.count_nonzero(invalid_q))}/{q.size}"
        )
    p = float(pressure_hpa) * 100.0
    if not 0.0 < p <= 120000.0:
        raise ValueError("Invalid ERA5 isobaric pressure level")
    vapour_pressure = p * q / (_EPSILON + (1.0 - _EPSILON) * q)
    saturation_water = 611.21 * np.exp(17.502 * (t - 273.16) / (t - 32.19))
    saturation_ice = 611.21 * np.exp(22.587 * (t - 273.16) / (t + 0.7))
    water_fraction = np.clip((t - 250.16) / 23.0, 0.0, 1.0) ** 2
    saturation_mixed = (water_fraction * saturation_water +
                        (1.0 - water_fraction) * saturation_ice)
    relative_humidity_percent = 100.0 * vapour_pressure / saturation_mixed
    if not np.all(np.isfinite(relative_humidity_percent)):
        raise ValueError("Calculated ERA5 relative humidity contains invalid values")
    return relative_humidity_percent


def _load_slice(field, index: int, lat_order, lon_order, np, *, level: int | None = None):
    if level is not None:
        field = field.sel(level=level)
    sliced = field.isel(time=index) if "time" in field.dims else field
    value = sliced.transpose("latitude", "longitude").values
    data = np.asarray(value, dtype=np.float64)[np.ix_(lat_order, lon_order)]
    if not np.all(np.isfinite(data)):
        raise ValueError(f"NetCDF variable {field.name!r} has missing/non-finite values")
    return data


def _encode_message(e, output, values, when: datetime, *,
                    param: int, level: int | None,
                    lat, lon, precip: bool = False):
    sample = "regular_ll_pl_grib1" if level is not None else "regular_ll_sfc_grib1"
    handle = e.codes_grib_new_from_samples(sample)
    if handle is None:
        raise RuntimeError("ecCodes GRIB1 templates unavailable")
    try:
        e.codes_set(handle, "centre", 98)        # ECMWF
        e.codes_set(handle, "table2Version", 128)
        e.codes_set(handle, "paramId", param)
        e.codes_set(handle, "typeOfLevel", "isobaricInhPa" if level is not None else "surface")
        e.codes_set(handle, "level", int(level) if level is not None else 0)
        e.codes_set(handle, "Ni", int(lon.size))
        e.codes_set(handle, "Nj", int(lat.size))
        e.codes_set(handle, "latitudeOfFirstGridPointInDegrees", float(lat[0]))
        e.codes_set(handle, "latitudeOfLastGridPointInDegrees", float(lat[-1]))
        e.codes_set(handle, "longitudeOfFirstGridPointInDegrees", float(lon[0]) % 360)
        e.codes_set(handle, "longitudeOfLastGridPointInDegrees", float(lon[-1]) % 360)
        e.codes_set(handle, "iDirectionIncrementInDegrees", 0.25)
        e.codes_set(handle, "jDirectionIncrementInDegrees", 0.25)
        e.codes_set(handle, "iScansNegatively", 0)
        e.codes_set(handle, "jScansPositively", 0)
        if precip:
            # ARCO "total_precipitation" is one-hour accumulation ending at
            # the valid time. Do not invent a six-hour accumulation.
            ref = when - timedelta(hours=1)
            e.codes_set(handle, "dataDate", int(ref.strftime("%Y%m%d")))
            e.codes_set(handle, "dataTime", int(ref.strftime("%H%M")))
            e.codes_set(handle, "indicatorOfUnitOfTimeRange", 1)
            e.codes_set(handle, "timeRangeIndicator", 4)
            e.codes_set(handle, "startStep", 0)
            e.codes_set(handle, "endStep", 1)
        else:
            e.codes_set(handle, "dataDate", int(when.strftime("%Y%m%d")))
            e.codes_set(handle, "dataTime", int(when.strftime("%H%M")))
            e.codes_set(handle, "timeRangeIndicator", 0)
            e.codes_set(handle, "step", 0)
        e.codes_set(handle, "bitsPerValue", 16 if param != 129 else 24)
        e.codes_set_values(handle, values.reshape(-1))
        e.codes_write(handle, output)
    finally:
        e.codes_release(handle)


def netcdf_to_grib1(netcdf: Path, pressure_grib: Path,
                    surface_grib: Path,
                    notify: Callable[[str], None] | None = None, *,
                    quality_report: dict[str, object] | None = None,
                    wanted_times: tuple[datetime, ...] | None = None,
                    bbox: tuple[float, float, float, float] | None = None,
                    ) -> tuple[datetime, datetime]:
    """Convert a validated ERA5 ARCO NetCDF regional subset to two GRIB1 files."""
    # Distinguish a broken PyInstaller module archive from meteorological
    # NetCDF corruption. The native backend loads CFFI/pycparser lazily.
    try:
        e, np, xr = _load_libraries()
        from .portable import verify_frozen_grib_backend
        import sys
        if getattr(sys, "frozen", False):
            verify_frozen_grib_backend()
    except Exception as exc:
        import zlib
        if isinstance(exc, (ImportError, zlib.error)):
            raise RuntimeError(
                "ERA5 GRIB1 conversion backend could not be loaded from "
                "the application bundle. The error occurred while importing "
                "ecCodes/CFFI/pycparser, not while reading ERA5 NetCDF. "
                "Close the app, replace the entire HYSPLIT_Batch folder "
                "(EXE and _internal) with a fully downloaded verified copy "
                "on the local Desktop, then run HYSPLIT_Batch.exe --self-test "
                "to check grib1_backend. "
                f"Original error: {type(exc).__name__}: {exc}"
            ) from exc
        raise
    netcdf = Path(netcdf)
    if not netcdf.is_file():
        raise FileNotFoundError(netcdf)
    original = xr.open_dataset(netcdf, engine="netcdf4")
    ds = original
    if wanted_times is not None:
        try:
            ds = ds.sel(time=[np.datetime64(t.replace(tzinfo=None), "s") for t in wanted_times])
        except Exception:
            original.close()
            raise
    if bbox is not None:
        from .era5 import _subset_arco_domain
        ds = _subset_arco_domain(ds, bbox)
    pressure_grib, surface_grib = Path(pressure_grib), Path(surface_grib)
    pressure_grib.parent.mkdir(parents=True, exist_ok=True)
    surface_grib.parent.mkdir(parents=True, exist_ok=True)
    upper_tmp = pressure_grib.with_suffix(".grib.part")
    lower_tmp = surface_grib.with_suffix(".grib.part")
    for tmp in (upper_tmp, lower_tmp):
        tmp.unlink(missing_ok=True)
    try:
        times = _utc_times(ds, np)
        lat_order, lon_order, lat, lon = _axes(ds, np)
        pressure_humidity = ("relative_humidity" if "relative_humidity" in ds
                             else "specific_humidity")
        if pressure_humidity not in ds:
            raise ValueError(
                "ARCO NetCDF missing pressure humidity: require specific_humidity "
                "(kg/kg) or relative_humidity (%)"
            )
        for name, _param, _units in PRESSURE_PARAMS:
            source_name = pressure_humidity if name == "relative_humidity" else name
            if source_name not in ds:
                raise ValueError(f"ARCO NetCDF missing pressure variable {source_name}")
            _check_field(ds[source_name], True)
        if pressure_humidity == "specific_humidity" and notify:
            notify("Deriving RH [%] from ARCO specific_humidity [kg/kg], "
                   "temperature [K] and isobaric pressure [hPa]; mixed water/ice saturation")
        if notify:
            notify(
                "GRIB1 packing precision: 16 bits/value, 24 bits for geopotential; "
                "the NetCDF source is preserved and HYSPLIT grib2arl applies its "
                "own ARL packing."
            )
        negative_q_total = 0
        negative_q_samples = 0
        negative_q_minimum: float | None = None
        negative_q_by_level = {str(level): 0 for level in PRESSURE_LEVELS}
        negative_q_max_slice_fraction = 0.0
        for name, _param, _units in SURFACE_PARAMS:
            if name not in ds:
                raise ValueError(f"ARCO NetCDF missing surface variable {name}")
            _check_field(ds[name], False)
        estimated_grib_bytes = estimate_grib1_pair_size_bytes(
            len(times), len(lat), len(lon)
        )
        check_arco_conversion_space(
            estimated_grib_bytes, pressure_grib, notify=notify,
            expected_arl_bytes=len(times)*(1+37*6+6)*(len(lat)*len(lon)+50),
        )
        # Confirm grid coverage explicitly. No interpolation or extrapolation.
        with upper_tmp.open("wb") as upper, lower_tmp.open("wb") as lower:
            for i, when in enumerate(times):
                if notify and (i == 0 or i % 24 == 0):
                    notify(f"ARCO NetCDF -> GRIB1: {when.isoformat()} "
                           f"({i+1}/{len(times)} hourly records)")
                for level in PRESSURE_LEVELS:
                    for name, param, _units in PRESSURE_PARAMS:
                        if name == "relative_humidity" and pressure_humidity == "specific_humidity":
                            temperature = _load_slice(
                                ds["temperature"], i, lat_order, lon_order, np, level=level
                            )
                            q = _load_slice(
                                ds["specific_humidity"], i, lat_order, lon_order, np, level=level
                            )
                            q, negative_count, negative_minimum = (
                                _clip_negative_specific_humidity_for_rh(q, np)
                            )
                            negative_q_total += negative_count
                            negative_q_samples += int(q.size)
                            negative_q_by_level[str(level)] += negative_count
                            negative_q_max_slice_fraction = max(
                                negative_q_max_slice_fraction,
                                negative_count / q.size,
                            )
                            if negative_minimum is not None:
                                negative_q_minimum = (
                                    negative_minimum if negative_q_minimum is None
                                    else min(negative_q_minimum, negative_minimum)
                                )
                            values = relative_humidity_from_specific_humidity(
                                temperature, q, float(level), np
                            )
                        else:
                            values = _load_slice(
                                ds[name], i, lat_order, lon_order, np, level=level
                            )
                        _encode_message(e, upper, values, when, param=param,
                                        level=level, lat=lat, lon=lon)
                for name, param, _units in SURFACE_PARAMS:
                    values = _load_slice(ds[name], i, lat_order, lon_order, np)
                    _encode_message(e, lower, values, when, param=param,
                                    level=None, lat=lat, lon=lon,
                                    precip=name == "total_precipitation")
        if not upper_tmp.stat().st_size or not lower_tmp.stat().st_size:
            raise RuntimeError("ecCodes generated an empty GRIB1 file")
        upper_tmp.replace(pressure_grib)
        lower_tmp.replace(surface_grib)
        if negative_q_total:
            negative_q_fraction = negative_q_total / negative_q_samples
            message = (
                f"RH derivation: set {negative_q_total} negative specific_humidity "
                f"values to 0 for RH derivation only (minimum="
                f"{negative_q_minimum:.9g} kg/kg; overall={negative_q_fraction:.6%}, "
                f"max level-hour={negative_q_max_slice_fraction:.6%}); "
                f"source NetCDF unchanged"
            )
            if notify:
                notify(message)
        if quality_report is not None:
            quality_report.update({
                "negative_specific_humidity_values_set_to_zero_for_rh": negative_q_total,
                "minimum_negative_specific_humidity_kg_kg": negative_q_minimum,
                "negative_specific_humidity_fraction_overall": (
                    negative_q_total / negative_q_samples if negative_q_samples else 0.0
                ),
                "negative_specific_humidity_max_fraction_per_level_time_slice": (
                    negative_q_max_slice_fraction
                ),
                "by_pressure_level_hpa": negative_q_by_level,
                "policy": (
                    "For RH derivation only, all negative q values are floored to zero; "
                    "source NetCDF values are preserved and correction counts/minima "
                    "are recorded. No magnitude or prevalence cutoff is applied."
                ),
                "source_netcdf_modified": False,
            })
        return times[0], times[-1]
    finally:
        original.close()
        for tmp in (upper_tmp, lower_tmp):
            tmp.unlink(missing_ok=True)


def convert_arco_netcdf_to_arl(netcdf: Path, target_dir: Path,
                               earliest: datetime, latest: datetime, *,
                               hysplit_executable: Path,
                               notify: Callable[[str], None] | None = None) -> list[Path]:
    """Use ecCodes + native grib2arl + native filedates; publish only validated ARL."""
    require_native_windows()
    netcdf = Path(netcdf).resolve()
    if not netcdf.is_file():
        raise FileNotFoundError(netcdf)
    exec_dir = Path(hysplit_executable).resolve().parent
    converter = exec_dir / "grib2arl.exe"
    filedates = exec_dir / "filedates.exe"
    for path in (converter, filedates):
        if not path.is_file():
            raise FileNotFoundError(f"Native HYSPLIT decoder missing: {path}")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_verified_cached_gribs(target_dir, filedates, notify)
    ident = (
        f"{netcdf.resolve()}|{netcdf.stat().st_size}|{netcdf.stat().st_mtime_ns}|"
        f"{PRESSURE_LEVELS}|{PRESSURE_PARAMS}|{SURFACE_PARAMS}"
    )
    token = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
    output = target_dir / f"arco_era5_{token}.arl"
    pressure = target_dir / f"arco_era5_{token}_pressure.grib"
    surface = target_dir / f"arco_era5_{token}_surface.grib"
    quality_path = pressure.with_suffix(".quality.json")
    staged = output.with_suffix(output.suffix + ".part")
    cleanup_paths = [
        pressure, surface,
        pressure.with_suffix(".grib.part"),
        surface.with_suffix(".grib.part"),
        quality_path,
        staged,
    ]
    arl_validated = False
    preserve_staged = False
    try:
        if output.is_file() and output.stat().st_size > 0:
            check_signature(output)
            report = check_coverage(
                [output], earliest, latest, filedates, max_gap_hours=1.01
            )
            arl_validated = True
            if notify:
                notify(
                    f"ARCO ARL cache verified by filedates; existing ARL reused as-is: "
                    f"{output.name}"
                )
                cached_metadata = {}
                try:
                    value = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        cached_metadata = value
                except (OSError, UnicodeError, ValueError, TypeError):
                    pass
                if not has_current_arco_conversion_policy(cached_metadata):
                    notify(
                        "ARL RH conversion metadata is missing or differs from this app; "
                        "the validated ARL is being used unchanged."
                    )
            return [output]

        humidity_quality: dict[str, object] = {}
        reused_cached_gribs = False
        if quality_path.is_file():
            try:
                cached_quality = json.loads(quality_path.read_text(encoding="utf-8"))
                if isinstance(cached_quality, dict):
                    humidity_quality.update(cached_quality)
            except (OSError, ValueError, TypeError):
                humidity_quality.clear()
        recovered_staged = staged.is_file()
        humidity_input = "unknown"
        if recovered_staged:
            try:
                report = check_coverage(
                    [staged], earliest, latest, filedates, max_gap_hours=1.01,
                )
                if not report.verified:
                    raise RuntimeError("filedates did not verify staged ARL time coverage")
            except Exception as exc:
                preserve_staged = True
                raise RuntimeError(
                    f"Existing staged ARCO ARL was preserved and no conversion was "
                    f"restarted: {staged}. filedates verification failed: {exc}"
                ) from exc
            if notify:
                notify(
                    f"Reusing staged ARCO ARL after filedates verification: "
                    f"{staged.name}; no GRIB/ARL reconversion"
                )
            if netcdf.is_file():
                import xarray as xr
                with xr.open_dataset(netcdf, engine="netcdf4") as nc:
                    humidity_input = (
                        "relative_humidity" if "relative_humidity" in nc.data_vars
                        else "specific_humidity" if "specific_humidity" in nc.data_vars
                        else "unknown"
                    )
        else:
            pressure_exists = pressure.is_file()
            surface_exists = surface.is_file()
            if pressure_exists and surface_exists:
                require_grib1(pressure)
                require_grib1(surface)
                reused_cached_gribs = True
                if not humidity_quality and notify:
                    notify(
                        "Reusing the existing ARCO pressure/surface GRIB pair without "
                        "reconversion; RH-quality JSON is missing, so its historical "
                        "RH derivation details are unknown."
                    )
            else:
                if pressure_exists != surface_exists:
                    existing = pressure if pressure_exists else surface
                    require_grib1(existing)
                    if notify:
                        notify(
                            "Only one completed ARCO GRIB was present. Preserving it "
                            "until a complete replacement pair is generated from the "
                            "existing local NetCDF; no ERA5 download is needed."
                        )
                orphan_parts = [path for path in (
                    pressure.with_suffix(".grib.part"),
                    surface.with_suffix(".grib.part"),
                ) if path.is_file()]
                if orphan_parts and notify:
                    notify(
                        "Removing interrupted, incomplete GRIB staging files and "
                        "rebuilding from the existing local NetCDF; no network "
                        "download is needed: "
                        + ", ".join(path.name for path in orphan_parts)
                    )
                netcdf_to_grib1(netcdf, pressure, surface, notify,
                                quality_report=humidity_quality)
                quality_path.write_text(
                    json.dumps(humidity_quality, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            # Preserve provenance: GRIB1 outputs RH parameter 157 in either case,
            # but the field is derived when the input supplies specific humidity.
            import xarray as xr
            with xr.open_dataset(netcdf, engine="netcdf4") as nc:
                humidity_input = (
                    "relative_humidity" if "relative_humidity" in nc.data_vars
                    else "specific_humidity" if "specific_humidity" in nc.data_vars
                    else "unknown"
                )
            from .native_era5 import run_native_grib1_pair
            staged = run_native_grib1_pair(
                pressure, surface, output, converter,
                level_count=len(PRESSURE_LEVELS)+1, notify=notify
            )
            report = check_coverage(
                [staged], earliest, latest, filedates, max_gap_hours=1.01,
            )
            if not report.verified:
                raise RuntimeError("ARCO ARL valid-hour verification did not complete")
        staged.replace(output)
        arl_validated = True
        metadata = {
            # A cached GRIB or staged ARL was not converted with this invocation.
            # Do not label historical bytes with the current app's policy.
            "conversion_version": (
                ARCO_CONVERSION_VERSION
                if not recovered_staged and not reused_cached_gribs else None
            ),
            "recovered_staged_arl": recovered_staged,
            "reused_cached_gribs_without_quality_json": (
                reused_cached_gribs and not bool(humidity_quality)
            ),
            "source": str(netcdf), "format": "ERA5 ARCO 0.25 degree NetCDF",
            "intermediate_pressure_grib": str(pressure),
            "intermediate_surface_grib": str(surface),
            "intermediate_grib_cleanup": "attempted after ARL validation",
            "grib_edition": None if recovered_staged else 1,
            "pressure_levels_hpa": None if recovered_staged else list(PRESSURE_LEVELS),
            "grib1_bits_per_value": None if recovered_staged or reused_cached_gribs else {
                "default": 16,
                "geopotential_param_id_129": 24,
                "note": "GRIB packing; source NetCDF preserved; ARL is packed by grib2arl",
            },
            "hysplit_total_vertical_layers": (
                None if recovered_staged else len(PRESSURE_LEVELS) + 1
            ),
            "humidity_input": humidity_input,
            "humidity_output": "relative_humidity (%) / ECMWF GRIB1 paramId=157",
            "humidity_derivation": (
                "not recorded; staged/cached data reused after time verification"
                if recovered_staged or reused_cached_gribs
                else "q,T,p -> RH; ECMWF mixed water/ice saturation formula"
                if humidity_input == "specific_humidity"
                else "direct relative_humidity field"
                if humidity_input == "relative_humidity"
                else "not recorded; staged/cached data reused after time verification"
            ),
            "humidity_quality_control": humidity_quality or ({
                "policy": "not recorded; staged ARL or cached GRIB pair reused unchanged"
            } if recovered_staged or reused_cached_gribs else {
                "negative_specific_humidity_values_set_to_zero_for_rh": 0,
                "minimum_negative_specific_humidity_kg_kg": None,
                "negative_specific_humidity_fraction_overall": 0.0,
                "negative_specific_humidity_max_fraction_per_level_time_slice": 0.0,
                "by_pressure_level_hpa": {},
                "policy": (
                    "For RH derivation only, all negative q values are floored to zero; "
                    "source NetCDF values are preserved and correction counts/minima "
                    "are recorded. No magnitude or prevalence cutoff is applied."
                ),
                "source_netcdf_modified": False,
            }),
            "time_count": report.time_count,
            "first": report.first.isoformat() if report.first else None,
            "last": report.last.isoformat() if report.last else None,
            "validation": "NOAA Windows filedates (timestamps only; not physical validation)",
        }
        output.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return [output]
    finally:
        outcome = "after ARL validation" if arl_validated else "after conversion stopped"
        if arl_validated:
            _cleanup_intermediates(cleanup_paths, notify, outcome)
        else:
            # Complete GRIBs are reusable conversion inputs. Keeping them after
            # a failed ARL conversion avoids repeating the expensive NetCDF scan
            # on restart. Incomplete GRIB/staging fragments are discarded.
            incomplete = [
                pressure.with_suffix(".grib.part"),
                surface.with_suffix(".grib.part"),
            ]
            if not preserve_staged:
                incomplete.append(staged)
            _cleanup_intermediates(incomplete, notify, outcome)
            if pressure.is_file() and surface.is_file() and notify:
                notify(
                    "Complete ARCO GRIB inputs and RH provenance were retained for "
                    "the next local retry; no ERA5 download or GRIB regeneration is needed."
                )

