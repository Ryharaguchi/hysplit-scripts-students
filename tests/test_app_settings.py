"""Portable GUI settings are atomic and stored in the local user profile."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.app_settings import load_settings, save_settings, settings_path


class TestAppSettings(unittest.TestCase):
    def test_settings_path_uses_local_appdata(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\example\AppData\Local"}):
            self.assertEqual(
                settings_path(),
                Path(r"C:\Users\example\AppData\Local\HYSPLIT_Batch\settings.json"),
            )

    def test_atomic_round_trip_preserves_unicode_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            expected = {
                "schema_version": 1,
                "fields": {"csv": r"H:\マイドライブ\観測.csv", "workers": "自動"},
                "met_diagnostics": ["TM_TPOT", "TM_RELH"],
                "show_advanced_timestep": False,
            }
            self.assertEqual(save_settings(expected, path), path)
            self.assertEqual(load_settings(path), expected)
            self.assertEqual(list(Path(temp).glob("*.part")), [])

    def test_invalid_settings_are_ignored_without_deleting_the_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(load_settings(path), {})
            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid")

    def test_failed_write_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            with self.assertRaises(TypeError):
                save_settings({"unsupported": object()}, path)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(temp).glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
