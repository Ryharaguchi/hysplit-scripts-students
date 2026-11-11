"""Offline unit tests; no licensed models or external meteorology required."""

import csv
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.core import (
    StartPoint, execute_groups, expand_starts, fixed_starts, group_starts,
    load_starts_csv, parse_heights, parse_time_offsets, parse_utc,
    parse_xy_offsets, render_control, write_group,
)

UTC = timezone.utc


class TestCore(unittest.TestCase):
    def test_parse_heights_inclusive(self):
        values = parse_heights("500:10000:100")
        self.assertEqual((len(values), values[0], values[-1]), (96, 500, 10000))
        self.assertEqual(parse_heights("500,600,900"), [500, 600, 900])
        with self.assertRaises(ValueError):
            parse_heights("0:10:1")
        with self.assertRaises(ValueError):
            parse_heights("500:1000:-100")

    def test_timestamps_and_offsets(self):
        self.assertEqual(parse_utc("2026-10-10T09:00+09:00"),
                         datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
        self.assertEqual(parse_utc("2026-10-10 00:00"),
                         datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
        self.assertEqual(parse_time_offsets("-0.5,0,1"), [-0.5, 0, 1])
        self.assertEqual(parse_xy_offsets("0,0;5,-5"), [(0, 0), (5, -5)])
        with self.assertRaises(ValueError):
            parse_xy_offsets("0;5")

    def test_case_generation_count_groups(self):
        initial = datetime(2026, 10, 10, tzinfo=UTC)
        bases = fixed_starts(initial, initial, 6, 35, 135, parse_heights("500:10000:100"))
        points = expand_starts(bases, [(0, 0), (5, 0), (-5, 0), (0, 5), (0, -5)], [-1, 0, 1])
        self.assertEqual(len(points), 1440)
        # 480 starts at each UTC hour, 100 starts per CONTROL -> 15 runs.
        groups = group_starts(points, max_points=100)
        self.assertEqual(len(groups), 15)
        self.assertTrue(all(len(g.points) <= 100 for g in groups))
        self.assertEqual(len({g.when for g in groups}), 3)

    def test_month_boundary_and_minutes(self):
        initial = parse_utc("2026-01-01 00:30")
        p = StartPoint(initial, 35, 135, 500)
        shifted = expand_starts([p], [(0, 0)], [-1.0])
        self.assertEqual(shifted[0].when, parse_utc("2025-12-31 23:30"))
        control = render_control(group_starts(shifted)[0], [Path("/tmp/fake.arl")], 24, 10000)
        self.assertTrue(control.startswith("25 12 31 23 30\n1\n"))
        self.assertIn("\n-24\n0\n10000.0\n1\n", control)

    def test_csv_and_control_and_cfg(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            csvfile = base / "points.csv"
            csvfile.write_text("time_utc,latitude,longitude,height_m\n2026-10-10T00:00Z,35,135,500\n", encoding="utf-8")
            points = load_starts_csv(csvfile)
            self.assertEqual(len(points), 1)
            bdy = base / "hysplit" / "bdyfiles"
            bdy.mkdir(parents=True)
            met = base / "met" / "gdas1.oct26.w2"
            met.parent.mkdir()
            met.write_bytes(b"FAKE ARL")
            folder = write_group(group_starts(points)[0], base / "run", [met], 48, 20000, "MSL", bdy)
            self.assertIn("KMSL = 1", (folder / "SETUP.CFG").read_text())
            self.assertIn(bdy.resolve().as_posix() + "/", (folder / "ASCDATA.CFG").read_text())
            self.assertIn("gdas1.oct26.w2", (folder / "CONTROL").read_text())
            with (folder / "starts.csv").open() as file:
                self.assertEqual(list(csv.DictReader(file))[0]["trajectory_number"], "1")

    @unittest.skipIf(os.name == "nt", "POSIX executable-stub test; Windows HYSPLIT requires external binary")
    def test_parallel_runner_with_stub(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            stub = base / "fake_hyts"
            stub.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\nPath('tdump').write_text('OK\\n')\n")
            stub.chmod(0o755)
            folders = [base / f"case{i}" for i in range(3)]
            for f in folders:
                f.mkdir()
            results = execute_groups(stub, folders, workers=2)
            self.assertEqual(len(results), 3)
            self.assertTrue(all(ok for _, ok in results))


if __name__ == "__main__":
    unittest.main()
