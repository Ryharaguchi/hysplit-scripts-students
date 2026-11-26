import json
import tempfile
import unittest
from pathlib import Path

from hysplit_tool.retention import (
    cleanup_successful_meteorology,
    cleanup_successful_run,
)


class TestRetention(unittest.TestCase):
    def test_successful_run_keeps_results_and_removes_work_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            group = root / "group-001"
            group.mkdir(parents=True)
            (group / "tdump").write_text("trajectory data", encoding="ascii")
            (root / "observations.csv").write_text("duplicate input", encoding="ascii")
            result = root / "Trajectory_period_01.csv"
            result.write_text("results", encoding="ascii")
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="ascii")

            cleanup_successful_run(root, [group])

            self.assertFalse(group.exists())
            self.assertTrue((root / "observations.csv").is_file())
            self.assertTrue(result.is_file())
            self.assertTrue(manifest.is_file())

    def test_meteorology_cleanup_keeps_arl_and_removes_used_verified_source(self):
        with tempfile.TemporaryDirectory() as temp:
            met = Path(temp) / "met"
            arl_dir = met / "arl"
            arl_dir.mkdir(parents=True)
            arl = arl_dir / "arco_era5_test.arl"
            arl.write_bytes(b"ARL")
            metadata = {
                "source": str(met / "arco_era5_test_northern37_hysplit.nc"),
                "validation": "NOAA Windows filedates (timestamps only)",
                "time_count": 3,
            }
            metadata_path = arl.with_suffix(".json")
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            (arl_dir / "arco_era5_test.arl.log").write_text("log", encoding="utf-8")
            source = Path(metadata["source"])
            source.write_bytes(b"NetCDF")
            unused = met / "unrelated.nc"
            unused.write_bytes(b"keep")

            cleanup_successful_meteorology(met, [arl])

            self.assertTrue(arl.exists())
            self.assertTrue(metadata_path.exists())
            self.assertFalse(source.exists())
            self.assertFalse((arl_dir / "arco_era5_test.arl.log").exists())
            self.assertTrue(unused.is_file())

    def test_optional_source_retention_keeps_netcdf(self):
        with tempfile.TemporaryDirectory() as temp:
            met = Path(temp) / "met"
            arl_dir = met / "arl"
            arl_dir.mkdir(parents=True)
            arl = arl_dir / "arco_era5_test.arl"
            arl.write_bytes(b"ARL")
            source = met / "arco_era5_test_northern37_hysplit.nc"
            source.write_bytes(b"NetCDF")
            arl.with_suffix(".json").write_text(json.dumps({
                "source": str(source),
                "validation": "NOAA Windows filedates",
                "time_count": 3,
            }), encoding="utf-8")

            cleanup_successful_meteorology(met, [arl], keep_sources=True)

            self.assertTrue(arl.is_file())
            self.assertTrue(source.is_file())


if __name__ == "__main__":
    unittest.main()
