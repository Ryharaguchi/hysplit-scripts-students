"""Incremental, restartable NetCDF export for Google ARCO ERA5.

Google's 0.25-degree pressure-level Zarr stores 37 vertical levels across
the whole globe in *each variable/hour chunk*. Selecting a regional subset
reduces the output, but not the bytes required to fetch a Zarr chunk.
Writing all hours via xarray.to_netcdf with chunks=None silently holds a
large computation without intermediate commits. This writer instead reads
one *variable/hour* at a time, syncs a CF-compatible NetCDF after every
complete hour, and resumes from a JSON checkpoint after interruption.

Not a promise of fast downloads; the upstream Zarr chunk remains huge.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Callable

_FORMAT = "arco-hourly-stream-v1"
_PROGRESS_INTERVAL_S = 20


def _checksum(selected, target: Path, *, source: str) -> str:
    """Tie a partial file to exact coordinates, fields and source store."""
    from numpy import asarray

    dims = tuple(sorted((str(k), int(v)) for k, v in selected.sizes.items()))
    coords = {
        name: asarray(selected[name].values).tobytes().hex()
        for name in ("time", "level", "latitude", "longitude")
    }
    request = json.dumps({
        "format": _FORMAT,
        "target": target.name,
        "source": source,
        "dims": dims,
        "variables": sorted(selected.data_vars),
        "coords": coords,
    }, sort_keys=True).encode("utf-8")
    return hashlib.sha256(request).hexdigest()


def _save_checkpoint(checkpoint: Path, token: str, count: int) -> None:
    temporary = checkpoint.with_name(checkpoint.name + ".writing")
    temporary.write_text(
        json.dumps({"format": _FORMAT, "token": token, "completed_hours": count}),
        encoding="utf-8",
    )
    os.replace(temporary, checkpoint)


def _create_file(file, selected, np, *, write_static: bool = True) -> None:
    """Create NetCDF4 variables with xarray-compatible CF time/axis metadata."""
    file.createDimension("time", None)
    for dim in ("level", "latitude", "longitude"):
        file.createDimension(dim, int(selected.sizes[dim]))
    epoch = "seconds since 1970-01-01 00:00:00"
    time = file.createVariable("time", "i8", ("time",))
    time.units = epoch
    time.calendar = "proleptic_gregorian"
    level = file.createVariable("level", "i4", ("level",))
    level.units = "hPa"
    level[:] = np.asarray(selected["level"].values, dtype="int32")
    for axis, units in (("latitude", "degrees_north"), ("longitude", "degrees_east")):
        var = file.createVariable(axis, "f8", (axis,))
        var.units = units
        var[:] = np.asarray(selected[axis].values, dtype="float64")
    for name in selected.data_vars:
        data = selected[name]
        dims = tuple(data.dims)
        if (set(dims) - {"time", "level", "latitude", "longitude"}
                or "latitude" not in dims or "longitude" not in dims
                or ("level" in dims and "time" not in dims)):
            raise ValueError(f"Unsupported ARCO variable dimensions: {name} {dims}")
        dtype = np.dtype(data.dtype)
        if dtype.kind not in ("f", "i", "u"):
            raise ValueError(f"Unsupported ARCO variable type: {name} {dtype}")
        # Keep the original floating precision in the downstream GRIB pipeline.
        dtype = "f8" if dtype.itemsize > 4 else "f4"
        chunking = tuple(
            1 if dim in ("time", "level") else min(64, int(selected.sizes[dim]))
            for dim in dims
        )
        nc = file.createVariable(
            name, dtype, dims, zlib=True, complevel=1, chunksizes=chunking,
        )
        for attr in ("units", "long_name", "standard_name"):
            if isinstance(data.attrs.get(attr), str):
                setattr(nc, attr, data.attrs[attr])

    # Static topography is relatively small; emit once.
    if write_static:
        for name in selected.data_vars:
            if "time" not in selected[name].dims:
                values = np.asarray(selected[name].values)
                file[name][:] = values
    file.sync()


def _assert_resume_compatible(file, selected, np, count: int) -> None:
    for dim in ("level", "latitude", "longitude"):
        if dim not in file.dimensions or len(file.dimensions[dim]) != selected.sizes[dim]:
            raise ValueError(f"Partial ARCO NetCDF has incompatible dimension {dim}")
        if not np.array_equal(
            np.asarray(file[dim][:]), np.asarray(selected[dim].values)
        ):
            raise ValueError(f"Partial ARCO NetCDF has incompatible coordinates {dim}")
    if set(selected.data_vars) - set(file.variables):
        raise ValueError("Partial ARCO NetCDF is missing a required variable")
    for name, data in selected.data_vars.items():
        if tuple(file[name].dimensions) != tuple(data.dims):
            raise ValueError(
                f"Partial ARCO NetCDF has incompatible dimensions for {name}"
            )
    if not 0 <= count <= len(file.dimensions["time"]):
        raise ValueError("Partial ARCO checkpoint exceeds NetCDF time records")
    actual = np.asarray(file["time"][:count])
    expected = np.asarray(selected["time"].values[:count]).astype(
        "datetime64[s]"
    ).astype("int64")
    if not np.array_equal(actual, expected):
        raise ValueError("Partial ARCO NetCDF timestamps differ from request")


def _write_one_hour(file, selected, index: int, time_epoch: int, np) -> None:
    """Read one hourly variable at a time; avoid loading the full time span."""
    for name in selected.data_vars:
        variable = selected[name]
        if "time" not in variable.dims:
            continue
        dims = tuple(dim for dim in variable.dims if dim != "time")
        values = np.asarray(variable.isel(time=index).transpose(*dims).values)
        expected = tuple(selected.sizes[dim] for dim in dims)
        if values.shape != expected:
            raise ValueError(
                f"Invalid ARCO slice shape {name}: {values.shape} != {expected}"
            )
        position = tuple(index if dim == "time" else slice(None)
                         for dim in variable.dims)
        file[name][position] = values
    # Only commit timestamp after all variables for this hour have been written.
    file["time"][index] = time_epoch


def _migrate_allvars_partial(
    source_partial: Path, source_checkpoint: Path,
    temporary: Path, checkpoint: Path, selected, np, netCDF4,
    token: str, notify: Callable[[str], None] | None,
) -> int:
    """Copy committed hours from an all-variable cache into a field subset.

    The source remains untouched during migration. After the complete
    HYSPLIT-only NetCDF is validated, the caller removes obsolete source
    partials and migration staging files.
    """
    stage = temporary.with_name(temporary.name + ".migrating")
    stage_checkpoint = checkpoint.with_name(checkpoint.name + ".migrating")
    state = json.loads(source_checkpoint.read_text(encoding="utf-8"))
    if state.get("format") != _FORMAT or not isinstance(state.get("token"), str):
        raise ValueError("The all-variable partial checkpoint is invalid")
    completed = int(state.get("completed_hours", -1))

    # Recover a migration interrupted between the two atomic renames.
    if temporary.is_file() and not checkpoint.is_file() and stage_checkpoint.is_file():
        pending = json.loads(stage_checkpoint.read_text(encoding="utf-8"))
        if pending.get("format") == _FORMAT and pending.get("token") == token:
            try:
                with netCDF4.Dataset(temporary, "r") as migrated:
                    _assert_resume_compatible(
                        migrated, selected, np, int(pending["completed_hours"])
                    )
                os.replace(stage_checkpoint, checkpoint)
                return int(pending["completed_hours"])
            except (OSError, KeyError, ValueError, TypeError):
                # Preserve the source and rebuild the target staging copy below.
                pass

    if stage.is_file() and stage_checkpoint.is_file():
        try:
            pending = json.loads(stage_checkpoint.read_text(encoding="utf-8"))
            if pending.get("format") == _FORMAT and pending.get("token") == token:
                with netCDF4.Dataset(stage, "r") as migrated:
                    _assert_resume_compatible(
                        migrated, selected, np, int(pending["completed_hours"])
                    )
                os.replace(stage, temporary)
                os.replace(stage_checkpoint, checkpoint)
                if notify:
                    notify(
                        f"Google ARCO: recovered interrupted field migration; "
                        f"resuming from {int(pending['completed_hours'])}/{len(selected.time)} hours"
                    )
                return int(pending["completed_hours"])
        except (OSError, KeyError, ValueError, TypeError):
            # The source archive remains intact, so an incomplete staging copy
            # can safely be rebuilt on the next attempt.
            pass
    if stage.exists():
        stage.unlink()
    if stage_checkpoint.exists():
        stage_checkpoint.unlink()

    with netCDF4.Dataset(source_partial, "r") as source:
        _assert_resume_compatible(source, selected, np, completed)
        with netCDF4.Dataset(stage, "w", format="NETCDF4") as migrated:
            _create_file(migrated, selected, np, write_static=False)
            for name, data in selected.data_vars.items():
                dims = tuple(data.dims)
                if "time" in dims:
                    if completed:
                        slices = tuple(
                            slice(0, completed) if dim == "time" else slice(None)
                            for dim in dims
                        )
                        migrated[name][slices] = source[name][slices]
                else:
                    migrated[name][:] = source[name][:]
            if completed:
                migrated["time"][:completed] = source["time"][:completed]
            migrated.sync()
    _save_checkpoint(stage_checkpoint, token, completed)
    with netCDF4.Dataset(stage, "r") as migrated:
        _assert_resume_compatible(migrated, selected, np, completed)
    os.replace(stage, temporary)
    os.replace(stage_checkpoint, checkpoint)
    if notify:
        notify(
            f"Google ARCO: migrated {completed} completed hours from the all-variable "
            "partial file; the source partial remains until the HYSPLIT NetCDF completes"
        )
    return completed


def export_hourly_netcdf(
    selected, result: Path, *,
    source: str,
    notify: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    migrate_from: tuple[Path, Path] | None = None,
) -> Path:
    """Export all requested hourly fields using bounded memory and resumable writes."""
    import netCDF4
    import numpy as np

    result = Path(result)
    result.parent.mkdir(parents=True, exist_ok=True)
    temporary = result.with_suffix(".part")
    checkpoint = temporary.with_name(temporary.name + ".json")
    token = _checksum(selected, result, source=source)
    times = np.asarray(selected["time"].values).astype("datetime64[s]").astype("int64")
    total = len(times)
    if not total:
        raise ValueError("No ERA5 hourly records selected")

    completed = 0
    resume = False
    if (temporary.is_file() and not checkpoint.is_file()
            and checkpoint.with_name(checkpoint.name + ".migrating").is_file()):
        if migrate_from is None:
            raise RuntimeError(
                f"Google ARCO: an interrupted field migration is present at {temporary}; "
                "the original all-variable partial paths are required to recover it."
            )
        import netCDF4
        completed = _migrate_allvars_partial(
            Path(migrate_from[0]), Path(migrate_from[1]),
            temporary, checkpoint, selected, np, netCDF4, token, notify,
        )
        resume = True
    if (not temporary.exists() and not checkpoint.exists() and migrate_from is not None
            and (Path(migrate_from[0]).exists() or Path(migrate_from[1]).exists())):
        source_partial, source_checkpoint = map(Path, migrate_from)
        if not source_partial.is_file() or not source_checkpoint.is_file():
            raise RuntimeError(
                "Google ARCO: the all-variable partial cache is incomplete; "
                f"both files were preserved: {source_partial}, {source_checkpoint}"
            )
        import netCDF4
        completed = _migrate_allvars_partial(
            source_partial, source_checkpoint, temporary, checkpoint,
            selected, np, netCDF4, token, notify,
        )
        resume = True
    if temporary.is_file() and checkpoint.is_file():
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if state.get("format") != _FORMAT or state.get("token") != token:
                raise ValueError("The interrupted request does not match this window")
            completed = int(state["completed_hours"])
            with netCDF4.Dataset(temporary, "r") as file:
                _assert_resume_compatible(file, selected, np, completed)
            resume = True
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Google ARCO: existing partial data/checkpoint are incompatible. "
                f"Both files were preserved: {temporary}, {checkpoint}. {exc}"
            ) from exc
    if not resume and (temporary.exists() or checkpoint.exists()):
        raise RuntimeError(
            f"Google ARCO: orphan partial NetCDF/checkpoint preserved at "
            f"{temporary} and {checkpoint}. Inspect or relocate before retry."
        )

    if notify:
        msg = (f"Google ARCO: hourly NetCDF export {completed}/{total} complete; "
               "one variable/hour at a time to bound memory")
        notify(msg)
        if resume:
            notify(f"Google ARCO: resuming {total - completed} remaining hours")

    if progress:
        progress(completed, total)

    state_lock = threading.Lock()
    state = {"completed": completed}
    signal = threading.Event()

    def heartbeat() -> None:
        while not signal.wait(_PROGRESS_INTERVAL_S):
            with state_lock:
                done = state["completed"]
            if notify:
                notify(
                    f"Google ARCO export active: {done}/{total} hours saved; "
                    "waiting for large global Zarr chunks/network or decompression"
                )

    monitor = threading.Thread(target=heartbeat, name="arco-export-heartbeat", daemon=True)
    monitor.start()
    try:
        with netCDF4.Dataset(temporary, "a" if resume else "w",
                             format="NETCDF4") as file:
            if not resume:
                _create_file(file, selected, np)
                _save_checkpoint(checkpoint, token, 0)
            for i in range(completed, total):
                _write_one_hour(file, selected, i, int(times[i]), np)
                file.sync()
                _save_checkpoint(checkpoint, token, i + 1)
                with state_lock:
                    state["completed"] = i + 1
                if progress:
                    progress(i + 1, total)
                if notify and (i == 0 or (i + 1) % 6 == 0 or i == total - 1):
                    notify(
                        f"Google ARCO NetCDF: {i + 1}/{total} hours saved "
                        f"({100 * (i+1) / total:.1f}%), "
                        f"partial file {temporary.stat().st_size / (1024**2):.1f} MiB"
                    )
        # Check the completed file is CF-readable before publishing it.
        import xarray as xr
        with xr.open_dataset(temporary, engine="netcdf4") as check:
            if check.sizes.get("time") != total:
                raise ValueError("ERA5 NetCDF export has missing hourly records")
            check_times = np.asarray(check.time.values).astype(
                "datetime64[s]"
            ).astype("int64")
            if not np.array_equal(check_times, times):
                raise ValueError("ERA5 NetCDF export timestamps do not match request")
        os.replace(temporary, result)
        checkpoint.unlink(missing_ok=True)
        if notify:
            notify(f"Google ARCO NetCDF saved successfully: {result}")
        return result
    finally:
        signal.set()
        monitor.join(timeout=2)

