"""Windows-native ERA5 CDS GRIB1 -> HYSPLIT ARL conversion.

Uses the native Windows grib2arl.exe shipped with HYSPLIT. Does not invoke
WSL, NOAAs online READY calculation, or a user-selected converter.
Only input GRIB edition 1 is accepted. NOAA's filedates.exe must confirm
model-hour coverage before any ARL file is published.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from .platform_guard import require_native_windows
from .validation import check_coverage, check_signature

_PAIR = re.compile(r"^era5_(pressure|single)_(\d{6})_(.+)\.grib$", re.IGNORECASE)


def match_cds_pairs(sources: list[Path]) -> dict[str, tuple[Path, Path]]:
    """Pair upper/surface files of the same month, bounding box and day range."""
    pairs: dict[str, dict[str, Path]] = defaultdict(dict)
    for path in map(Path, sources):
        matched = _PAIR.match(path.name)
        if not matched:
            raise ValueError(f"Unexpected CDS GRIB filename: {path.name}")
        variant, month, label = matched.groups()
        identifier = month + "_" + label
        if variant.lower() in pairs[identifier]:
            raise ValueError(f"Duplicate CDS {variant} dataset: {path.name}")
        pairs[identifier][variant.lower()] = path
    if not pairs:
        raise ValueError("No CDS meteorological data downloaded")
    missing = {k: sorted({"pressure", "single"} - set(v))
               for k, v in pairs.items() if set(v) != {"pressure", "single"}}
    if missing:
        raise ValueError(f"ERA5 CDS upper/surface pairs incomplete: {missing}")
    return {key: (values["pressure"], values["single"])
            for key, values in sorted(pairs.items())}


def require_grib1(path: Path) -> None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 16:
        raise ValueError(f"Missing/empty GRIB file: {path}")
    with path.open("rb") as stream:
        header = stream.read(8)
    if header[:4] != b"GRIB":
        raise ValueError(f"File is not GRIB: {path}")
    if header[7] != 1:
        raise ValueError(
            f"Windows grib2arl.exe only accepts GRIB1; {path.name} is GRIB edition "
            f"{header[7]}. GRIB2 requires an independently validated conversion path."
        )


def run_native_grib1_pair(pressure: Path, surface: Path, destination: Path,
                          converter: Path, *,
                          level_count: int = 13,
                          notify: Callable[[str], None] | None = None) -> Path:
    """Run legacy grib2arl.exe in an isolated working directory.

    HYSPLIT's legacy converter uses local names DATA.GRIB and SFC.GRIB;
    k = number of vertical levels including the surface.
    """
    require_native_windows()
    pressure, surface = Path(pressure), Path(surface)
    converter, destination = Path(converter).resolve(), Path(destination)
    if not converter.is_file() or converter.suffix.lower() != ".exe":
        raise FileNotFoundError(f"Native Windows grib2arl.exe not found: {converter}")
    if not 2 <= level_count <= 60:
        raise ValueError("ARL vertical level count outside 2..60")
    require_grib1(pressure)
    require_grib1(surface)
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_path = destination.with_suffix(destination.suffix + ".log")
    with tempfile.TemporaryDirectory(prefix="hysplit_grib1_", dir=destination.parent) as tmp:
        work = Path(tmp)
        for src, target in [(pressure, work / "DATA.GRIB"),
                            (surface, work / "SFC.GRIB")]:
            try:
                os.link(src, target)
            except OSError:
                shutil.copyfile(src, target)
        args = [
            str(converter), "-iDATA.GRIB", "-sSFC.GRIB", "-g3", "-p1",
            f"-k{level_count}", "-q0", "-z1",
        ]
        if notify:
            notify(f"Windows grib2arl: converting {pressure.name}")
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            try:
                response = subprocess.run(
                    args, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, timeout=3600, check=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise RuntimeError(f"grib2arl execution failed: {exc}; log: {log_path}") from exc
        result = work / "DATA.ARL"
        if response.returncode or not result.is_file() or result.stat().st_size == 0:
            raise RuntimeError(
                f"grib2arl returned {response.returncode} without usable DATA.ARL; log: {log_path}"
            )
        check_signature(result)
        # Staging file is only published after NOAA filedates validation.
        staged = destination.with_suffix(destination.suffix + ".part")
        staged.unlink(missing_ok=True)
        shutil.copyfile(result, staged)
        return staged


def convert_cds_grib1_to_arl(sources: list[Path], target_dir: Path,
                             earliest: datetime, latest: datetime, *,
                             hysplit_executable: Path,
                             level_count: int = 13,
                             notify: Callable[[str], None] | None = None) -> list[Path]:
    """Pair CDS files, convert them on Windows and verify coverage before rename.

    The exact ERA5 GRIB variable set, grid and level configuration must be
    validated for a research dataset. CDS may return GRIB2 or different
    messages; unsupported formats are rejected, not silently interpreted.
    """
    require_native_windows()
    exec_dir = Path(hysplit_executable).resolve().parent
    converter = exec_dir / "grib2arl.exe"
    filedates = exec_dir / "filedates.exe"
    if not converter.is_file():
        raise FileNotFoundError(f"grib2arl.exe is required in HYSPLIT exec directory: {exec_dir}")
    if not filedates.is_file():
        raise FileNotFoundError(f"filedates.exe is required in HYSPLIT exec directory: {exec_dir}")
    pairs = match_cds_pairs(sources)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    finalized: list[Path] = []
    staged: list[tuple[Path, Path]] = []
    for key, (pressure, surface) in pairs.items():
        # Caching key includes file identities and selected level scheme.
        identity = f"{pressure.resolve()}:{pressure.stat().st_size}:{pressure.stat().st_mtime_ns}|" + \
                   f"{surface.resolve()}:{surface.stat().st_size}:{surface.stat().st_mtime_ns}|{level_count}"
        token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        destination = target_dir / f"era5_cds_{key}_{token}.arl"
        if destination.is_file() and destination.stat().st_size > 0:
            finalized.append(destination)
            if notify:
                notify(f"ERA5 converted cache: {destination.name}")
            continue
        intermediate = run_native_grib1_pair(
            pressure, surface, destination, converter, level_count=level_count, notify=notify)
        staged.append((destination, intermediate))
    to_verify = finalized + [temp for _, temp in staged]
    try:
        report = check_coverage(to_verify, earliest, latest, filedates, max_gap_hours=1.01)
        if not report.verified:
            raise RuntimeError("ERA5 ARL time verification failed")
        for dest, tmp in staged:
            tmp.replace(dest)
    except Exception:
        for _dest, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise
    return finalized + [dest for dest, _tmp in staged]
