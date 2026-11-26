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
    match_cds_pairs, require_grib1, run_native_grib1_pair,
    convert_cds_grib1_to_arl, cleanup_abandoned_converter_workspaces,
    check_arco_conversion_space,
)
from hysplit_tool.validation import CoverageReport
from hysplit_tool.time_windows import TimeWindow
from hysplit_tool.batch_meteorology import (
    _verified_arco_arl_cache, _verified_cds_arl_cache,
)
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
                disk_usage.return_value.free = 19 * (1024 ** 3)
                with self.assertRaisesRegex(OSError, "No conversion was started"):
                    check_arco_conversion_space(
                        10 * (1024 ** 3), root / "cache_pressure.grib", notify.append,
                    )
            self.assertTrue(any("estimated peak needed=20.00 GiB" in x for x in notify))
            self.assertEqual(list(root.iterdir()), [])

    def test_arco_disk_preflight_includes_copy_if_hardlinks_are_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("hysplit_tool.native_era5._probe_hardlink", return_value=False), \
                 patch("hysplit_tool.native_era5.shutil.disk_usage") as disk_usage:
                disk_usage.return_value.free = 29 * (1024 ** 3)
                with self.assertRaisesRegex(OSError, "estimated peak minimum 30.00 GiB"):
                    check_arco_conversion_space(
                        10 * (1024 ** 3), root / "cache_pressure.grib",
                    )

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
            upper = d / "era5_pressure_202610_08-12_abcd1234_northern_hemisphere_hysplit.grib"
            surface = d / "era5_single_202610_08-12_abcd1234_northern_hemisphere_hysplit.grib"
            fake_grib(upper)
            fake_grib(surface)
            invocations = []

            def run_mock(command, cwd, **kwargs):
                invocations.append(command)
                self.assertEqual(command[0], str(converter.resolve()))
                self.assertIn("-k38", command)
                self.assertTrue((Path(cwd) / "DATA.GRIB").is_file())
                self.assertTrue((Path(cwd) / "SFC.GRIB").is_file())
                (Path(cwd) / "DATA.ARL").write_bytes(b"mocked ARL records")
                return CompletedProcess(command, 0)

            def coverage_mock(paths, earliest, latest, used_filedates, **kwargs):
                self.assertTrue(all(Path(p).is_file() for p in paths))
                self.assertEqual(Path(used_filedates).resolve(), filedates.resolve())
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
            upper_keep = d / "era5_pressure_202610_09-12_abcd1234_northern_hemisphere_hysplit.grib"
            surface_keep = d / "era5_single_202610_09-12_abcd1234_northern_hemisphere_hysplit.grib"
            fake_grib(upper_keep)
            fake_grib(surface_keep)
            with patch("hysplit_tool.native_era5.require_native_windows"), \
                 patch("hysplit_tool.native_era5.subprocess.run", side_effect=run_mock), \
                 patch("hysplit_tool.native_era5.check_coverage", side_effect=coverage_mock):
                kept_paths = convert_cds_grib1_to_arl(
                    [upper_keep, surface_keep], d / "output",
                    datetime(2026, 10, 9, tzinfo=UTC),
                    datetime(2026, 10, 10, tzinfo=UTC),
                    hysplit_executable=exe, keep_sources=True)
            self.assertEqual(len(invocations), 2)
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].read_bytes(), b"mocked ARL records")
            self.assertFalse(paths[0].with_suffix(paths[0].suffix + ".part").exists())
            self.assertFalse(upper.exists())
            self.assertFalse(surface.exists())
            self.assertEqual(len(kept_paths), 1)
            self.assertTrue(upper_keep.is_file())
            self.assertTrue(surface_keep.is_file())
            metadata = json.loads(paths[0].with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["field_profile"], "hysplit")
            self.assertEqual(metadata["pair_count"], 1)
            self.assertEqual(len(metadata["source_grib_files"]), 2)
            window = TimeWindow(
                datetime(2026, 10, 10, tzinfo=UTC),
                datetime(2026, 10, 11, tzinfo=UTC),
            )
            with patch(
                "hysplit_tool.batch_meteorology.check_coverage",
                return_value=CoverageReport(True, ("validated",)),
            ):
                cached = _verified_cds_arl_cache(d / "output", window, exe)
            self.assertEqual(cached, tuple(paths))

    def test_failed_arl_stage_copy_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            execdir = root / "exec"
            execdir.mkdir()
            converter = execdir / "grib2arl.exe"
            converter.write_bytes(b"stub")
            pressure = root / "pressure.grib"
            surface = root / "surface.grib"
            fake_grib(pressure)
            fake_grib(surface)
            destination = root / "arco_era5_cache.arl"

            def run_mock(command, cwd, **kwargs):
                (Path(cwd) / "DATA.ARL").write_bytes(b"mocked ARL records")
                return CompletedProcess(command, 0)

            real_copyfile = shutil.copyfile

            def copy_then_fail(source, target):
                target = Path(target)
                if target.name.endswith(".arl.part"):
                    target.write_bytes(b"incomplete ARL")
                    raise OSError("disk full")
                return real_copyfile(source, target)

            with patch("hysplit_tool.native_era5.require_native_windows"), \
                 patch("hysplit_tool.native_era5.subprocess.run", side_effect=run_mock), \
                 patch("hysplit_tool.native_era5.shutil.copyfile", side_effect=copy_then_fail):
                with self.assertRaisesRegex(OSError, "disk full"):
                    run_native_grib1_pair(
                        pressure, surface, destination, converter,
                    )

            self.assertFalse(destination.with_suffix(".arl.part").exists())

    def test_arco_cache_requires_current_relative_humidity_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            arl_dir = root / "arl"
            arl_dir.mkdir()
            execdir = root / "exec"
            execdir.mkdir()
            exe = execdir / "hyts_std.exe"
            filedates = execdir / "filedates.exe"
            exe.write_bytes(b"stub")
            filedates.write_bytes(b"stub")
            source = root / "arco_era5_20260602T1025_20260605T1135_northern37_hysplit.nc"
            output = arl_dir / "arco_era5_cache.arl"
            output.write_bytes(b"arl")
            window = TimeWindow(
                datetime(2026, 6, 2, 10, tzinfo=UTC),
                datetime(2026, 6, 5, 11, tzinfo=UTC),
            )
            metadata_path = output.with_suffix(".json")
            metadata = {
                "source": str(source),
                "pressure_levels_hpa": list(ARCO_PRESSURE_LEVELS),
                "first": window.start.isoformat(),
                "last": window.end.isoformat(),
                "humidity_quality_control": {"policy": "legacy cutoff"},
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            with patch(
                "hysplit_tool.batch_meteorology.check_coverage",
                return_value=CoverageReport(True, ("validated",)),
            ) as coverage:
                self.assertEqual(
                    _verified_arco_arl_cache(arl_dir, window, exe), (),
                )
                coverage.assert_not_called()

                metadata["humidity_quality_control"]["policy"] = (
                    "Negative q is floored only for RH; "
                    "No magnitude or prevalence cutoff is applied."
                )
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                self.assertEqual(
                    _verified_arco_arl_cache(arl_dir, window, exe), (output,),
                )

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
