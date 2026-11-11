import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.meteo import RemoteFile, download_arl_file, local_arl_files, plan_archives
from hysplit_tool.era5 import region_around, _month_days

UTC = timezone.utc


class TestMeteo(unittest.TestCase):
    def test_gdas_boundary(self):
        start = datetime(2026, 1, 31, 0, 0, tzinfo=UTC)
        end = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
        plan = plan_archives("gdas1", start, end)
        self.assertEqual([p.name for p in plan], ["gdas1.jan26.w5", "gdas1.feb26.w1"])
        self.assertIn("/gdas1/2026/gdas1.jan26.w5", plan[0].urls[0])

    def test_gdas_week(self):
        start = datetime(2026, 10, 8, 10, tzinfo=UTC)
        end = datetime(2026, 10, 10, 0, tzinfo=UTC)
        plan = plan_archives("gdas1", start, end)
        self.assertEqual([p.name for p in plan], ["gdas1.oct26.w2"])
        with self.assertRaises(ValueError):
            plan_archives("ERA5", start, end)

    def test_gfs_daily(self):
        start = datetime(2026, 9, 10, 0, tzinfo=UTC)
        end = datetime(2026, 9, 11, 0, tzinfo=UTC)
        plan = plan_archives("gfs0p25", start, end)
        self.assertEqual([p.name for p in plan], [
            "20260909_gfs0p25", "20260910_gfs0p25", "20260911_gfs0p25",
        ])
        self.assertIn("/gfs0p25/2026/09/", plan[0].urls[0])

    def test_atomic_download_mock(self):
        with tempfile.TemporaryDirectory() as td:
            seen = []

            def fake_urlopen(request, timeout):
                seen.append(request.full_url)
                return io.BytesIO(b"FAKE ARL BINARY")

            spec = RemoteFile("met.bin", ("https://example.test/met.bin",))
            path = download_arl_file(spec, Path(td), opener=fake_urlopen)
            self.assertEqual(path.read_bytes(), b"FAKE ARL BINARY")
            self.assertFalse((Path(td) / "met.bin.part").exists())
            self.assertEqual(download_arl_file(spec, Path(td), opener=fake_urlopen), path)
            self.assertEqual(len(seen), 1)
            self.assertEqual(local_arl_files(str(path)), [path])

    def test_era5_bounding_region_and_months(self):
        self.assertEqual(region_around(35, 135), (60, 100, 10, 170))
        months = _month_days(datetime(2026, 1, 31), datetime(2026, 2, 2))
        self.assertEqual(months, {(2026, 1): {"31"}, (2026, 2): {"01", "02"}})


if __name__ == "__main__":
    unittest.main()
