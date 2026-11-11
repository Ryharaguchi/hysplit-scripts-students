"""Download NOAA preconverted ARL meteorology (GDAS1 / GFS 0.25)."""

from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

BASE = "https://www.ready.noaa.gov/data/archives"


@dataclass(frozen=True)
class RemoteFile:
    name: str
    urls: tuple[str, ...]


def days_inclusive(start: date, end: date):
    for i in range((end - start).days + 1):
        yield start + timedelta(days=i)


def plan_archives(source: str, earliest: datetime, latest: datetime) -> list[RemoteFile]:
    """Plan inputs with 3h temporal padding for HYSPLIT interpolation.

    The caller supplies start/end of all desired back trajectories, not merely
    their launch times. Meteorological coverage is inferred from filenames only.
    """
    if earliest > latest:
        raise ValueError("Earliest data time exceeds latest")
    first = (earliest - timedelta(hours=3)).date()
    last = (latest + timedelta(hours=3)).date()
    files = {}
    for day in days_inclusive(first, last):
        if source == "gdas1":
            week = min((day.day - 1) // 7 + 1, 5)
            name = f"gdas1.{day:%b}{day:%y}.w{week}".lower()
            urls = (f"{BASE}/gdas1/{day:%Y}/{name}", f"{BASE}/gdas1/{name}")
        elif source == "gfs0p25":
            name = f"{day:%Y%m%d}_gfs0p25"
            urls = (f"{BASE}/gfs0p25/{day:%Y}/{day:%m}/{name}", f"{BASE}/gfs0p25/{name}")
        else:
            raise ValueError(f"Unknown NOAA ARL data source: {source}")
        files[name] = RemoteFile(name, urls)
    return list(files.values())


def download_arl_file(spec: RemoteFile, target_dir: Path,
                      notify: Callable[[str], None] | None = None,
                      opener=None) -> Path:
    """Sequential HTTPS download with atomic rename and HTTP fallback; no partial files reused."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / spec.name
    if target.is_file() and target.stat().st_size > 0:
        if notify:
            notify(f"CACHED {target.name}")
        return target
    partial = target.with_name(target.name + ".part")
    errors = []
    opener = opener or urllib.request.urlopen
    for url in spec.urls:
        try:
            if notify:
                notify(f"Downloading {spec.name} from {url}")
            request = urllib.request.Request(url, headers={"User-Agent": "hysplit-students/0.1"})
            with opener(request, timeout=90) as response, partial.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            if partial.stat().st_size == 0:
                raise OSError("Downloaded file is empty")
            os.replace(partial, target)
            return target
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            errors.append(f"{url}: {exc}")
            partial.unlink(missing_ok=True)
    raise RuntimeError(f"Could not download {spec.name}. Tried: " + "; ".join(errors))


def download_noaa(source: str, earliest: datetime, latest: datetime, target_dir: Path,
                  notify: Callable[[str], None] | None = None) -> list[Path]:
    plan = plan_archives(source, earliest, latest)
    if notify:
        notify(f"NOAA {source}: {len(plan)} ARL files; large downloads possible")
    # NOAA recommends <=2 simultaneous FTP connections; one sequential HTTPS download.
    return [download_arl_file(item, target_dir, notify) for item in plan]


def local_arl_files(spec: str) -> list[Path]:
    """Accept semicolon-separated explicit ARL paths; no unvalidated directory globs."""
    paths = [Path(item.strip().strip('"')).expanduser().resolve()
             for item in spec.split(";") if item.strip()]
    if not paths:
        raise ValueError("Select one or more ARL meteorological files")
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"ARL file not found / empty: {path}")
    return paths
