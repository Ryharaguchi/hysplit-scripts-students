"""ERA5 CDS GRIB -> ARL bridge using NOAA's external api2arl converter.

This is not a reimplementation of NOAA's ARL format. GRIB decoding and field
translation use ECMWF ecCodes. Every produced ARL file needs NOAA filedates
validation before it is accepted as HYSPLIT input.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .validation import check_coverage

UTC = timezone.utc
G0 = 9.80665  # m/s^2; geopotential (m^2/s^2) -> geopotential height (m)
UPPER_REQUIRED = frozenset(("gh", "t", "u", "v", "w"))
SURFACE_REQUIRED = frozenset(("sp",))
SUPPORTED = UPPER_REQUIRED | SURFACE_REQUIRED | {"r", "2t", "2r", "10u", "10v", "orog"}


def _ceil_hour(moment: datetime) -> datetime:
    hour = moment.replace(minute=0, second=0, microsecond=0)
    return hour if hour == moment else hour + timedelta(hours=1)


def convert_grib_to_hourly_grib2(
    source_files: list[Path], work: Path, earliest: datetime, latest: datetime,
    notify: Callable[[str], None] | None = None,
) -> dict[datetime, Path]:
    """Normalize ERA5 CDS GRIB1/2 into one GRIB2 per valid UTC hour.

    Converting a GRIB1 message to edition 2 is supported by ecCodes for common
    parameters; unsupported local parameters fail closed. ECMWF geopotential
    z (m^2/s^2) is converted to geopotential height gh (m), as expected by
    NOAA's default api2arl decoding table. No interpolation is applied.
    """
    try:
        import eccodes  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ERA5 ARL conversion requires: pip install eccodes") from exc
    if not source_files:
        raise ValueError("No CDS GRIB files to convert")
    if earliest.tzinfo is None or latest.tzinfo is None or earliest > latest:
        raise ValueError("Invalid UTC coverage bounds")
    first = earliest.astimezone(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    last = _ceil_hour(latest.astimezone(UTC)) + timedelta(hours=1)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    paths: dict[datetime, Path] = {}
    observed = defaultdict(set)
    pressure_levels = defaultdict(set)
    counters = defaultdict(int)

    for source in source_files:
        source = Path(source)
        with source.open("rb") as input_stream:
            while True:
                gid = eccodes.codes_grib_new_from_file(input_stream)
                if gid is None:
                    break
                try:
                    date = int(eccodes.codes_get(gid, "validityDate"))
                    time = int(eccodes.codes_get(gid, "validityTime"))
                    instant = datetime.strptime(f"{date:08d}{time:04d}", "%Y%m%d%H%M").replace(tzinfo=UTC)
                    if not first <= instant <= last:
                        continue
                    if instant.minute:
                        raise ValueError(f"ERA5 file contains non-hourly valid time: {instant}")
                    grid = str(eccodes.codes_get(gid, "gridType"))
                    if grid != "regular_ll":
                        raise ValueError(f"api2arl requires regular latitude/longitude GRIB: got {grid}")
                    kind = str(eccodes.codes_get(gid, "typeOfLevel"))
                    name = str(eccodes.codes_get(gid, "shortName"))
                    if kind == "isobaricInhPa" and name == "z":
                        # geopotential (m^2 s^-2) -> geopotential height (m)
                        values = eccodes.codes_get_values(gid)
                        eccodes.codes_set(gid, "paramId", 156)
                        eccodes.codes_set_values(gid, values / G0)
                        name = str(eccodes.codes_get(gid, "shortName"))
                        if name != "gh":
                            raise ValueError("Unable to re-encode ERA5 geopotential as geopotential height")
                    if name not in SUPPORTED:
                        continue
                    if kind == "isobaricInhPa" and name in UPPER_REQUIRED | {"r"}:
                        pressure_levels[instant].add(int(eccodes.codes_get(gid, "level")))
                    elif kind == "surface" and name in SURFACE_REQUIRED | {"orog"}:
                        pass
                    elif kind == "heightAboveGround" and name in {"2t", "2r", "10u", "10v"}:
                        pass
                    else:
                        continue
                    if int(eccodes.codes_get(gid, "edition")) == 1:
                        eccodes.codes_set(gid, "edition", 2)
                    if int(eccodes.codes_get(gid, "edition")) != 2:
                        raise ValueError(f"Failed GRIB1->GRIB2 conversion for {source.name}")
                    observed[instant].add(name)
                    output = paths.setdefault(instant, work / f"{instant:%Y%m%dT%H}Z.grib2")
                    with output.open("ab") as target:
                        eccodes.codes_write(gid, target)
                    counters[instant] += 1
                except Exception as exc:
                    raise RuntimeError(f"Cannot process ERA5 GRIB message in {source.name}: {exc}") from exc
                finally:
                    eccodes.codes_release(gid)
    if not paths:
        raise ValueError("No GRIB messages fall within required model period")
    expected = [first + timedelta(hours=h)
                for h in range(int((last - first).total_seconds() / 3600) + 1)]
    for instant in expected:
        if instant not in paths:
            raise ValueError(f"Missing ERA5 hourly GRIB data at {instant.isoformat()}")
        missing = (UPPER_REQUIRED | SURFACE_REQUIRED) - observed[instant]
        if missing:
            raise ValueError(f"Missing required ERA5 variables at {instant.isoformat()}: {sorted(missing)}")
        if len(pressure_levels[instant]) < 5:
            raise ValueError(f"Insufficient ERA5 pressure levels at {instant.isoformat()}")
    if notify:
        notify(f"Prepared {len(paths)} hourly GRIB2 datasets from ERA5 CDS")
    return dict(sorted(paths.items()))


def _converter_command(binary: str, cwd: Path, input_name: str, output_name: str) -> list[str]:
    """Build native or WSL command without shell interpolation."""
    if binary.startswith("wsl:"):
        linux_binary = binary[4:].strip()
        if not linux_binary.startswith("/"):
            raise ValueError("WSL converter must be specified as wsl:/absolute/path/to/api2arl")
        probe = subprocess.run(["wsl.exe", "--exec", "wslpath", "-a", str(cwd.resolve())],
                               capture_output=True, text=True, errors="replace", timeout=30)
        if probe.returncode:
            raise RuntimeError(f"WSL path conversion failed: {probe.stderr.strip()}")
        linux_dir = probe.stdout.strip().rstrip("/")
        if not linux_dir.startswith("/"):
            raise RuntimeError("Invalid WSL working directory")
        return ["wsl.exe", "--cd", linux_dir, "--exec", linux_binary,
                "-i" + input_name, "-o" + output_name, "-t50"]
    native = Path(binary.strip().strip('"')).expanduser().resolve()
    if not native.is_file():
        raise FileNotFoundError(f"NOAA api2arl executable not found: {native}")
    return [str(native), "-i" + input_name, "-o" + output_name, "-t50"]


def convert_cds_to_arl(source_files: list[Path], destination: Path,
                       earliest: datetime, latest: datetime, *,
                       converter: str, filedates: Path,
                       notify: Callable[[str], None] | None = None) -> list[Path]:
    """Convert via external NOAA api2arl; require independent ARL time validation.

    Stores one ARL file per UTC month to avoid HYSPLIT's month-boundary issue.
    Aborts before returning usable files if the converter or filedates fails.
    """
    if not converter.strip():
        raise ValueError("Install/compile NOAA api2arl and specify its path first")
    if not Path(filedates).is_file():
        raise FileNotFoundError("NOAA filedates executable required to validate converted ARL")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="era5_convert_", dir=destination) as tmp:
        work = Path(tmp)
        hourly = convert_grib_to_hourly_grib2(source_files, work / "hourly", earliest, latest, notify)
        months = defaultdict(list)
        for when, path in hourly.items():
            months[when.strftime("%Y%m")].append((when, path))
        outputs = []
        for month, records in sorted(months.items()):
            subdir = work / f"convert_{month}"
            subdir.mkdir()
            output = destination / f"era5_cds_{month}.arl"
            staged = destination / f"era5_cds_{month}.arl.part"
            staged.unlink(missing_ok=True)
            try:
                with staged.open("wb") as final:
                    for when, grib_file in records:
                        local_grib = subdir / "input.grib2"
                        shutil.copyfile(grib_file, local_grib)
                        local_arl = subdir / "output.arl"
                        local_arl.unlink(missing_ok=True)
                        args = _converter_command(converter, subdir, local_grib.name, local_arl.name)
                        completed = subprocess.run(args, cwd=subdir, capture_output=True,
                                                   text=True, errors="replace", timeout=900)
                        (subdir / f"{when:%Y%m%dT%H}.log").write_text(
                            completed.stdout + "\n" + completed.stderr, encoding="utf-8")
                        if completed.returncode or not local_arl.is_file() or local_arl.stat().st_size == 0:
                            raise RuntimeError(f"api2arl failed at {when.isoformat()}: "
                                               + (completed.stderr or completed.stdout)[-700:])
                        with local_arl.open("rb") as generated:
                            shutil.copyfileobj(generated, final)
                        if notify:
                            notify(f"Converted ERA5 {when:%Y-%m-%d %H} UTC")
                # Validate all monthly files together before exposing them.
                outputs.append((output, staged))
            except Exception:
                staged.unlink(missing_ok=True)
                raise
        staged_paths = [path for _output, path in outputs]
        check_coverage(staged_paths, earliest, latest, filedates)
        for output, staged in outputs:
            staged.replace(output)
        return [output for output, _stage in outputs]
