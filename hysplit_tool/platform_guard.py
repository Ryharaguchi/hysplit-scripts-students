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
