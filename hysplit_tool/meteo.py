"""Download NOAA preconverted ARL meteorology (GDAS1 / GFS 0.25)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .time_windows import TimeWindow

BASE = "https://www.ready.noaa.gov/data/archives"
S3_BASE = "https://noaa-oar-arl-hysplit-pds.s3.amazonaws.com"
MIB = 1024 * 1024
MAX_RETRIES = 3
PROGRESS_INTERVAL_SECONDS = 5.0
DISK_RESERVE_BYTES = 256 * MIB


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
            urls = (
                f"{S3_BASE}/gdas1/{day:%Y}/{name}",
                f"{BASE}/gdas1/{day:%Y}/{name}",
                f"{BASE}/gdas1/{name}",
            )
        elif source == "gfs0p25":
            name = f"{day:%Y%m%d}_gfs0p25"
            urls = (
                f"{S3_BASE}/gfs0p25/{day:%Y}/{day:%m}/{name}",
                f"{BASE}/gfs0p25/{day:%Y}/{day:%m}/{name}",
                f"{BASE}/gfs0p25/{name}",
            )
        else:
            raise ValueError(f"Unknown NOAA ARL data source: {source}")
        files[name] = RemoteFile(name, urls)
    return list(files.values())


def plan_archive_windows(
    source: str, windows: Iterable[TimeWindow],
) -> list[RemoteFile]:
    """Deduplicate NOAA weekly/daily ARL files for disjoint trajectory windows."""
    files: dict[str, RemoteFile] = {}
    for window in sorted(windows):
        for entry in plan_archives(source, window.start, window.end):
            files.setdefault(entry.name, entry)
    return list(files.values())


def _download_error_hint(error: Exception) -> str:
    """Actionable diagnostics without weakening TLS verification."""
    message = str(error)
    if "CERTIFICATE_VERIFY_FAILED" in message.upper() or "SSL: CERTIFICATE" in message.upper():
        return (
            "TLS certificate validation failed. Check the Windows date/time, "
            "company HTTPS-inspection proxy and trusted CA certificates. "
            "Do not disable SSL certificate validation."
        )
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401,403):
            return "Server denied access (HTTP 401/403); check URL, proxy/firewall policies."
        if error.code == 404:
            return "Archive file is unavailable (HTTP 404); check date and data source."
        if error.code == 429:
            return "Server rate limit (HTTP 429); retry later."
    if isinstance(error, (TimeoutError, urllib.error.URLError)):
        return "Connection timeout/DNS/proxy failure; check network and HTTPS proxy settings."
    if isinstance(error, PermissionError):
        return "Cannot write to the data folder; check disk permissions/antivirus/OneDrive locks."
    return message


def _retryable_download_error(error: Exception) -> bool:
    """Never retry missing files, local access failures or certificate errors."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    if isinstance(error, PermissionError):
        return False
    if "CERTIFICATE_VERIFY_FAILED" in str(error).upper():
        return False
    return isinstance(error, (urllib.error.URLError, TimeoutError,
                              ConnectionResetError, ConnectionAbortedError)) or (
        isinstance(error, OSError) and "incomplete download" in str(error).lower()
    )


def download_arl_file(
    spec: RemoteFile,
    target_dir: Path,
    notify: Callable[[str], None] | None = None,
    opener=None,
    *,
    max_retries: int = MAX_RETRIES,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download via S3/READY with HTTP Range resume, retries and live progress.

    Large NOAA files are streamed (never buffered into RAM). Resuming is only
    allowed when the matching URL has ETag or Last-Modified validation.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / spec.name
    if target.is_file() and target.stat().st_size > 0:
        from .validation import check_signature
        check_signature(target)
        if notify:
            notify(f"CACHED {target.name} ({target.stat().st_size / MIB:.1f} MiB)")
        if progress:
            size = target.stat().st_size
            progress(size, size)
        return target
    if max_retries < 1 or max_retries > 10:
        raise ValueError("max_retries must be between 1 and 10")
    partial = target.with_name(target.name + ".part")
    metadata = target.with_name(target.name + ".part.json")
    errors: list[str] = []
    opener = opener or urllib.request.urlopen

    for url in spec.urls:
        for attempt in range(1, max_retries + 1):
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
                # No server validator = no safe way to append.
                partial.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                existing = 0
            headers = {"User-Agent": "HYSPLIT-Batch/1.2"}
            if resume:
                headers.update({"Range": f"bytes={existing}-", "If-Range": validator})
            if notify:
                notify(
                    f"{'Resuming' if resume else 'Downloading'} {spec.name} "
                    f"(attempt {attempt}/{max_retries}), host: "
                    f"{urllib.parse.urlparse(url).hostname or url}"
                    + (f", already {existing / MIB:.1f} MiB" if resume else "")
                )
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
                        notify("Server ignored HTTP Range; safely restarting from byte zero")
                    length = get_header("Content-Length")
                    if length is not None:
                        size = int(length)
                        if size < 0:
                            raise OSError("Invalid HTTP Content-Length")
                        if append and expected_total != existing + size:
                            raise OSError("Range length does not match Content-Length")
                        if not append:
                            expected_total = size
                    if expected_total is not None:
                        free = shutil.disk_usage(target_dir).free
                        bytes_remaining = expected_total - (existing if append else 0)
                        if free < bytes_remaining + DISK_RESERVE_BYTES:
                            raise OSError(
                                f"Not enough free disk space: need at least "
                                f"{(bytes_remaining + DISK_RESERVE_BYTES) / MIB:.0f} MiB, "
                                f"available {free / MIB:.0f} MiB. Choose another work folder."
                            )
                        if notify:
                            notify(
                                f"NOAA file {spec.name}: {expected_total / MIB:.1f} MiB, "
                                f"free disk {free / MIB:.0f} MiB"
                            )
                    if progress:
                        progress(existing if append else 0, expected_total)
                    metadata.write_text(json.dumps({
                        "url": url,
                        "etag": get_header("ETag") or (saved.get("etag") if append else None),
                        "last_modified": get_header("Last-Modified")
                                         or (saved.get("last_modified") if append else None),
                    }), encoding="utf-8")

                    began = time.monotonic()
                    progress_at = began
                    written = 0
                    with partial.open("ab" if append else "wb") as output:
                        while True:
                            block = response.read(MIB)
                            if not block:
                                break
                            output.write(block)
                            written += len(block)
                            now = time.monotonic()
                            if notify and now - progress_at >= PROGRESS_INTERVAL_SECONDS:
                                done = (existing if append else 0) + written
                                elapsed = max(now - began, 0.001)
                                rate = written / elapsed / MIB
                                fraction = (
                                    f"{100 * done / expected_total:.1f}%"
                                    if expected_total else "size unknown"
                                )
                                notify(
                                    f"NOAA {spec.name}: {done / MIB:.1f}"
                                    + (f"/{expected_total / MIB:.1f} MiB" if expected_total else " MiB")
                                    + f" ({fraction}), {rate:.2f} MiB/s"
                                )
                                progress_at = now
                                if progress:
                                    progress(done, expected_total)
                    actual = partial.stat().st_size
                    if not actual or (expected_total is not None and actual != expected_total):
                        raise OSError(
                            f"Incomplete download {spec.name}: {actual} of {expected_total} bytes"
                        )
                    from .validation import check_signature
                    check_signature(partial)
                    os.replace(partial, target)
                    metadata.unlink(missing_ok=True)
                    if progress:
                        progress(actual, expected_total or actual)
                    if notify:
                        notify(f"DOWNLOADED {target.name}: {actual / MIB:.1f} MiB")
                    return target
            except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
                errors.append(f"{url} (attempt {attempt}): {_download_error_hint(exc)}")
                if isinstance(exc, PermissionError) or (
                    isinstance(exc, OSError) and "Not enough free disk" in str(exc)
                ):
                    raise RuntimeError(errors[-1]) from exc
                if not _retryable_download_error(exc) or attempt == max_retries:
                    break
                if notify:
                    notify(f"Temporary download error: {_download_error_hint(exc)}; retrying")
                time.sleep(min(2**(attempt - 1), 4))
    raise RuntimeError(
        f"Could not download {spec.name}. Tried all NOAA hosts. "
        + " | ".join(errors)
    )


def download_noaa_windows(
    source: str, windows: Iterable[TimeWindow], target_dir: Path,
    notify: Callable[[str], None] | None = None,
    progress: Callable[[int, int, float | None, str], None] | None = None,
) -> list[Path]:
    """Fetch each *distinct* required NOAA archive once, skipping empty gaps."""
    plan = plan_archive_windows(source, windows)
    if notify:
        notify(
            f"NOAA {source}: {len(plan)} unique ARL files for disjoint periods; "
            "cached files are reused"
        )
    files = []
    for index, item in enumerate(plan):
        if progress:
            progress(index, len(plan), 0.0, item.name)

        def on_bytes(done: int, total: int | None, *,
                     _index: int = index, _name: str = item.name) -> None:
            if progress:
                fraction = (min(1.0, done / total) if total else None)
                progress(_index, len(plan), fraction, _name)

        files.append(download_arl_file(item, target_dir, notify, progress=on_bytes))
        if progress:
            progress(index + 1, len(plan), 1.0, item.name)
    return files


def download_noaa(source: str, earliest: datetime, latest: datetime, target_dir: Path,
                  notify: Callable[[str], None] | None = None) -> list[Path]:
    """Backwards-compatible single-interval downloader."""
    return download_noaa_windows(source, (TimeWindow(earliest, latest),),
                                 target_dir, notify)


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
