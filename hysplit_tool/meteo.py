"""Download NOAA preconverted ARL meteorology (GDAS1 / GFS 0.25)."""

from __future__ import annotations

import json
import os
import re
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
    """Download with checked HTTP Range resume and atomic final rename.

    Resume requires a matching URL and an ETag or Last-Modified validator.
    Server responses that ignore Range restart from byte zero, never append.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / spec.name
    if target.is_file() and target.stat().st_size > 0:
        from .validation import check_signature
        check_signature(target)
        if notify:
            notify(f"CACHED {target.name}")
        return target
    partial = target.with_name(target.name + ".part")
    metadata = target.with_name(target.name + ".part.json")
    errors = []
    opener = opener or urllib.request.urlopen

    for url in spec.urls:
        saved = {}
        if partial.is_file() and metadata.is_file():
            try:
                saved = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if saved.get("url") != url:
            partial.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            saved = {}
        existing = partial.stat().st_size if partial.is_file() else 0
        validator = saved.get("etag") or saved.get("last_modified")
        resume = existing > 0 and bool(validator)
        if existing and not resume:
            partial.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            existing = 0
        headers = {"User-Agent": "hysplit-students/0.2"}
        if resume:
            headers.update({"Range": f"bytes={existing}-", "If-Range": validator})
        if notify:
            notify(f"{'Resuming' if resume else 'Downloading'} {spec.name} from {url}"
                   + (f" at {existing:,} bytes" if resume else ""))
        try:
            request = urllib.request.Request(url, headers=headers)
            with opener(request, timeout=90) as response:
                status = getattr(response, "status", None) or 200
                response_headers = getattr(response, "headers", {}) or {}
                get_header = getattr(response_headers, "get", lambda _k, _v=None: None)
                append = False
                expected_total = None
                if resume and status == 206:
                    value = get_header("Content-Range", "") or ""
                    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", value.strip())
                    if not match or int(match.group(1)) != existing:
                        raise OSError(f"Invalid HTTP Content-Range for {spec.name}: {value!r}")
                    start, stop, total = map(int, match.groups())
                    if stop < start or total <= stop:
                        raise OSError(f"Invalid HTTP Content-Range total: {value!r}")
                    expected_total = total
                    append = True
                elif status not in (200, 206):
                    raise OSError(f"Unexpected HTTP status {status} for {spec.name}")
                elif status == 206:
                    raise OSError("Unexpected HTTP 206 response without a Range request")
                elif resume and notify:
                    notify("Server ignored Range; safely restarting download from byte zero")
                length = get_header("Content-Length")
                if length is not None:
                    size = int(length)
                    if size < 0:
                        raise OSError("Invalid Content-Length")
                    if append and expected_total != existing + size:
                        raise OSError("Range length does not match Content-Length")
                    if not append:
                        expected_total = size

                metadata.write_text(json.dumps({
                    "url": url,
                    "etag": get_header("ETag") or (saved.get("etag") if append else None),
                    "last_modified": get_header("Last-Modified")
                        or (saved.get("last_modified") if append else None),
                }), encoding="utf-8")
                with partial.open("ab" if append else "wb") as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        stream.write(block)
                actual = partial.stat().st_size
                if not actual or (expected_total is not None and actual != expected_total):
                    raise OSError(f"Incomplete download {spec.name}: {actual} of {expected_total} bytes")
                with partial.open("rb") as stream:
                    first = stream.read(128).lstrip().lower()
                if first.startswith((b"<!doctype", b"<html", b"<?xml", b"{\"error\"")):
                    raise OSError("Server returned an HTML/XML/JSON error rather than meteorology")
                os.replace(partial, target)
                metadata.unlink(missing_ok=True)
                return target
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            # A different fallback endpoint may not have identical bytes.
            # Keep a partial only for a future attempt at the *same* URL.
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
