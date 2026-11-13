"""Sparse meteorology acquisition for a single multi-date trajectory batch.

Files are cached in the usual met/<provider> directory. Every merged required
window is processed independently for CDS/ARCO. NOAA archives are deduplicated
by filename, and per-window file lists avoid passing unrelated datasets to
HYSPLIT. No blank days between distant observations are requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .time_windows import TimeWindow, split_window
from .meteo import download_arl_file, plan_archives, plan_archive_windows, local_arl_files
from .era5 import download_cds, download_google_arco
from .native_era5 import convert_cds_grib1_to_arl
from .arco_convert import convert_arco_netcdf_to_arl
from .progress import TaskProgress


@dataclass
class SparseMeteorology:
    by_window: dict[TimeWindow, tuple[Path, ...]]
    files: tuple[Path, ...]

    def for_window(self, window: TimeWindow) -> tuple[Path, ...]:
        selected = self.by_window[window]
        if not selected:
            raise RuntimeError(f"No meteorology for {window}")
        return selected


def _unique(paths):
    return tuple(dict.fromkeys(Path(x) for x in paths))


def preparation_count(provider: str, windows: tuple[TimeWindow, ...], *,
                      action: str) -> int:
    """Unit count used by the GUI bar: steps, not estimated running time."""
    if provider in ("gdas1", "gfs0p25"):
        return len(plan_archive_windows(provider, windows))
    if provider == "local":
        return 1
    if provider == "era5_cds":
        return len(windows) * (2 if action == "run" else 1)
    if provider == "arco_era5":
        parts = sum(len(split_window(window)) for window in windows)
        return parts * (2 if action == "run" else 1)
    raise ValueError(f"Unsupported meteorology source: {provider}")


def prepare_meteorology(
    provider: str, windows: tuple[TimeWindow, ...], target_dir: Path,
    bbox: tuple[float, float, float, float], *,
    action: str,
    local_met: str,
    hysplit_executable: Path,
    notify: Callable[[str], None],
    progress: TaskProgress,
) -> SparseMeteorology:
    """Acquire and optionally convert the exact disjoint time windows."""
    if not windows:
        raise ValueError("No required meteorological periods")
    if action not in ("download", "run"):
        raise ValueError(f"Unknown batch action {action}")
    by_window: dict[TimeWindow, tuple[Path, ...]] = {}
    if provider in ("gdas1", "gfs0p25"):
        specs = plan_archive_windows(provider, windows)
        notify(
            f"NOAA {provider}: {len(specs)} distinct ARL files for "
            f"{len(windows)} required time windows"
        )
        names: dict[str, Path] = {}
        for spec in specs:
            label = f"NOAA {spec.name}"
            progress.start(label, 0.0)
            def received(done: int, total: int | None, *, _label=label):
                progress.fraction(
                    f"{_label}: {done / 1048576:.1f} MiB"
                    + (f" / {total / 1048576:.1f} MiB" if total else ""),
                    done / total if total else None
                )
            names[spec.name] = download_arl_file(
                spec, target_dir, notify, progress=received
            )
            progress.complete(f"取得済み {spec.name}")
        for window in windows:
            by_window[window] = tuple(
                names[spec.name]
                for spec in plan_archives(provider, window.start, window.end)
            )
    elif provider == "local":
        progress.start("ローカルARLファイルを確認")
        files = _unique(local_arl_files(local_met))
        for window in windows:
            by_window[window] = files
        progress.complete(f"ローカルARL {len(files)}ファイル")
    elif provider == "era5_cds":
        for index, window in enumerate(windows, 1):
            label = f"ERA5 CDS {index}/{len(windows)}"
            progress.start(f"{label}: GRIBをダウンロード中")
            inputs = download_cds(
                window.start, window.end, target_dir, bbox, notify=notify
            )
            progress.complete(f"{label}: GRIB {len(inputs)}ファイル取得")
            if action == "run":
                progress.start(f"{label}: WindowsでARLに変換中")
                outputs = convert_cds_grib1_to_arl(
                    inputs, target_dir / "arl", window.start, window.end,
                    hysplit_executable=hysplit_executable, notify=notify,
                )
                progress.complete(f"{label}: ARL変換完了")
            else:
                outputs = inputs
            by_window[window] = _unique(outputs)
    elif provider == "arco_era5":
        all_parts = [
            (window, part)
            for window in windows
            for part in split_window(window)
        ]
        saved: dict[TimeWindow, list[Path]] = {window: [] for window in windows}
        for index, (window, part) in enumerate(all_parts, 1):
            label = f"Google ARCO ERA5 {index}/{len(all_parts)}"
            progress.start(f"{label}: 地域NetCDFを取得中")
            netcdf = download_google_arco(
                part.start, part.end, target_dir, bbox, notify=notify,
                progress=lambda current, total, _label=label: progress.fraction(
                    f"{_label}: NetCDF書き出し {current}/{total} 時間",
                    current / total if total else None,
                ),
            )
            progress.complete(f"{label}: NetCDF取得完了")
            if action == "run":
                progress.start(f"{label}: WindowsでARLに変換中")
                outputs = convert_arco_netcdf_to_arl(
                    netcdf, target_dir/"arl", part.start, part.end,
                    hysplit_executable=hysplit_executable, notify=notify,
                )
                progress.complete(f"{label}: ARL変換完了")
            else:
                outputs = [netcdf]
            saved[window].extend(outputs)
        by_window = {window: _unique(saved[window]) for window in windows}
    else:
        raise ValueError(f"Unsupported meteorology source: {provider}")
    all_paths = _unique(
        path for window in windows for path in by_window[window]
    )
    return SparseMeteorology(by_window, all_paths)

