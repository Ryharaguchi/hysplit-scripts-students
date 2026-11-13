"""Portable Windows application paths and standalone EXE diagnostics.

HYSPLIT binaries are *not* bundled; NOAA requires separate download and
express permission for redistribution. Detect a legitimate local install.
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path


def application_dir() -> Path:
    """Location of the distributable EXE, or source repository for Python."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def hysplit_candidates() -> tuple[Path, ...]:
    """Detect independently installed HYSPLIT without fixed usernames or drives."""
    exe_dir = application_dir()
    home = Path.home()
    candidates: list[Path] = []

    for name in ("HYSPLIT_EXE", "HYSPLIT_HOME"):
        raw = os.environ.get(name, "").strip().strip('"')
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        candidates.extend((
            candidate,
            candidate / "hyts_std.exe",
            candidate / "exec" / "hyts_std.exe",
        ))

    # These are optional user-provided installations, not model distributions.
    candidates.extend((
        exe_dir / "HYSPLIT" / "exec" / "hyts_std.exe",
        exe_dir / "hysplit" / "exec" / "hyts_std.exe",
        exe_dir.parent / "HYSPLIT" / "exec" / "hyts_std.exe",
        Path(r"C:\hysplit\exec\hyts_std.exe"),
        Path(r"C:\Program Files\HYSPLIT\exec\hyts_std.exe"),
        home / "HYSPLIT" / "exec" / "hyts_std.exe",
        home / "Desktop" / "backwardtrajectory" / "HYSPLIT" / "exec" / "hyts_std.exe",
        home / "Documents" / "HYSPLIT" / "exec" / "hyts_std.exe",
    ))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def find_hysplit_executable() -> Path:
    for candidate in hysplit_candidates():
        if candidate.is_file() and candidate.name.lower() == "hyts_std.exe":
            return candidate
    return Path(r"C:\hysplit\exec\hyts_std.exe")


def windows_desktop_dir() -> Path:
    """Resolve the Windows user's real Desktop, including OneDrive redirection.

    Read Windows' per-user known-folder registry setting. The fallback is
    USERPROFILE/Desktop if the registry is unavailable on this PC.
    """
    if os.name == "nt":
        try:
            import winreg

            key_path = (
                r"Software\Microsoft\Windows\CurrentVersion"
                r"\Explorer\User Shell Folders"
            )
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                desktop, _kind = winreg.QueryValueEx(key, "Desktop")
            if desktop:
                path = Path(os.path.expandvars(desktop)).expanduser()
                if path.is_dir():
                    return path
        except (OSError, ValueError):
            pass
    return Path.home() / "Desktop"


def default_work_root() -> Path:
    """Run on the other PC's Desktop, never on a Google Drive virtual disk.

    A portable distribution can live on H: for transfer, but HYSPLIT
    calculations and multi-gigabyte meteorology caches belong on local disk.
    """
    return windows_desktop_dir() / "HYSPLIT_Work"


_REQUIRED_PACKAGES = (
    "numpy", "xarray", "netCDF4", "eccodes", "zarr", "gcsfs",
    "fsspec", "cdsapi",
)


def verify_frozen_grib_backend() -> dict[str, object]:
    """Exercise actual ecCodes/CFFI/pycparser dynamic imports in a frozen EXE.

    Merely importing the eccodes top-level package is insufficient; GRIB1
    creation is the path used by ARCO NetCDF conversion and loads CFFI's
    fallback pycparser module out of the PyInstaller PYZ archive.
    """
    import cffi
    import pycparser
    import eccodes

    parser = pycparser.CParser()
    parser.parse("typedef unsigned int hysplit_test_type;")
    ffi = cffi.FFI()
    ffi.cdef("typedef unsigned int hysplit_test_type;")
    sample = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib1")
    if sample is None:
        raise RuntimeError("ecCodes GRIB1 sample could not be loaded")
    try:
        message = bytes(eccodes.codes_get_message(sample))
    finally:
        eccodes.codes_release(sample)
    if not message.startswith(b"GRIB") or message[7] != 1:
        raise RuntimeError("ecCodes GRIB1 sample returned invalid message")
    return {
        "ok": True,
        "cffi_version": getattr(cffi, "__version__", "unknown"),
        "pycparser_version": getattr(pycparser, "__version__", "unknown"),
        "grib1_sample_bytes": len(message),
    }


def standalone_diagnostics(report_path: Path) -> bool:
    """Check the distribution without executing a model or accessing network.

    The GUI build is windowed; write diagnostic JSON instead of stdout.
    A missing HYSPLIT installation is a *separate prerequisite*, not an EXE
    packaging defect.
    """
    issues: list[str] = []
    packages: dict[str, dict[str, str | bool]] = {}
    for name in _REQUIRED_PACKAGES:
        try:
            loaded = importlib.import_module(name)
            packages[name] = {
                "ok": True,
                "version": str(getattr(loaded, "__version__", "not reported")),
            }
        except Exception as exc:
            packages[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            issues.append(f"{name}: {exc}")

    # Verify the precise native GRIB backend used *after* NetCDF acquisition.
    # This catches broken/missing modules in a transferred onedir distribution.
    backend = {"ok": False, "skipped": True}
    if "eccodes" in _REQUIRED_PACKAGES:
        try:
            backend = verify_frozen_grib_backend()
        except Exception as exc:
            backend = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            issues.append(
                "ecCodes/CFFI/pycparser GRIB1 conversion backend: "
                f"{type(exc).__name__}: {exc}"
            )

    gui_ok = False
    try:
        from .gui import App
        gui = App()
        gui.withdraw()
        gui_ok = True
        gui.destroy()
    except Exception as exc:
        issues.append(f"Tkinter GUI: {type(exc).__name__}: {exc}")

    model = find_hysplit_executable()
    bdy = model.parent.parent / "bdyfiles"
    data = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "executable_path": str(Path(sys.executable).resolve()),
        "frozen": bool(getattr(sys, "frozen", False)),
        "application_dir": str(application_dir()),
        "default_work_root": str(default_work_root()),
        "hysplit_executable": str(model),
        "hysplit_executable_found": model.is_file(),
        "hysplit_bdyfiles_found": bdy.is_dir(),
        "gui_loaded": gui_ok,
        "libraries": packages,
        "grib1_backend": backend,
        "packaging_passed": not issues,
        "errors": issues,
        "hysplit_note": "Install NOAA HYSPLIT separately on each PC; it is not redistributed.",
    }
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return not issues

