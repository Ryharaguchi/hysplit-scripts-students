"""Windows-native runtime policy: no WSL, Linux or macOS execution."""

import os
import sys


def require_native_windows() -> None:
    """Refuse execution outside native Windows Python before running any model."""
    if os.name != "nt" or sys.platform != "win32":
        raise RuntimeError(
            "Windows-native Python is required. WSL, Linux and macOS are prohibited. "
            "Run 'py -3 run_gui.py' in Windows PowerShell or Command Prompt."
        )

from contextlib import contextmanager


@contextmanager
def prevent_automatic_system_sleep():
    """Keep native Windows awake while a long unattended batch runs.

    No Windows power-plan change; restore the prior idle-sleep behavior when
    the batch finishes. Manual Sleep, shutdown and laptop lid policy override
    this request. Called on the background worker thread.
    """
    require_native_windows()
    import ctypes

    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED; deliberately not DISPLAY_REQUIRED.
    continuous = 0x80000000
    system_required = 0x00000001
    kernel32 = ctypes.windll.kernel32
    applied = kernel32.SetThreadExecutionState(continuous | system_required)
    try:
        yield
    finally:
        if applied:
            kernel32.SetThreadExecutionState(continuous)
