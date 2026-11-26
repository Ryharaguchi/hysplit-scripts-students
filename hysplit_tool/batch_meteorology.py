"""Sparse meteorology acquisition for a single multi-date trajectory batch.

Files are cached in the usual met/<provider> directory. Every merged required
window is processed independently for CDS/ARCO. NOAA archives are deduplicated
by filename, and per-window file lists avoid passing unrelated datasets to
HYSPLIT. No blank days between distant observations are requested.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, ContextManager

from .time_windows import TimeWindow, split_window
from .meteo import download_arl_file, plan_archives, plan_archive_windows, local_arl_files
from .era5 import download_cds, download_google_arco
from .native_era5 import (
    cleanup_abandoned_converter_workspaces,
    cleanup_verified_cds_gribs,
    convert_cds_grib1_to_arl,
)
from .arco_convert import (
    has_current_arco_conversion_policy,
    cleanup_verified_hysplit_netcdf_sources,
    convert_arco_netcdf_to_arl,
)
from .progress import TaskProgress
from .validation import check_coverage, read_arl_times


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


def _read_sidecar(output: Path) -> dict:
    sidecar = output.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _profile_from_arco_metadata(metadata: dict) -> str | None:
    source = Path(str(metadata.get("source", ""))).name.lower()
    if source.endswith("_northern37_hysplit.nc"):
        return "hysplit"
    if source.endswith("_northern37_allvars.nc"):
        return "allvars"
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _select_verified_cache(candidates, label: str, notify=None):
    """Select one verified cache, accepting duplicates only when byte-identical."""
    if not candidates:
        return ()
    if len(candidates) == 1:
        return candidates[0][0]

    sizes = [tuple(path.stat().st_size for path in paths) for paths, _, _ in candidates]
    signatures = None
    comparison_error = None
    if all(size == sizes[0] for size in sizes[1:]):
        if notify:
            notify(
                f"Multiple time-verified {label} ARL caches cover this period; "
                "comparing their exact file contents by SHA-256 (read-only)."
            )
        try:
            signatures = [
                tuple(_sha256_file(path) for path in paths)
                for paths, _, _ in candidates
            ]
        except OSError as exc:
            comparison_error = f"{type(exc).__name__}: {exc}"
        if signatures and all(signature == signatures[0] for signature in signatures[1:]):
            keep_index = max(
                range(len(candidates)),
                key=lambda index: max(
                    path.stat().st_mtime_ns for path in candidates[index][0]
                ),
            )
            removed = []
            cleanup_failures = []
            for index, (paths, _, _) in enumerate(candidates):
                if index == keep_index:
                    continue
                for path in paths:
                    try:
                        size = path.stat().st_size
                        path.unlink()
                        removed.append((path.name, size))
                    except OSError as exc:
                        cleanup_failures.append(f"{path.name}: {exc}")
                        continue
                    for auxiliary in (
                        path.with_suffix(".json"),
                        path.with_suffix(".arl.log"),
                    ):
                        try:
                            if auxiliary.is_file():
                                size = auxiliary.stat().st_size
                                auxiliary.unlink()
                                removed.append((auxiliary.name, size))
                        except OSError as exc:
                            cleanup_failures.append(f"{auxiliary.name}: {exc}")
            if notify:
                removed_bytes = sum(size for _, size in removed)
                notify(
                    f"The {label} ARL caches are byte-identical "
                    f"(SHA-256 {signatures[keep_index][0][:16]}). Reusing "
                    f"{', '.join(path.name for path in candidates[keep_index][0])}; "
                    f"removed {len(removed)} redundant ARL/sidecar/log files "
                    f"({removed_bytes / (1024 ** 3):.2f} GiB)."
                )
                for failure in cleanup_failures:
                    notify(f"Could not remove an identical ARL duplicate: {failure}")
            return candidates[keep_index][0]

    descriptions = []
    for index, (paths, profile, metadata) in enumerate(candidates):
        names = ", ".join(path.name for path in paths)
        source = Path(str(metadata.get("source", ""))).name
        if not source:
            source_gribs = metadata.get("source_grib_files", ())
            if not isinstance(source_gribs, (list, tuple)):
                source_gribs = ()
            source = ", ".join(
                Path(str(item)).name
                for item in source_gribs
                if isinstance(item, str)
            )
        details = []
        if profile:
            details.append(f"JSON profile={profile}")
        if source:
            details.append(f"source={source}")
        if signatures:
            details.append(
                "SHA-256=" + ",".join(value[:16] for value in signatures[index])
            )
        else:
            details.append(
                "bytes=" + ",".join(str(path.stat().st_size) for path in paths)
            )
        descriptions.append(names + (f" ({'; '.join(details)})" if details else ""))
    reason = (
        f" The file contents could not be compared: {comparison_error}."
        if comparison_error else
        " The files have different byte contents."
    )
    raise RuntimeError(
        f"Multiple time-verified {label} ARL caches cover this period: "
        f"{descriptions}.{reason} Both files were preserved; the app did not "
        "select a cache from sidecar JSON, overwrite either file, or start a "
        "new download/conversion. Choose the intended ARL explicitly or move "
        "the unwanted cache aside."
    )


def _verified_arco_arl_cache(
    arl_dir: Path, window: TimeWindow, hysplit_executable: Path,
    field_profile: str = "hysplit",
    notify: Callable[[str], None] | None = None,
) -> tuple[Path, ...]:
    """Reuse any time-verified ARCO ARL; sidecar policy is informational only."""
    filedates = Path(hysplit_executable).resolve().parent / "filedates.exe"
    if not filedates.is_file():
        raise RuntimeError(
            f"Cannot verify or reuse existing ERA5 ARL files because filedates.exe "
            f"is missing: {filedates}. No new ERA5 download was started."
        )

    candidates = []
    related_failures = []
    unverifiable = []
    for output in arl_dir.glob("arco_era5_*.arl"):
        if not output.is_file() or output.stat().st_size == 0:
            continue
        metadata = _read_sidecar(output)
        try:
            report = check_coverage(
                [output], window.start, window.end, filedates, max_gap_hours=1.01,
            )
            if report.verified:
                candidates.append((
                    (output,), _profile_from_arco_metadata(metadata), metadata,
                ))
                continue
        except Exception:
            pass
        try:
            times = read_arl_times([output], filedates)
            source = Path(str(metadata.get("source", ""))).name
            expected_prefix = (
                f"arco_era5_{window.start:%Y%m%dT%H%M}_"
                f"{window.end:%Y%m%dT%H%M}_"
            )
            overlaps = (
                times[-1] >= window.start - timedelta(hours=1.01)
                and times[0] <= window.end + timedelta(hours=1.01)
            )
            if overlaps or source.startswith(expected_prefix):
                related_failures.append(output.name)
        except Exception:
            unverifiable.append(output.name)
            source = Path(str(metadata.get("source", ""))).name
            expected_prefix = (
                f"arco_era5_{window.start:%Y%m%dT%H%M}_"
                f"{window.end:%Y%m%dT%H%M}_"
            )
            if source.startswith(expected_prefix):
                related_failures.append(output.name)

    if not candidates and (related_failures or unverifiable):
        raise RuntimeError(
            "Existing ARCO ARL files appear to belong to this period but do not "
            f"provide verifiable coverage: {related_failures or unverifiable}. They were preserved; "
            "the app did not start a replacement download."
        )
    return _select_verified_cache(candidates, "ARCO", notify=notify)


def _verified_cds_arl_cache(
    arl_dir: Path, window: TimeWindow, hysplit_executable: Path,
    field_profile: str = "hysplit",
) -> tuple[Path, ...]:
    """Reuse CDS ARLs by their actual timestamps, never by sidecar grouping."""
    filedates = Path(hysplit_executable).resolve().parent / "filedates.exe"
    if not filedates.is_file():
        raise RuntimeError(
            f"Cannot verify or reuse existing ERA5 ARL files because filedates.exe "
            f"is missing: {filedates}. No new ERA5 download was started."
        )

    outputs = [
        output for output in arl_dir.glob("era5_cds_*.arl")
        if output.is_file() and output.stat().st_size > 0
    ]
    relevant: list[tuple[Path, tuple[datetime, ...]]] = []
    related_failures: list[str] = []
    unknown_files: list[str] = []
    lower = window.start - timedelta(hours=1.01)
    upper = window.end + timedelta(hours=1.01)

    def filename_overlaps(path: Path) -> bool | None:
        # Current cache names include the CDS month and requested day span.
        # This is used only to fail safely when filedates cannot read the ARL.
        found = re.match(
            r"^era5_cds_(\d{4})(\d{2})_(\d{2})-(\d{2})_", path.name,
            flags=re.IGNORECASE,
        )
        if not found:
            return None
        year, month, first_day, last_day = map(int, found.groups())
        try:
            start = datetime(year, month, first_day, tzinfo=window.start.tzinfo)
            end = datetime(year, month, last_day, 23, tzinfo=window.start.tzinfo)
        except ValueError:
            return None
        return end >= lower and start <= upper

    for output in outputs:
        try:
            times = read_arl_times([output], filedates)
        except Exception:
            overlap = filename_overlaps(output)
            if overlap is True:
                related_failures.append(output.name)
            elif overlap is None:
                # An unreadable legacy filename cannot be safely associated
                # with a time range; stop rather than silently download over it.
                unknown_files.append(output.name)
            continue
        overlaps = times[-1] >= lower and times[0] <= upper
        metadata = _read_sidecar(output)
        try:
            same_window = (
                datetime.fromisoformat(metadata["requested_start"]) == window.start
                and datetime.fromisoformat(metadata["requested_end"]) == window.end
            )
        except (KeyError, TypeError, ValueError):
            same_window = False
        if overlaps:
            relevant.append((output, times))
        elif same_window:
            related_failures.append(output.name)

    if related_failures or (unknown_files and not relevant):
        names = related_failures + (unknown_files if not relevant else [])
        raise RuntimeError(
            "Existing CDS ARL files may belong to this period but cannot be used "
            f"safely: {names}. They were preserved; no replacement download or "
            "conversion was started."
        )
    if not relevant:
        return ()

    # A repeated timestamp across separate ARLs is ambiguous. Do not let the
    # set-based coverage checker hide duplicate datasets or choose by JSON.
    owners: dict[datetime, Path] = {}
    duplicates: set[Path] = set()
    for output, times in relevant:
        for when in times:
            if when < lower or when > upper:
                continue
            prior = owners.get(when)
            if prior is not None and prior != output:
                duplicates.update((prior, output))
            else:
                owners[when] = output
    if duplicates:
        raise RuntimeError(
            "Multiple CDS ARL files contain the same valid times for this period: "
            f"{sorted(path.name for path in duplicates)}. No file was selected by "
            "sidecar JSON; all files were preserved."
        )

    selected = tuple(path for path, _times in sorted(
        relevant, key=lambda item: item[1][0]
    ))
    try:
        report = check_coverage(
            list(selected), window.start, window.end, filedates,
            max_gap_hours=1.01,
        )
    except Exception as exc:
        raise RuntimeError(
            "Existing CDS ARL files overlap this period but do not provide "
            f"verified continuous coverage: {[path.name for path in selected]}. "
            "They were preserved; no replacement download was started. "
            f"{exc}"
        ) from exc
    if not report.verified:
        raise RuntimeError(
            "Existing CDS ARL files overlap this period but filedates did not "
            f"verify their coverage: {[path.name for path in selected]}. "
            "They were preserved; no replacement download was started."
        )
    return selected


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
    if action == "run" and provider in ("era5_cds", "arco_era5"):
        arl_dir = target_dir / "arl"
        cleanup_abandoned_converter_workspaces(arl_dir, notify=notify)
        if provider == "era5_cds" and not keep_sources:
            filedates = Path(hysplit_executable).resolve().parent / "filedates.exe"
            cleanup_verified_cds_gribs(
                arl_dir, target_dir, filedates, notify=notify,
            )
        elif provider == "arco_era5" and not keep_sources:
            filedates = Path(hysplit_executable).resolve().parent / "filedates.exe"
            cleanup_verified_hysplit_netcdf_sources(
                arl_dir, target_dir, filedates, notify=notify,
            )
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
    elif provider == "era5_cds":
        for index, window in enumerate(windows, 1):
            label = f"ERA5 CDS {index}/{len(windows)}"
            if action == "run":
                progress.start(f"{label}: 完了済みARLを確認中")
                cached_arl = _verified_cds_arl_cache(
                    target_dir / "arl", window, hysplit_executable,
                    era5_field_profile,
                )
                if cached_arl:
                    notify(
                        "Reusing verified local ERA5 CDS ARL without GRIB download: "
                        + ", ".join(path.name for path in cached_arl)
                    )
                    for path in cached_arl:
                        metadata = _read_sidecar(path)
                        source_gribs = metadata.get("source_grib_files", ())
                        if isinstance(source_gribs, list) and source_gribs:
                            notify(
                                f"ARL provenance only for {path.name}; source GRIBs: "
                                + ", ".join(Path(str(item)).name for item in source_gribs)
                            )
                        recorded_profile = metadata.get("field_profile")
                        if not metadata:
                            notify(
                                f"ARL provenance JSON is missing/unreadable for {path.name}; "
                                "the ARL is being used as-is after filedates coverage verification."
                            )
                        elif recorded_profile != era5_field_profile:
                            notify(
                                f"ARL profile differs from the selected profile "
                                f"({recorded_profile} vs {era5_field_profile}); using the "
                                "existing ARL as-is."
                            )
                    progress.complete(f"{label}: 検証済みARLを再利用")
                    progress.complete(f"{label}: GRIB取得・ARL変換を省略")
                    publish(window, _unique(cached_arl))
                    continue
            progress.start(f"{label}: GRIBをダウンロード中")
            inputs = download_cds(
                window.start, window.end, target_dir, bbox, notify=notify,
                field_profile=era5_field_profile,
            )
            progress.complete(f"{label}: GRIB {len(inputs)}ファイル取得")
            if action == "run":
                progress.start(f"{label}: WindowsでARLに変換中")
                with (conversion_slot() if conversion_slot else nullcontext()):
                    outputs = convert_cds_grib1_to_arl(
                        inputs, target_dir / "arl", window.start, window.end,
                        hysplit_executable=hysplit_executable,
                        keep_sources=keep_sources, notify=notify,
                    )
                progress.complete(f"{label}: ARL変換完了")
            else:
                outputs = inputs
            publish(window, _unique(outputs))
    elif provider == "arco_era5":
        all_parts = [
            (window, part)
            for window in windows
            for part in split_window(window)
        ]
        saved: dict[TimeWindow, list[Path]] = {window: [] for window in windows}
        for index, (window, part) in enumerate(all_parts, 1):
            label = f"Google ARCO ERA5 {index}/{len(all_parts)}"
            if action == "run":
                progress.start(f"{label}: 完了済みARLを確認中")
                cached_arl = _verified_arco_arl_cache(
                    target_dir / "arl", part, hysplit_executable,
                    era5_field_profile, notify=notify,
                )
                if cached_arl:
                    notify(
                        f"Reusing verified local ARCO ARL without NetCDF download: "
                        f"{', '.join(path.name for path in cached_arl)}"
                    )
                    for path in cached_arl:
                        metadata = _read_sidecar(path)
                        if not metadata:
                            notify(
                                f"ARL provenance JSON is missing/unreadable for {path.name}; "
                                "the ARL is being used as-is after filedates coverage verification."
                            )
                            continue
                        source = metadata.get("source")
                        if source:
                            notify(
                                f"ARL provenance only for {path.name}; source: "
                                f"{Path(str(source)).name}"
                            )
                        recorded_profile = _profile_from_arco_metadata(metadata)
                        if recorded_profile and recorded_profile != era5_field_profile:
                            notify(
                                f"ARL profile differs from the selected profile "
                                f"({recorded_profile} vs {era5_field_profile}); using the "
                                "existing ARL as-is."
                            )
                        if not has_current_arco_conversion_policy(metadata):
                            quality = metadata.get("humidity_quality_control")
                            policy = quality.get("policy") if isinstance(quality, dict) else None
                            notify(
                                "ARL RH conversion metadata differs from this app; "
                                "the existing ARL is used unchanged. "
                                + (f"Recorded policy: {policy}" if policy else "No policy text recorded.")
                            )
                    progress.complete(f"{label}: 検証済みARLを再利用")
                    progress.complete(f"{label}: NetCDF取得・ARL変換を省略")
                    saved[window].extend(cached_arl)
                    continue
            progress.start(f"{label}: 地域NetCDFを取得中")
            netcdf = download_google_arco(
                part.start, part.end, target_dir, bbox, notify=notify,
                field_profile=era5_field_profile,
                progress=lambda current, total, _label=label: progress.fraction(
                    f"{_label}: NetCDF書き出し {current}/{total} 時間",
                    current / total if total else None,
                ),
            )
            progress.complete(f"{label}: NetCDF取得完了")
            if action == "run":
                progress.start(f"{label}: WindowsでARLに変換中")
                with (conversion_slot() if conversion_slot else nullcontext()):
                    outputs = convert_arco_netcdf_to_arl(
                        netcdf, target_dir/"arl", part.start, part.end,
                        hysplit_executable=hysplit_executable, notify=notify,
                    )
                filedates = Path(hysplit_executable).resolve().parent / "filedates.exe"
                if not keep_sources:
                    cleanup_verified_hysplit_netcdf_sources(
                        target_dir / "arl", target_dir, filedates, notify=notify,
                    )
                progress.complete(f"{label}: ARL変換完了")
            else:
                outputs = [netcdf]
            saved[window].extend(outputs)
        for window in windows:
            publish(window, _unique(saved[window]))
    else:
        raise ValueError(f"Unsupported meteorology source: {provider}")
    all_paths = _unique(
        path for window in windows for path in by_window[window]
    )
    return SparseMeteorology(by_window, all_paths)

