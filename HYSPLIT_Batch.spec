# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 portable HYSPLIT Batch GUI, without NOAA model executables.

PyInstaller builds all Python, tkinter and ERA5 dependencies into an onedir
application. NOAA HYSPLIT (hyts_std.exe, grib2arl.exe, filedates.exe, bdyfiles)
MUST be installed separately and is NOT bundled due to NOAA restrictions.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).resolve()
datas = []
binaries = []
hiddenimports = [
    "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
    "numpy", "xarray", "xarray.backends.netCDF4_", "netCDF4",
    "eccodes", "gcsfs", "fsspec", "zarr", "numcodecs", "cdsapi",
]

# These packages are imported dynamically in meteorology adapters.
# Include native DLLs and Zarr codec/provider registrations as well.
for package in (
    "eccodes", "netCDF4", "gcsfs", "fsspec", "zarr", "numcodecs", "cdsapi",
):
    data_files, dll_files, mods = collect_all(package)
    datas.extend(data_files)
    binaries.extend(dll_files)
    hiddenimports.extend(mods)

for package in (
    "xarray.backends", "zarr.codecs", "numcodecs", "google.auth",
    "google.cloud.storage", "google.api_core", "google.resumable_media",
):
    hiddenimports.extend(collect_submodules(package, on_error="warn"))

a = Analysis(
    [str(root / "run_gui.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "tensorflow", "tensorflow_probability", "jax",
        "matplotlib", "seaborn", "cv2", "bokeh",
        "jupyter", "pytest", "IPython", "sympy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HYSPLIT_Batch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HYSPLIT_Batch",
)

