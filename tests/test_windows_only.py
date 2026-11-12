"""Windows-native-only runtime policy tests."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.platform_guard import require_native_windows
from hysplit_tool.native_era5 import run_native_grib1_pair


class TestWindowsOnly(unittest.TestCase):
    def test_policy_is_enforced_on_current_platform(self):
        if sys.platform == "win32" and os.name == "nt":
            require_native_windows()
        else:
            with self.assertRaisesRegex(RuntimeError, "WSL, Linux and macOS"):
                require_native_windows()

    def test_linux_platform_rejected_even_when_os_name_mocked(self):
        with patch("hysplit_tool.platform_guard.sys.platform", "linux"):
            with self.assertRaises(RuntimeError):
                require_native_windows()

    def test_native_converter_calls_windows_guard_before_io(self):
        with patch("hysplit_tool.native_era5.require_native_windows",
                   side_effect=RuntimeError("native Windows required")):
            with self.assertRaisesRegex(RuntimeError, "native Windows"):
                run_native_grib1_pair(Path("missing_upper"), Path("missing_surface"),
                                      Path("output.arl"), Path("grib2arl.exe"))


if __name__ == "__main__":
    unittest.main()
