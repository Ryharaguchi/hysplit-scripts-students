"""Remove bulky run scratch and meteorology caches after successful runs."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, Iterable


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def cleanup_successful_run(
    run_root: Path,
    group_folders: Iterable[Path],
    *,
    notify: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Delete generated per-group working folders and shared terrain copies.

    The caller must invoke this only after every trajectory CSV has been
    written and map generation has finished. Result files, input snapshot, and
    manifest remain so the run can be reproduced from its verified ARL cache.
    """
    root = Path(run_root).resolve()
    removed_folders = 0
    freed = 0
    failures = 0
    seen: set[Path] = set()

    for raw_folder in group_folders:
        folder = Path(raw_folder)
        if folder in seen:
            continue
        seen.add(folder)
        if folder.is_symlink():
            failures += 1
            continue
        resolved = folder.resolve()
        if resolved.parent != root or not folder.is_dir():
            failures += 1
            continue
        try:
            size = sum(
                item.stat().st_size
                for item in folder.rglob("*")
                if item.is_file() and not item.is_symlink()
            )
            shutil.rmtree(folder)
        except OSError:
            failures += 1
            continue
        removed_folders += 1
        freed += size

    shared_tables = root / "bdyfiles"
    try:
        if shared_tables.is_dir() and not shared_tables.is_symlink():
            size = sum(
                item.stat().st_size
                for item in shared_tables.rglob("*")
                if item.is_file() and not item.is_symlink()
            )
            shutil.rmtree(shared_tables)
            removed_folders += 1
            freed += size
    except OSError:
        failures += 1

    result = {
        "folders": removed_folders,
        "files": 0,
        "bytes": freed,
        "failures": failures,
    }
    if notify:
        notify(
            "Successful-run working files cleaned: "
            f"{removed_folders} group folders, "
            f"{freed / (1024 ** 3):.2f} GiB freed"
            + (f"; {failures} items could not be removed" if failures else "")
        )
    return result


def cleanup_successful_meteorology(
    met_root: Path,
    meteorology_files: Iterable[Path],
    *,
    keep_sources: bool = False,
    notify: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Prune successful-run meteorology intermediates while keeping its ARL cache.

    The verified ARL and its JSON provenance are the minimum cache required for
    a fast trajectory rerun. Logs and RH quality reports are disposable. Source
    NetCDF/GRIB archives are removed only when they are named in that ARL's
    provenance and ``keep_sources`` is false. All candidates must remain inside
    this provider cache directory.
    """
    root = Path(met_root).resolve()
    candidates: set[Path] = set()
    outputs = tuple(Path(path) for path in meteorology_files)

    def add_candidate(value: object, *, suffix: str | None = None,
                      prefixes: tuple[str, ...] = ()) -> None:
        if not isinstance(value, (str, Path)):
            return
        raw = Path(value)
        try:
            resolved = raw.resolve()
        except OSError:
            return
        if not _inside(resolved, root) or raw.is_symlink():
            return
        if suffix and resolved.suffix.lower() != suffix:
            return
        if prefixes and not resolved.name.lower().startswith(prefixes):
            return
        candidates.add(resolved)

    for raw_output in outputs:
        raw_path = Path(raw_output)
        if raw_path.is_symlink():
            continue
        try:
            output = raw_path.resolve()
        except OSError:
            continue
        if not _inside(output, root) or not output.is_file():
            continue
        if output.suffix.lower() != ".arl":
            continue

        metadata_path = output.with_suffix(".json")
        metadata = {}
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                metadata = {}

        add_candidate(output.with_name(output.name + ".log"))
        add_candidate(output.with_name(output.stem + "_pressure.quality.json"))
        add_candidate(output.with_name(output.stem + "_surface.quality.json"))

        if not keep_sources and isinstance(metadata, dict):
            try:
                has_verified_times = (
                    str(metadata.get("validation", "")).startswith(
                        "NOAA Windows filedates"
                    )
                    and int(metadata.get("time_count", 0) or 0) > 0
                )
            except (TypeError, ValueError):
                has_verified_times = False
            if has_verified_times:
                source = metadata.get("source")
                if (isinstance(source, str)
                        and Path(source).suffix.lower() == ".nc"
                        and Path(source).name.lower().endswith("_hysplit.nc")):
                    # Full-variable NetCDF is an archival source, not scratch.
                    # Only remove the explicitly generated HYSPLIT subset.
                    add_candidate(
                        source, suffix=".nc", prefixes=("arco_era5_",)
                    )
                source_gribs = metadata.get("source_grib_files", ())
                if isinstance(source_gribs, list):
                    for source in source_gribs:
                        add_candidate(
                            source, suffix=".grib",
                            prefixes=("era5_pressure_", "era5_single_"),
                        )

    removed = 0
    freed = 0
    retained_arl = sum(
        1 for raw in outputs
        if Path(raw).suffix.lower() == ".arl"
        and not Path(raw).is_symlink()
        and _inside(Path(raw).resolve(), root)
        and Path(raw).is_file()
    )
    failures = 0
    for path in sorted(candidates, key=lambda item: str(item).lower()):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            path.unlink()
        except OSError:
            failures += 1
            continue
        removed += 1
        freed += size

    result = {"files": removed, "bytes": freed, "failures": failures}
    if notify:
        notify(
            "Successful-run meteorology intermediates cleaned: "
            f"{removed} files, {freed / (1024 ** 3):.2f} GiB freed; "
            f"{retained_arl} verified ARL cache files kept"
            + (f"; {failures} files could not be removed" if failures else "")
        )
    result["arl_files_kept"] = retained_arl
    return result
