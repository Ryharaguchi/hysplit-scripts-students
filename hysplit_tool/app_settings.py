"""Per-user, atomic persistence for portable HYSPLIT GUI settings."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    """Keep settings in the local Windows profile, not beside the Drive app."""
    root = os.environ.get("LOCALAPPDATA", "").strip()
    if root:
        base = Path(root)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "HYSPLIT_Batch" / "settings.json"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_settings(payload: dict[str, Any], path: Path | None = None) -> Path:
    """Write settings via a same-directory temp file so interruption is safe."""
    target = Path(path) if path is not None else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".part", dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target
