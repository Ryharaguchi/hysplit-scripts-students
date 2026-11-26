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
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .era5 import PRESSURE_LEVELS
from .platform_guard import require_native_windows
from .validation import check_signature



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
    # Keep the converter's text files verbatim (no warning-line limit).
    for name in ("MESSAGE", "WARNING", "CFG_ARL"):
        if name == "CFG_ARL" and not include_config:
            continue
        path = work / name
        if name == "MESSAGE":
            contents = message_text
        elif path.is_file():
            try:
                contents = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                contents = f"Could not read {name}: {exc}"
        else:
            contents = ""
        details.extend((f"--- {name} ---", contents or "(not created or empty)"))
    return "\n".join(details), tuple(warnings)


def _grib_inventory(pressure, surface):
    """Failure-only header scan. No raw data copies or new diagnostic files."""
    from .grib_catalog import scan_grib, UPPER_KEYS, SURFACE_KEYS
    from datetime import timedelta
    lines = ["--- Actual GRIB1 input inventory (valid UTC; level hPa, paramId) ---"]
    by_file = []
    for path, keys in ((pressure, UPPER_KEYS), (surface, SURFACE_KEYS)):
        groups = defaultdict(Counter)
        try:
            records = scan_grib(path)
            for m in records:
                groups[m.when][m.field] += 1
            lines.append(f"{path}: {len(records)} indexed messages, {len(groups)} UTC hours")
            lines.append(f"  grids={sorted(set(m.grid for m in records))}")
            lines.append(f"  levels={sorted(set(m.field[0] for m in records), reverse=True)}; "
                         f"paramIds={sorted(set(m.field[1] for m in records))}")
            if records:
                lines.append(f"  byte offsets {min(m.offset for m in records)}.."
                             f"{max(m.offset+m.size for m in records)} / {path.stat().st_size}")
            if groups:
                lines.append(f"  valid UTC {min(groups).isoformat()} .. {max(groups).isoformat()}")
            by_file.append((path, set(keys), groups))
        except Exception as exc:
            # Diagnostic failure must not hide the original converter error.
            lines.append(f"{path}: header scan failed: {type(exc).__name__}: {exc}")
    hours = sorted(set(t for _, _, groups in by_file for t in groups))
    for a, b in zip(hours, hours[1:]):
        if b - a != timedelta(hours=1):
            lines.append(f"  Gap between {a.isoformat()} and {b.isoformat()}")
    for path, keys, groups in by_file:
        complete = 0
        for t in hours:
            fields = groups.get(t, Counter())
            missing = sorted(keys - fields.keys())
            duplicates = sorted((k, n) for k, n in fields.items() if n > 1)
            if missing or duplicates:
                lines.append(f"  {t.isoformat()} {path.name}: {sum(fields.values())} records; "
                             f"missing={missing}; duplicates={duplicates}")
            else:
                complete += 1
        lines.append(f"  {path.name}: {complete}/{len(hours)} hours with all required fields and no duplicates")
    return "\n".join(lines)


def _append_log(path, text):
    with path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(text.rstrip() + "\n")


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
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_path = destination.with_suffix(destination.suffix + ".log")
    _append_log(log_path, f"\n=== HYSPLIT Batch {__version__}: {datetime.now(timezone.utc).isoformat()} ===")
    try:
        if not converter.is_file() or converter.suffix.lower() != ".exe":
            raise FileNotFoundError(f"Native Windows grib2arl.exe not found: {converter}")
        if level_count < 2:
            raise ValueError("ARL requires surface and at least one upper level")
        _append_log(log_path, f"Converter: {converter}; SHA256={hashlib.sha256(converter.read_bytes()).hexdigest()}")
        for path in (pressure, surface):
            require_grib1(path)
            _append_log(log_path, f"Input GRIB1: {path}; bytes={path.stat().st_size}")
    except (OSError, ValueError) as exc:
        _append_log(log_path, f"Preflight failed: {type(exc).__name__}: {exc}")
        raise
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
    _append_log(log_path, f"Command: {subprocess.list2cmdline(args)}")
    if notify:
        notify(f"Windows grib2arl: converting {pressure.name}")

    # The Windows converter can intermittently fail while rereading its own
    # CFG_ARL. Retry that specific Fortran input-conversion failure once in a
    # fresh temporary directory; do not repeat unrelated conversion errors.
    for attempt in (1, 2):
        _append_log(log_path, f"Attempt {attempt}/2: {datetime.now(timezone.utc).isoformat()}; "
                    f"disk free bytes={shutil.disk_usage(destination.parent).free}; "
                    f"expected ARL bytes={expected_arl_bytes}")
        try:
            _check_conversion_space(pressure, surface, destination, notify,
                                    expected_arl_bytes=expected_arl_bytes)
        except OSError as exc:
            _append_log(log_path, f"Disk preflight failed: {exc}")
            raise
        work_path: Path | None = None
        started = time.monotonic()
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

                with log_path.open("a", encoding="utf-8", errors="replace") as log:
                    log.write(f"\n=== grib2arl attempt {attempt}/2 ===\n")
                    log.flush()
                    stdout_start = log_path.stat().st_size
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
                        log.write(f"\nLaunch error: {exc}\n{diagnostic}\n"
                                  f"{_grib_inventory(pressure, surface)}\n")
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
                result_bytes = result.stat().st_size if result.is_file() else 0
                _append_log(log_path, f"Exit code={response_code}; elapsed={time.monotonic()-started:.2f}s; "
                            f"ARL bytes={result_bytes}")
                if not response_code and result_bytes > 0:
                    diagnostic, warnings = _converter_messages(work, include_config=True)
                    _append_log(log_path, diagnostic)
                    if notify and warnings:
                        notify("ERA5 conversion warning: " + " | ".join(warnings[:4]))
                    check_signature(result)
                    # Staging file is only published after NOAA filedates validation.
                    # The work directory is created under destination.parent,
                    # so this atomic rename stays on one volume and uses no
                    # second full-sized copy of the converted ARL.
                    os.replace(result, staged)
                    return staged

                # Do not mistake an older appended error for this attempt.
                with log_path.open("rb") as f:
                    f.seek(stdout_start)
                    output = f.read().decode("utf-8", errors="replace")
                retryable_cfg_error = (
                    response_code == 64
                    and "forrtl: severe (64): input conversion error" in output.lower()
                    and "cfg_arl" in output.lower()
                )
                diagnostic, _warnings = _converter_messages(work, include_config=True)
                _append_log(log_path, diagnostic)
                _append_log(log_path, _grib_inventory(pressure, surface))
                if retryable_cfg_error and attempt == 1:
                    _append_log(log_path, "Retrying CFG_ARL read failure in a fresh temporary directory.")
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
