"""Sparse meteorology acquisition for a single multi-date trajectory batch.

Files are cached in the usual met/<provider> directory. Every merged required
window is processed independently for CDS/ARCO. NOAA archives are deduplicated
by filename, and per-window file lists avoid passing unrelated datasets to
HYSPLIT. No blank days between distant observations are requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager

from .time_windows import TimeWindow
from .meteo import download_arl_file, plan_archives, plan_archive_windows, local_arl_files
from .era5_materialize import Era5Materializer
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
    if provider in ("era5_cds","arco_era5"):
        return len(windows) * (2 if action=="run" else 1)
    raise ValueError(f"Unsupported meteorology source: {provider}")


def prepare_meteorology(
    provider: str, windows: tuple[TimeWindow, ...], target_dir: Path,
    bbox: tuple[float, float, float, float], *,
    action: str,
    local_met: str,
    hysplit_executable: Path,
    notify: Callable[[str], None],
    progress: TaskProgress,
    era5_field_profile: str = "allvars",
    keep_sources: bool = False,
    on_window_ready: Callable[[TimeWindow, tuple[Path, ...]], None] | None = None,
    conversion_slot: Callable[[], ContextManager[None]] | None = None,
) -> SparseMeteorology:
    """Acquire periods in order and announce each verified window immediately.

    ``on_window_ready`` is called after a window has its complete local ARL
    set (or downloaded files for download-only jobs). A caller can then start
    that period's trajectories while this function fetches the next period.
    """
    if not windows:
        raise ValueError("No required meteorological periods")
    if action not in ("download", "run"):
        raise ValueError(f"Unknown batch action {action}")
    by_window: dict[TimeWindow, tuple[Path, ...]] = {}

    def publish(window: TimeWindow, files: tuple[Path, ...]) -> None:
        if not files:
            raise RuntimeError(f"No meteorology files for {window}")
        by_window[window] = files
        if on_window_ready is not None:
            on_window_ready(window, files)

    if provider in ("gdas1", "gfs0p25"):
        specs = plan_archive_windows(provider, windows)
        notify(
            f"NOAA {provider}: {len(specs)} distinct ARL files for "
            f"{len(windows)} required time windows"
        )
        names: dict[str, Path] = {}
        specs_by_window = {
            window: plan_archives(provider, window.start, window.end)
            for window in windows
        }
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
            for window, required in specs_by_window.items():
                if window not in by_window and all(item.name in names for item in required):
                    publish(window, tuple(names[item.name] for item in required))
        for window in windows:
            if window not in by_window:
                publish(window, tuple(names[spec.name]
                                      for spec in specs_by_window[window]))
    elif provider == "local":
        progress.start("ローカルARLファイルを確認")
        files = _unique(local_arl_files(local_met))
        for window in windows:
            publish(window, files)
        progress.complete(f"ローカルARL {len(files)}ファイル")
    elif provider in ("era5_cds","arco_era5"):
        materializer=Era5Materializer(provider,target_dir,bbox,hysplit_executable,
                                     era5_field_profile,keep_sources,notify,conversion_slot)
        try:
            for index,window in enumerate(windows,1):
                label=f"{provider} {index}/{len(windows)}"
                progress.start(f"{label}: checking local meteorology")
                files=materializer.prepare(
                    window,action=action,
                    progress=lambda current,total,_label=label: progress.fraction(
                        f"{_label}: current NetCDF {current}/{total} hours",
                        current/total if total else None))
                progress.complete(f"{label}: source/ARL ready")
                if action=="run":
                    progress.complete(f"{label}: verified ARL coverage")
                publish(window,_unique(files))
        finally:
            materializer.close()
    else:
        raise ValueError(f"Unsupported meteorology source: {provider}")
    all_paths = _unique(
        path for window in windows for path in by_window[window]
    )
    return SparseMeteorology(by_window, all_paths)

