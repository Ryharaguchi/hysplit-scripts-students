"""Windows-native ERA5 CDS GRIB1 -> HYSPLIT ARL conversion.

Uses the native Windows grib2arl.exe shipped with HYSPLIT. Does not invoke
WSL, NOAAs online READY calculation, or a user-selected converter.
Only input GRIB edition 1 is accepted. NOAA's filedates.exe must confirm
model-hour coverage before any ARL file is published.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from .era5 import PRESSURE_LEVELS
from .platform_guard import require_native_windows
from .validation import check_coverage, check_signature, parse_filedates

_PAIR = re.compile(r"^era5_(pressure|single)_(\d{6})_(.+)\.grib$", re.IGNORECASE)


def _grib2arl_is_running() -> bool:
    """Return True when process state is unknown or grib2arl may be active."""
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq grib2arl.exe", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A failed process check must never authorize deleting converter files.
        return True
    if result.returncode:
        return True
    return "grib2arl.exe" in result.stdout.lower()


def cleanup_abandoned_converter_workspaces(
    arl_dir: Path, *, notify: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Remove converter scratch folders left by a stopped/failed app run.

    Only immediate children created by this converter are considered. If any
    grib2arl process is running (or process status cannot be read), cleanup is
    skipped to protect an active conversion.
    """
    arl_dir = Path(arl_dir)
    if _grib2arl_is_running() or not arl_dir.is_dir():
        return 0, 0

    removed = 0
    freed = 0
    for work in arl_dir.glob("hysplit_grib1_*"):
        if not work.is_dir() or work.is_symlink():
            continue
        try:
            size = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
            shutil.rmtree(work)
        except OSError as exc:
            if notify:
                notify(f"Converter scratch retained (cleanup failed): {work.name}: {exc}")
            continue
        removed += 1
        freed += size

    if removed and notify:
        notify(
            f"Abandoned ERA5 converter scratch removed: {removed} folders, "
            f"{freed / (1024 ** 3):.2f} GiB freed"
        )
    return removed, freed


def cleanup_verified_cds_gribs(
    arl_dir: Path, source_dir: Path, filedates: Path, *,
    notify: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Finish GRIB cleanup after a prior run stopped after ARL publication."""
    arl_dir, source_dir, filedates = Path(arl_dir), Path(source_dir), Path(filedates)
    if _grib2arl_is_running():
        if notify:
            notify("CDS GRIB cleanup skipped because grib2arl is still running")
        return 0, 0
    root = source_dir.resolve()
    removed = 0
    freed = 0
    for metadata_path in arl_dir.glob("era5_cds_*.json"):
        output = metadata_path.with_suffix(".arl")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            paths = [Path(value).resolve() for value in metadata["source_grib_files"]]
            if (not output.is_file() or output.stat().st_size == 0
                    or not paths):
                continue
            if any(path.parent != root or path.suffix.lower() != ".grib"
                   or not path.name.lower().startswith(("era5_pressure_", "era5_single_"))
                   for path in paths):
                continue
            if not any(path.is_file() for path in paths):
                continue
            scan = subprocess.run(
                [str(filedates.resolve()), output.name], cwd=output.parent,
                capture_output=True, text=True, errors="replace",
            )
            if scan.returncode:
                continue
            times = parse_filedates(scan.stdout)
            if not times:
                continue
            report = check_coverage(
                [output], times[0], times[-1], filedates, max_gap_hours=1.01,
            )
            if not report.verified:
                continue
            for path in paths:
                if path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    freed += size
        except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                subprocess.SubprocessError):
            continue
    if removed and notify:
        notify(
            f"Verified CDS conversion intermediates removed: {removed} GRIB files, "
            f"{freed / (1024 ** 3):.2f} GiB freed"
        )
    return removed, freed


def _probe_hardlink(directory: Path) -> bool:
    """Check whether tiny same-directory hardlinks are available."""
    source = target = None
    try:
        fd, name = tempfile.mkstemp(prefix=".hysplit_link_check_", dir=directory)
        os.close(fd)
        source = Path(name)
        target = source.with_name(source.name + ".link")
        os.link(source, target)
        return True
    except OSError:
        return False
    finally:
        for path in (target, source):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def check_arco_conversion_space(estimated_grib_bytes: int, destination: Path,
                               notify: Callable[[str], None] | None = None, *,
                               expected_arl_bytes: int | None = None) -> None:
    """Check peak free space before ARCO creates its large intermediate GRIBs.

    The peak includes both GRIB files and one ARL. The converter output is
    moved to its staging name on the same volume, avoiding a second full ARL
    copy. If hardlinks are unavailable, a second GRIB pair is also needed.
    Existing ARLs are already reflected in current free space and remain intact.
    """
    if estimated_grib_bytes < 1:
        raise ValueError("Estimated ARCO GRIB size must be positive")
    work_dir = Path(destination).parent
    # Other cached periods may be much longer and are not estimates for
    # this conversion. When known, use the requested ARL record count.
    estimated_arl = expected_arl_bytes if expected_arl_bytes is not None else max(1,estimated_grib_bytes//2)
    copied_inputs = 0 if _probe_hardlink(work_dir) else estimated_grib_bytes
    required = estimated_grib_bytes + estimated_arl + copied_inputs
    available = shutil.disk_usage(work_dir).free
    if notify:
        notify(
            "ARCO conversion disk preflight: "
            f"free={available / (1024 ** 3):.2f} GiB, "
            f"estimated peak needed={required / (1024 ** 3):.2f} GiB"
        )
    if available < required:
        raise OSError(
            "Not enough free disk space to start ARCO NetCDF→GRIB1→ARL conversion: "
            f"available {available / (1024 ** 3):.2f} GiB, "
            f"estimated peak minimum {required / (1024 ** 3):.2f} GiB. "
            "No conversion was started; the source NetCDF and existing ARLs were preserved. "
            "Free space or choose a work drive with more capacity, then retry."
        )


def _check_conversion_space(pressure: Path, surface: Path, destination: Path,
                            notify: Callable[[str], None] | None = None, *,
                            expected_arl_bytes: int | None = None) -> None:
    """Avoid starting grib2arl when one output ARL and required copies cannot fit."""
    work_dir = destination.parent
    pressure_size, surface_size = pressure.stat().st_size, surface.stat().st_size
    input_size = pressure_size + surface_size
    estimated_arl = expected_arl_bytes if expected_arl_bytes is not None else max(1,input_size//2)
    hardlinks_available = (
        pressure.stat().st_dev == work_dir.stat().st_dev
        and surface.stat().st_dev == work_dir.stat().st_dev
        and _probe_hardlink(work_dir)
    )
    copied_inputs = 0 if hardlinks_available else input_size
    required = estimated_arl + copied_inputs
    available = shutil.disk_usage(work_dir).free
    if notify:
        notify(
            "ERA5 conversion disk check: "
            f"free={available / (1024 ** 3):.2f} GiB, "
            f"estimated peak needed={required / (1024 ** 3):.2f} GiB"
        )
    if available < required:
        raise OSError(
            "Not enough free disk space to start ERA5 GRIB1→ARL conversion: "
            f"available {available / (1024 ** 3):.2f} GiB, "
            f"estimated minimum {required / (1024 ** 3):.2f} GiB. "
            "The source GRIB and existing ARL files were preserved. Free space "
            "or choose a work drive with more capacity, then restart."
        )


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


def _converter_messages(work: Path, *, include_config: bool) -> tuple[str, tuple[str, ...]]:
    """Collect small diagnostics before TemporaryDirectory removes converter files."""
    message_path = work / "MESSAGE"
    message_text = ""
    if message_path.is_file():
        try:
            message_text = message_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    warnings: list[str] = []
    lines = message_text.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"\b(WARNING|ERROR|FATAL)\b", line, re.IGNORECASE):
            warnings.append(line.strip())
            if index + 1 < len(lines) and not re.search(
                r"\b(WARNING|ERROR|FATAL)\b", lines[index + 1], re.IGNORECASE
            ):
                warnings.append(lines[index + 1].strip())

    details = []
    if warnings:
        details.append("grib2arl MESSAGE warnings/errors:")
        details.extend(f"  {line}" for line in warnings[:40])
    if include_config:
        config_path = work / "CFG_ARL"
        if config_path.is_file():
            try:
                details.extend(("CFG_ARL at failure:", config_path.read_text(
                    encoding="utf-8", errors="replace"
                ).rstrip()))
            except OSError as exc:
                details.append(f"Could not read CFG_ARL: {exc}")
    return "\n".join(details), tuple(warnings)


def run_native_grib1_pair(pressure: Path, surface: Path, destination: Path,
                          converter: Path, *,
                          level_count: int = len(PRESSURE_LEVELS) + 1,
                          notify: Callable[[str], None] | None = None,
                          expected_arl_bytes: int | None = None) -> Path:
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
    staged = destination.with_suffix(destination.suffix + ".part")
    if staged.exists():
        raise RuntimeError(
            f"An unverified staged ARL already exists and was preserved: {staged}. "
            "The caller must check it with filedates before another conversion."
        )
    args = [
        str(converter), "-iDATA.GRIB", "-sSFC.GRIB", "-g3", "-p1",
        f"-k{level_count}", "-q0", "-z1",
    ]
    if notify:
        notify(f"Windows grib2arl: converting {pressure.name}")

    # The Windows converter can intermittently fail while rereading its own
    # CFG_ARL. Retry that specific Fortran input-conversion failure once in a
    # fresh temporary directory; do not repeat unrelated conversion errors.
    for attempt in (1, 2):
        _check_conversion_space(pressure, surface, destination, notify,
                                expected_arl_bytes=expected_arl_bytes)
        work_path: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="hysplit_grib1_", dir=destination.parent) as tmp:
                work = Path(tmp)
                work_path = work
                for src, target in [(pressure, work / "DATA.GRIB"),
                                    (surface, work / "SFC.GRIB")]:
                    try:
                        os.link(src, target)
                    except OSError:
                        shutil.copyfile(src, target)

                mode = "w" if attempt == 1 else "a"
                with log_path.open(mode, encoding="utf-8", errors="replace") as log:
                    log.write(f"\n=== grib2arl attempt {attempt}/2 ===\n")
                    process = None
                    try:
                        process = subprocess.Popen(
                            args, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,
                        )
                        last_report = time.monotonic()
                        while process.poll() is None:
                            try:
                                process.wait(timeout=20)
                            except subprocess.TimeoutExpired:
                                now = time.monotonic()
                                if notify and now - last_report >= 60:
                                    data_arl = work / "DATA.ARL"
                                    size = (
                                        data_arl.stat().st_size
                                        if data_arl.is_file() else 0
                                    )
                                    notify(
                                        "grib2arl is still converting; temporary "
                                        f"DATA.ARL={size / (1024 ** 3):.2f} GiB"
                                    )
                                    last_report = now
                        response_code = process.returncode
                    except OSError as exc:
                        diagnostic, _warnings = _converter_messages(work, include_config=True)
                        if diagnostic:
                            log.write(f"\n{diagnostic}\n")
                        raise RuntimeError(
                            f"grib2arl execution failed: {exc}; log: {log_path}"
                        ) from exc
                    except BaseException:
                        if process is not None and process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                        raise

                result = work / "DATA.ARL"
                if not response_code and result.is_file() and result.stat().st_size > 0:
                    diagnostic, warnings = _converter_messages(work, include_config=False)
                    if diagnostic:
                        with log_path.open("a", encoding="utf-8", errors="replace") as log:
                            log.write(f"\n{diagnostic}\n")
                        if notify:
                            notify("ERA5 conversion warning: " + " | ".join(warnings[:4]))
                    check_signature(result)
                    # Staging file is only published after NOAA filedates validation.
                    # The work directory is created under destination.parent,
                    # so this atomic rename stays on one volume and uses no
                    # second full-sized copy of the converted ARL.
                    os.replace(result, staged)
                    return staged

                output = log_path.read_text(encoding="utf-8", errors="replace")
                retryable_cfg_error = (
                    response_code == 64
                    and "forrtl: severe (64): input conversion error" in output.lower()
                    and "cfg_arl" in output.lower()
                )
                diagnostic, _warnings = _converter_messages(work, include_config=True)
                if diagnostic:
                    with log_path.open("a", encoding="utf-8", errors="replace") as log:
                        log.write(f"\n{diagnostic}\n")
                if retryable_cfg_error and attempt == 1:
                    if notify:
                        notify("grib2arlのCFG_ARL読込エラーを検出。新しい一時領域で1回再試行します。")
                else:
                    raise RuntimeError(
                        f"grib2arl returned {response_code} without usable DATA.ARL; log: {log_path}"
                    )
        finally:
            # TemporaryDirectory normally removes this itself. Retry explicitly
            # in case a disk-full or interrupted conversion left scratch behind.
            if work_path is not None and work_path.exists():
                try:
                    shutil.rmtree(work_path)
                except OSError as exc:
                    if notify:
                        notify(
                            f"ERA5 converter scratch could not be removed automatically: "
                            f"{work_path.name}: {exc}"
                        )
        if retryable_cfg_error and attempt == 1:
            continue

    raise RuntimeError(f"grib2arl conversion failed without usable DATA.ARL; log: {log_path}")


def convert_cds_grib1_to_arl(sources: list[Path], target_dir: Path,
                             earliest: datetime, latest: datetime, *,
                             hysplit_executable: Path,
                             level_count: int = len(PRESSURE_LEVELS) + 1,
                             keep_sources: bool = False,
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
    preserved_stages: set[Path] = set()
    sources_by_output: dict[Path, tuple[Path, Path]] = {}
    for key, (pressure, surface) in pairs.items():
        # Caching key includes file identities and selected level scheme.
        identity = f"{pressure.resolve()}:{pressure.stat().st_size}:{pressure.stat().st_mtime_ns}|" + \
                   f"{surface.resolve()}:{surface.stat().st_size}:{surface.stat().st_mtime_ns}|{level_count}"
        token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        destination = target_dir / f"era5_cds_{key}_{token}.arl"
        sources_by_output[destination] = (pressure, surface)
        if destination.is_file() and destination.stat().st_size > 0:
            finalized.append(destination)
            if notify:
                notify(f"ERA5 converted cache: {destination.name}")
            continue
        intermediate = destination.with_suffix(destination.suffix + ".part")
        if intermediate.exists():
            check_signature(intermediate)
            preserved_stages.add(intermediate)
            if notify:
                notify(
                    f"Reusing staged ERA5 ARL for filedates verification: "
                    f"{intermediate.name}"
                )
        else:
            intermediate = run_native_grib1_pair(
                pressure, surface, destination, converter,
                level_count=level_count, notify=notify,
            )
        staged.append((destination, intermediate))
    to_verify = finalized + [temp for _, temp in staged]
    try:
        report = check_coverage(to_verify, earliest, latest, filedates, max_gap_hours=1.01)
        if not report.verified:
            raise RuntimeError("ERA5 ARL time verification failed")
        for dest, tmp in staged:
            tmp.replace(dest)
        cache_id = hashlib.sha256((
            f"{earliest.isoformat()}|{latest.isoformat()}|{level_count}|"
            + "|".join(sorted(
                source.name for pair in sources_by_output.values() for source in pair
            ))
        ).encode("utf-8")).hexdigest()[:20]
        field_profile = (
            "hysplit" if all(
                source.name.lower().endswith("_hysplit.grib")
                for pair in sources_by_output.values() for source in pair
            ) else "allvars"
        )
        # Save provenance first; if cleanup is interrupted, the next app run
        # can validate this ARL again before removing the same source GRIBs.
        for dest, (pressure, surface) in sources_by_output.items():
            metadata = {
                "cache_id": cache_id,
                "requested_start": earliest.isoformat(),
                "requested_end": latest.isoformat(),
                "pair_count": len(pairs),
                "pressure_levels_hpa": list(PRESSURE_LEVELS),
                "field_profile": field_profile,
                "source_grib_files": [str(pressure.resolve()), str(surface.resolve())],
                "first": report.first.isoformat() if report.first else earliest.isoformat(),
                "last": report.last.isoformat() if report.last else latest.isoformat(),
                "time_count": report.time_count,
                "validation": "NOAA Windows filedates (timestamps only; not physical validation)",
            }
            dest.with_suffix(".json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception:
        for _dest, tmp in staged:
            if tmp not in preserved_stages:
                tmp.unlink(missing_ok=True)
        raise
    if keep_sources:
        if notify:
            notify("Verified CDS source GRIB files retained by setting")
    else:
        removed_bytes = 0
        removed_count = 0
        for source in dict.fromkeys(Path(path) for path in sources):
            try:
                if source.is_file():
                    removed_bytes += source.stat().st_size
                    source.unlink()
                    removed_count += 1
            except OSError as exc:
                if notify:
                    notify(
                        f"Converted ERA5 GRIB retained (cleanup failed): "
                        f"{source.name}: {exc}"
                    )
        if removed_count and notify:
            notify(
                f"Verified CDS conversion intermediates removed: {removed_count} GRIB files, "
                f"{removed_bytes / (1024 ** 3):.2f} GiB freed"
            )
    return finalized + [dest for dest, _tmp in staged]
