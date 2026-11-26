"""Native GRIB1 -> ARL tests use a process stub, not meteorological data."""
import tempfile
import unittest
import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from hysplit_tool.native_era5 import (
    require_grib1, run_native_grib1_pair,
    cleanup_abandoned_converter_workspaces,
    check_arco_conversion_space,
)
from hysplit_tool.validation import CoverageReport
from hysplit_tool.time_windows import TimeWindow
from hysplit_tool.arco_convert import PRESSURE_LEVELS as ARCO_PRESSURE_LEVELS

UTC = timezone.utc


def fake_grib(path: Path, edition: int = 1):
    path.write_bytes(b"GRIB\x00\x00\x00" + bytes([edition]) + b"EXAMPLE" * 12)


class TestNativeEra5(unittest.TestCase):
    def test_arco_disk_preflight_accounts_for_grib_and_arl_peak(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            notify = []
            with patch("hysplit_tool.native_era5._probe_hardlink", return_value=True), \
                 patch("hysplit_tool.native_era5.shutil.disk_usage") as disk_usage:
                disk_usage.return_value.free = 14 * (1024 ** 3)
                with self.assertRaisesRegex(OSError, "No conversion was started"):
                    check_arco_conversion_space(
                        10 * (1024 ** 3), root / "cache_pressure.grib", notify.append,
                    )
            self.assertTrue(any("estimated peak needed=15.00 GiB" in x for x in notify))
            self.assertEqual(list(root.iterdir()), [])

    def test_arco_disk_preflight_includes_copy_if_hardlinks_are_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("hysplit_tool.native_era5._probe_hardlink", return_value=False), \
                 patch("hysplit_tool.native_era5.shutil.disk_usage") as disk_usage:
                disk_usage.return_value.free = 24 * (1024 ** 3)
                with self.assertRaisesRegex(OSError, "estimated peak minimum 25.00 GiB"):
                    check_arco_conversion_space(
                        10 * (1024 ** 3), root / "cache_pressure.grib",
                    )

    def test_grib_edition_guard(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"input.grib"
            fake_grib(path)
            require_grib1(path)
            fake_grib(path,2)
            with self.assertRaisesRegex(ValueError,"GRIB1"):
                require_grib1(path)

    def test_native_staging_and_atomic_rename(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            converter=root/"grib2arl.exe";converter.write_bytes(b"stub")
            pressure=root/"p.grib";surface=root/"s.grib"
            fake_grib(pressure);fake_grib(surface)
            output=root/"output.arl"
            def popen(command,cwd,**kwargs):
                self.assertIn("-k38",command)
                self.assertEqual((Path(cwd)/"DATA.GRIB").read_bytes(),pressure.read_bytes())
                self.assertEqual((Path(cwd)/"SFC.GRIB").read_bytes(),surface.read_bytes())
                (Path(cwd)/"DATA.ARL").write_bytes(b"ARL fixture")
                proc=Mock()
                proc.poll.return_value=0;proc.returncode=0
                return proc
            with patch("hysplit_tool.native_era5.require_native_windows"), \
                 patch("hysplit_tool.native_era5.subprocess.Popen",side_effect=popen):
                staged=run_native_grib1_pair(pressure,surface,output,converter)
            self.assertEqual(staged.read_bytes(),b"ARL fixture")
            self.assertFalse(output.exists())
            self.assertTrue(pressure.exists() and surface.exists())

    def test_failed_rename_keeps_original_gribs(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            converter=root/"grib2arl.exe";converter.write_bytes(b"stub")
            pressure=root/"p.grib";surface=root/"s.grib"
            fake_grib(pressure);fake_grib(surface)
            output=root/"output.arl"
            def popen(command,cwd,**kwargs):
                (Path(cwd)/"DATA.ARL").write_bytes(b"ARL fixture")
                proc=Mock();proc.poll.return_value=0;proc.returncode=0
                return proc
            with patch("hysplit_tool.native_era5.require_native_windows"), \
                 patch("hysplit_tool.native_era5.subprocess.Popen",side_effect=popen), \
                 patch("hysplit_tool.native_era5.os.replace",side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError,"disk full"):
                    run_native_grib1_pair(pressure,surface,output,converter)
            self.assertFalse(output.with_suffix(".arl.part").exists())
            self.assertTrue(pressure.exists() and surface.exists())

    def test_failure_log_preserves_full_messages_and_previous_attempts(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            converter=root/"grib2arl.exe";converter.write_bytes(b"stub")
            pressure=root/"p.grib";surface=root/"s.grib"
            fake_grib(pressure);fake_grib(surface)
            output=root/"output.arl"
            log=output.with_suffix(".arl.log")
            log.write_text("OLD EXECUTION\n",encoding="utf-8")
            message="\n".join(f"WARNING detail {i}" for i in range(80))+"\nLAST DETAIL"
            def popen(command,cwd,**kwargs):
                work=Path(cwd)
                (work/"MESSAGE").write_text(message,encoding="utf-8")
                (work/"WARNING").write_text("full warning file",encoding="utf-8")
                (work/"CFG_ARL").write_text("pressure configuration",encoding="utf-8")
                proc=Mock();proc.poll.return_value=1131;proc.returncode=1131
                return proc
            with patch("hysplit_tool.native_era5.require_native_windows"), \
                 patch("hysplit_tool.native_era5.subprocess.Popen",side_effect=popen), \
                 patch("hysplit_tool.native_era5._grib_inventory",return_value="inventory complete"):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError,"1131"):
                        run_native_grib1_pair(pressure,surface,output,converter)
            text=log.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("OLD EXECUTION"))
            self.assertEqual(text.count("Exit code=1131"),2)
            self.assertEqual(text.count(message),2)
            for evidence in ("full warning file","pressure configuration","inventory complete","SHA256=","Command:"):
                self.assertEqual(text.count(evidence),2)
            self.assertEqual(set(x.name for x in root.iterdir()),
                             {"p.grib","s.grib","grib2arl.exe","output.arl.log"})

    def test_abandoned_converter_workspaces_are_only_removed_when_idle(self):
        with tempfile.TemporaryDirectory() as td:
            arl = Path(td) / "arl"
            arl.mkdir()
            work = arl / "hysplit_grib1_abandoned"
            work.mkdir()
            (work / "DATA.GRIB").write_bytes(b"x" * 1024)
            unrelated = arl / "keep_this"
            unrelated.mkdir()
            (unrelated / "DATA.GRIB").write_bytes(b"keep")
            with patch("hysplit_tool.native_era5._grib2arl_is_running", return_value=True):
                removed, freed = cleanup_abandoned_converter_workspaces(arl)
            self.assertEqual((removed, freed), (0, 0))
            self.assertTrue(work.exists())
            with patch("hysplit_tool.native_era5._grib2arl_is_running", return_value=False):
                removed, freed = cleanup_abandoned_converter_workspaces(arl)
            self.assertEqual((removed, freed), (1, 1024))
            self.assertFalse(work.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
