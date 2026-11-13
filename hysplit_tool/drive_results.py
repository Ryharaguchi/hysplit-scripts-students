"""Copy completed native Windows trajectory results to a Google Drive folder.

Large ARL/ERA5/GDAS meteorology downloads and HYSPLIT executables are kept
outside the Drive folder. A complete *run folder* is copied after execution
and map generation, with a temporary destination and post-copy verification.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable


def suggested_drive_results_root() -> str:
    """Prefer the user's H: Google Drive, but never fabricate/mount a drive.

    No G: fallback: these drives can be different Google accounts. On a PC
    without H:\\マイドライブ, copying is opt-in using the GUI folder picker.
    """
    if os.name != "nt":
        return ""
    path = Path("H:/マイドライブ")
    return str(path / "HYSPLIT") if path.is_dir() else ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def copy_completed_run(
    source: Path,
    drive_root: Path,
    notify: Callable[[str], None] | None = None,
) -> Path:
    """Copy an entire successful run, including tdump, logs, SVG and CSV.

    Refuse to copy incomplete outputs and never overwrite an existing run.
    A local HYSPLIT computation is not invalidated when Drive is unavailable.
    """
    source = Path(source).resolve(strict=True)
    drive_root = Path(drive_root)
    required = ("trajectories.csv", "manifest.json", "observations.csv")
    if not source.is_dir() or any(not (source / f).is_file() for f in required):
        raise ValueError(f"Source is not a complete HYSPLIT result: {source}")
    if not (drive_root.parent).is_dir():
        raise FileNotFoundError(
            f"Google Drive folder is unavailable: {drive_root.parent}. "
            "Mount the correct H: Google Drive or change the copy destination."
        )
    if drive_root.exists() and not drive_root.is_dir():
        raise NotADirectoryError(drive_root)

    dest_parent = drive_root / "runs"
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / source.name
    if dest.exists():
        raise FileExistsError(f"Drive result already exists; refusing to overwrite: {dest}")
    staged = dest_parent / f".{source.name}.copying_{uuid.uuid4().hex[:8]}"
    if notify:
        notify(f"Google Driveへ計算結果をコピー: {source.name} -> {dest}")
    try:
        shutil.copytree(source, staged, symlinks=False)
        # Verify the most valuable scientific outputs, without storing
        # a full second archive of huge tdump files in local memory.
        for filename in required:
            if _sha256(source / filename) != _sha256(staged / filename):
                raise OSError(f"Google Drive copy failed SHA256 verification: {filename}")
        if dest.exists():
            raise FileExistsError(f"Google Drive destination already exists: {dest}")
        os.replace(staged, dest)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    if notify:
        notify(
            f"Google Drive保存完了: {dest} "
            "(Googleのクラウド同期完了はGoogle Drive側で確認してください)"
        )
    return dest

