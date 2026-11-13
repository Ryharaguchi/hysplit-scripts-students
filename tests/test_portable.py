"""Portable Windows distribution path detection and self-test (no NOAA binaries)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.portable import (
    application_dir, default_work_root, find_hysplit_executable,
    hysplit_candidates, standalone_diagnostics,
)


class TestPortablePaths(unittest.TestCase):
    def test_hysplit_home_autodetects_user_installed_model(self):
        with tempfile.TemporaryDirectory() as td:
            model = Path(td) / "HYSPLIT"
            exe = model / "exec" / "hyts_std.exe"
            exe.parent.mkdir(parents=True)
            (model / "bdyfiles").mkdir()
            exe.write_bytes(b"test executable")
            with patch.dict("os.environ", {"HYSPLIT_HOME": str(model), "HYSPLIT_EXE": ""}):
                self.assertEqual(find_hysplit_executable(), exe)
                self.assertIn(exe, hysplit_candidates())

    def test_default_work_root_uses_local_desktop_not_google_drive(self):
        from hysplit_tool import portable
        with tempfile.TemporaryDirectory() as td:
            with patch.object(portable, "windows_desktop_dir", return_value=Path(td)):
                self.assertEqual(default_work_root(), Path(td) / "HYSPLIT_Work")

    def test_application_dir_uses_frozen_exe_parent(self):
        import hysplit_tool.portable as portable
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "HYSPLIT_Batch.exe"
            with patch.object(portable.sys, "frozen", True, create=True), \
                 patch.object(portable.sys, "executable", str(exe)):
                self.assertEqual(application_dir(), exe.resolve().parent)

    def test_gui_defaults_to_local_desktop_without_cloud_copy(self):
        from hysplit_tool.gui import App
        from hysplit_tool import portable
        with tempfile.TemporaryDirectory() as td:
            with patch.object(portable, "windows_desktop_dir", return_value=Path(td)), \
                 patch("hysplit_tool.gui.default_work_root", return_value=Path(td)/"HYSPLIT_Work"):
                gui = App()
                gui.withdraw()
                try:
                    self.assertEqual(Path(gui.v["root"].get()),
                                     Path(td) / "HYSPLIT_Work")
                    self.assertNotIn("cloud_copy_root", gui.v)
                finally:
                    gui.destroy()

    def test_diagnostic_json_without_real_model(self):
        from hysplit_tool import portable
        from hysplit_tool import gui as gui_module

        class StubGui:
            def withdraw(self): pass
            def destroy(self): pass

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "diagnostics.json"
            with patch.object(portable, "_REQUIRED_PACKAGES", ("json",)), \
                 patch.object(gui_module, "App", StubGui), \
                 patch.object(portable, "find_hysplit_executable",
                              return_value=Path(td)/"missing"/"hyts_std.exe"):
                self.assertTrue(standalone_diagnostics(output))
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(doc["gui_loaded"])
            self.assertTrue(doc["packaging_passed"])
            self.assertFalse(doc["hysplit_executable_found"])
            self.assertTrue(doc["libraries"]["json"]["ok"])


if __name__ == "__main__":
    unittest.main()

