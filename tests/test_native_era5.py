"""Native GRIB1 -> ARL tests use a process stub, not meteorological data."""
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from hysplit_tool.native_era5 import (
    match_cds_pairs, require_grib1, run_native_grib1_pair,
    convert_cds_grib1_to_arl,
)
from hysplit_tool.validation import CoverageReport

UTC = timezone.utc


def fake_grib(path: Path, edition: int = 1):
    path.write_bytes(b"GRIB\x00\x00\x00" + bytes([edition]) + b"EXAMPLE" * 12)


class TestNativeEra5(unittest.TestCase):
    def test_pairs_and_grib_guard(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            pressure = d / "era5_pressure_202610_08-12_abcd1234.grib"
            surface = d / "era5_single_202610_08-12_abcd1234.grib"
            fake_grib(pressure)
            fake_grib(surface)
            self.assertEqual(
                list(match_cds_pairs([surface, pressure]).values())[0],
                (pressure, surface),
            )
            require_grib1(pressure)
            fake_grib(surface, 2)
            with self.assertRaisesRegex(ValueError, "GRIB1"):
                require_grib1(surface)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                match_cds_pairs([pressure])

    def test_native_conversion_and_arl_validation(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            execdir = d / "exec"
            execdir.mkdir()
            exe = execdir / "hyts_std.exe"
            converter = execdir / "grib2arl.exe"
            filedates = execdir / "filedates.exe"
            for path in (exe, converter, filedates):
                path.write_bytes(b"test")
            upper = d / "era5_pressure_202610_08-12_abcd1234.grib"
            surface = d / "era5_single_202610_08-12_abcd1234.grib"
            fake_grib(upper)
            fake_grib(surface)
            invocations = []

            def run_mock(command, cwd, **kwargs):
                invocations.append(command)
                self.assertEqual(command[0], str(converter.resolve()))
                self.assertIn("-k13", command)
                self.assertTrue((Path(cwd) / "DATA.GRIB").is_file())
                self.assertTrue((Path(cwd) / "SFC.GRIB").is_file())
                (Path(cwd) / "DATA.ARL").write_bytes(b"mocked ARL records")
                return CompletedProcess(command, 0)

            def coverage_mock(paths, earliest, latest, used_filedates, **kwargs):
                self.assertTrue(all(Path(p).is_file() for p in paths))
                self.assertEqual(Path(used_filedates), filedates)
                self.assertAlmostEqual(kwargs["max_gap_hours"], 1.01)
                return CoverageReport(True, ("validated",))

            with patch("hysplit_tool.native_era5.require_native_windows"), \
                 patch("hysplit_tool.native_era5.subprocess.run", side_effect=run_mock), \
                 patch("hysplit_tool.native_era5.check_coverage", side_effect=coverage_mock):
                paths = convert_cds_grib1_to_arl(
                    [upper, surface], d / "output",
                    datetime(2026, 10, 10, tzinfo=UTC),
                    datetime(2026, 10, 11, tzinfo=UTC),
                    hysplit_executable=exe)
            self.assertEqual(len(invocations), 1)
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].read_bytes(), b"mocked ARL records")
            self.assertFalse(paths[0].with_suffix(paths[0].suffix + ".part").exists())


if __name__ == "__main__":
    unittest.main()
