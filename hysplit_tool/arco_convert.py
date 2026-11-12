"""Windows-only Google ARCO ERA5 NetCDF -> GRIB1 -> HYSPLIT ARL.

ARCO's 0.25-degree pressure/surface dataset is NOT a generic CF-NetCDF input.
This decoder explicitly validates the required ECMWF parameters, 12 pressure
levels, hourly steps and regular 0.25-degree grid. It then encodes GRIB1 using
ECMWF ecCodes, invokes the already-installed native grib2arl.exe, and refuses
to publish ARL until NOAA filedates.exe confirms complete temporal coverage.

Important: successful ARL packaging does not prove meteorological accuracy or
that a regional grid contains the entire backward trajectory. The Zarr source
can require large transfers even for a small regional NetCDF subset.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .native_era5 import run_native_grib1_pair
from .platform_guard import require_native_windows
from .validation import check_coverage, check_signature

UTC = timezone.utc
# The 12-layer pressure grid and 7-field surface sequence are compatible with
# the locally verified HYSPLIT grib2arl.exe -g3 -p1 -k13 -q0 -z1 workflow.
PRESSURE_LEVELS = (1000, 925, 850, 700, 600, 500,
                   400, 300, 250, 200, 150, 100)
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
    if (not np.all(np.isfinite(t)) or not np.all(np.isfinite(q)) or
            np.any(t < 150.0) or np.any(t > 350.0) or
            np.any(q < 0.0) or np.any(q >= 1.0)):
        raise ValueError("Invalid ERA5 specific humidity (kg/kg) or temperature (K)")
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
                    notify: Callable[[str], None] | None = None) -> tuple[datetime, datetime]:
    """Convert a validated ERA5 ARCO NetCDF regional subset to two GRIB1 files."""
    e, np, xr = _load_libraries()
    netcdf = Path(netcdf)
    if not netcdf.is_file():
        raise FileNotFoundError(netcdf)
    ds = xr.open_dataset(netcdf, engine="netcdf4")
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
        for name, _param, _units in SURFACE_PARAMS:
            if name not in ds:
                raise ValueError(f"ARCO NetCDF missing surface variable {name}")
            _check_field(ds[name], False)
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
        return times[0], times[-1]
    finally:
        ds.close()
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
    ident = f"{netcdf.resolve()}|{netcdf.stat().st_size}|{netcdf.stat().st_mtime_ns}|arco_nc_v1"
    token = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
    output = target_dir / f"arco_era5_{token}.arl"
    if output.is_file() and output.stat().st_size > 0:
        check_signature(output)
        check_coverage([output], earliest, latest, filedates, max_gap_hours=1.01)
        if notify:
            notify(f"ARCO ARL cache verified: {output.name}")
        return [output]
    pressure = target_dir / f"arco_era5_{token}_pressure.grib"
    surface = target_dir / f"arco_era5_{token}_surface.grib"
    if not (pressure.is_file() and surface.is_file()):
        netcdf_to_grib1(netcdf, pressure, surface, notify)
    # Preserve provenance: GRIB1 outputs RH parameter 157 in either case,
    # but the field is derived when the input supplies specific humidity.
    import xarray as xr
    with xr.open_dataset(netcdf, engine="netcdf4") as nc:
        humidity_input = ("relative_humidity" if "relative_humidity" in nc.data_vars
                          else "specific_humidity" if "specific_humidity" in nc.data_vars
                          else "unknown")
    from .native_era5 import run_native_grib1_pair
    staged = run_native_grib1_pair(
        pressure, surface, output, converter, level_count=13, notify=notify
    )
    try:
        report = check_coverage([staged], earliest, latest, filedates, max_gap_hours=1.01)
        if not report.verified:
            raise RuntimeError("ARCO ARL valid-hour verification did not complete")
        staged.replace(output)
        metadata = {
            "source": str(netcdf), "format": "ERA5 ARCO 0.25 degree NetCDF",
            "intermediate_pressure_grib": str(pressure),
            "intermediate_surface_grib": str(surface),
            "grib_edition": 1, "pressure_levels_hpa": list(PRESSURE_LEVELS),
            "humidity_input": humidity_input,
            "humidity_output": "relative_humidity (%) / ECMWF GRIB1 paramId=157",
            "humidity_derivation": (
                "q,T,p -> RH; ECMWF mixed water/ice saturation formula"
                if humidity_input == "specific_humidity"
                else "direct relative_humidity field"
            ),
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
        staged.unlink(missing_ok=True)

