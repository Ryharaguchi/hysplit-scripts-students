"""JST CSV and radial sensitivity tests (pure Python, no API traffic)."""
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.core import (
    StartPoint, destination_location, expand_observations, group_starts,
    load_starts_csv, parse_jst, EARTH_RADIUS_KM,
)

UTC = timezone.utc


def haversine(lat1, lon1, lat2, lon2):
    a = math.sin(math.radians(lat2-lat1)/2)**2 + \
        math.cos(math.radians(lat1))*math.cos(math.radians(lat2)) * \
        math.sin(math.radians(lon2-lon1)/2)**2
    return 2*EARTH_RADIUS_KM*math.asin(min(1.0, math.sqrt(a)))


class TestJstCsv(unittest.TestCase):
    def test_time_formats_and_explicit_timezone(self):
        expected = datetime(2026, 10, 10, 0, tzinfo=UTC)
        for value in (
            "2026/10/10 09:00", "2026-10-10 09:00:00",
            "2026.10.10 09:00", "202610100900",
            "20261010090000", "2026-10-10T09:00:00+09:00",
            "2026-10-10T00:00Z", "2026/10/10 09:00 JST",
            "2026-10-10 00:00 UTC",
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_jst(value), expected)
        self.assertEqual(parse_jst("2026/10/10 09:00:12"), expected)
        self.assertEqual(parse_jst("2026/10/10 09:00:30"),
                         datetime(2026, 10, 10, 0, 1, tzinfo=UTC))
        with self.assertRaises(ValueError):
            parse_jst("2026/99/10 09:00")

    def test_optional_header_any_names_and_csv_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            header = root / "head.csv"
            noheader = root / "nohead.csv"
            rows = "2026/10/10 09:00,35.2,135.4,500\n2026-10-10 09:01,35.3,135.5,600\n"
            header.write_text("観測時刻,緯度,経度,高度\n" + rows, encoding="utf-8-sig")
            noheader.write_text(rows, encoding="utf-8")
            for path in (header, noheader):
                with self.subTest(path=path):
                    pts = load_starts_csv(path)
                    self.assertEqual(len(pts), 2)
                    self.assertEqual([p.height_m for p in pts], [500., 600.])
                    self.assertEqual(pts[0].when, datetime(2026, 10, 10, 0, tzinfo=UTC))
                    self.assertEqual(pts[1].when, datetime(2026, 10, 10, 0, 1, tzinfo=UTC))
            noheader.write_text("2026/10/10 09:00,35.2,135.4,500\n", encoding="cp932")
            self.assertEqual(len(load_starts_csv(noheader)), 1)

    def test_cp932_and_errors_report_line(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cp932.csv"
            path.write_bytes("年月日,北緯,東経,高度\n2026/10/10 09:00,35,135,1000\n".encode("cp932"))
            self.assertEqual(load_starts_csv(path)[0].height_m, 1000.0)
            path.write_text("not-a-time,35,135,1000\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "row 1"):
                load_starts_csv(path)
            path.write_text("時刻,緯度,経度,高度\n2026/10/10 09:00,35,135\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "row 2"):
                load_starts_csv(path)


class TestSensitivity(unittest.TestCase):
    def test_nine_directions_have_same_radial_distance(self):
        lat, lon = 35.0, 135.0
        baseline = StartPoint(datetime(2026, 10, 10, 0, tzinfo=UTC), lat, lon, 800)
        pts = expand_observations([baseline], 5.0, 0.0)
        self.assertEqual(len(pts), 9)
        self.assertEqual((pts[0].latitude, pts[0].longitude), (lat, lon))
        bearings = []
        for p in pts[1:]:
            km = haversine(lat, lon, p.latitude, p.longitude)
            self.assertAlmostEqual(km, 5.0, places=4)
            bearings.append((p.latitude, p.longitude))
        self.assertEqual(len(set(bearings)), 8)

    def test_time_shift_and_grouping(self):
        when = parse_jst("2026/10/10 09:00")
        bases = [StartPoint(when, 35, 135, 500),
                 StartPoint(when, 35, 135, 600)]
        pts = expand_observations(bases, 3, 30)
        self.assertEqual(len(pts), 2*9*3)
        self.assertEqual(len(group_starts(pts)), 3)
        self.assertEqual(sorted({p.when.isoformat() for p in pts}),
                         ["2026-10-09T23:30:00+00:00",
                          "2026-10-10T00:00:00+00:00",
                          "2026-10-10T00:30:00+00:00"])
        self.assertEqual(len(expand_observations(bases, 0, 0)), 2)

    def test_pole_and_dateline(self):
        lat, lon = destination_location(89.9, 179.9, 90, 100)
        self.assertTrue(-90 <= lat <= 90 and -180 <= lon <= 180)
        self.assertAlmostEqual(haversine(89.9, 179.9, lat, lon), 100, places=4)

    def test_reject_invalid_shift(self):
        baseline = StartPoint(parse_jst("2026/10/10 09:00"), 35, 135, 500)
        for distance, time in [(-1, 0), (0, -1), (0, 0.5), (float("nan"), 0)]:
            with self.subTest(distance=distance, time=time), self.assertRaises(ValueError):
                expand_observations([baseline], distance, time)


if __name__ == "__main__":
    unittest.main()
