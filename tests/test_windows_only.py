"""Windows-only execution policy regression checks."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.platform_guard import require_native_windows
from hysplit_tool.era5_convert import _converter_command


class TestWindowsOnly(unittest.TestCase):
    def test_policy_is_enforced_on_this_platform(self):
        if sys.platform == "win32" and os.name == "nt":
            require_native_windows()
        else:
            with self.assertRaisesRegex(RuntimeError, "WSL, Linux and macOS"):
                require_native_windows()

    def test_linux_platform_rejected_even_when_os_name_mocked(self):
        with patch("hysplit_tool.platform_guard.sys.platform", "linux"):
            with self.assertRaises(RuntimeError):
                require_native_windows()

    def test_wsl_converter_string_rejected(self):
        with self.assertRaisesRegex(ValueError, "prohibited"):
            _converter_command("wsl:/opt/hysplit/api2arl", Path("."), "input.grib2", "output.arl")

    def test_converter_requires_windows_exe(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "api2arl"
            executable.write_text("mock", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "Windows-native"):
                _converter_command(str(executable), Path(temp), "input.grib2", "output.arl")


if __name__ == "__main__":
    unittest.main()
