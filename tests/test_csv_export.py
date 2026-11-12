"""Verify one-CSV output, geographic diagnostics and fail-closed semantics."""
import csv
import math
import tempfile
import unittest
from pathlib import Path

from hysplit_tool.csv_export import (
    OUTPUT_COLUMNS, _geopotential_height_estimate, export_run_csv,
)


def make_group(root: Path, name: str, *, terrain: bool = True,
               complete: bool = True) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "starts.csv").write_text(
        "trajectory_number,time_utc,latitude,longitude,height_m,origin\n"
        "1,2026-10-10T00:00:00+00:00,35,135,500,csv:2\n"
        "2,2026-10-10T00:00:00+00:00,35,135,600,csv:3\n",
        encoding="utf-8",
    )
    diagnostics = "2 PRESSURE TERR_MSL" if terrain else "1 PRESSURE"
    lines = [
        "     1     1",
        "    GDAS    26    10    10     0     0",
        "     2 BACKWARD OMEGA",
        "    26 10 10 00 35.000 135.000 500.0",
        "    26 10 10 00 35.000 135.000 600.0",
        diagnostics,
        "1 1 26 10 10 00 00 00 0.0 35.000 135.000 500.0 950.0" +
        (" 120.0" if terrain else ""),
        "1 1 26 10 09 23 00 00 -1.0 34.900 134.900 510.0 948.0" +
        (" 130.0" if terrain else ""),
    ]
    if complete:
        lines += [
            "2 1 26 10 10 00 00 00 0.0 35.000 135.000 600.0 940.0" +
            (" 120.0" if terrain else ""),
            "2 1 26 10 09 23 00 00 -1.0 34.850 134.850 595.0 941.0" +
            (" 131.0" if terrain else ""),
        ]
    (folder / "tdump").write_text("\n".join(lines) + "\n", encoding="ascii")
    return folder


class TestFullExport(unittest.TestCase):
    def test_all_batches_and_height_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folders = [make_group(root, "case00"), make_group(root, "case01")]
            report = export_run_csv(root, height_reference="MSL", group_folders=folders)
            self.assertEqual(report.path, root / "trajectories.csv")
            self.assertEqual((report.groups, report.trajectories, report.endpoints), (2, 4, 8))
            self.assertEqual(report.missing_terrain_endpoints, 0)
            with report.path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), OUTPUT_COLUMNS)
                rows = list(reader)
            self.assertEqual(len(rows), 8)
            self.assertEqual(len(set((r["run_group"], r["trajectory_number"])
                                     for r in rows)), 4)
            first = rows[0]
            self.assertEqual(first["source_csv_row"], "2")
            self.assertEqual(first["start_time_jst"], "2026-10-10T09:00+09:00")
            self.assertEqual(first["endpoint_time_jst"], "2026-10-10T09:00+09:00")
            self.assertEqual(first["start_height_reference"], "MSL")
            self.assertEqual(first["pressure_hpa"], "950.00")
            self.assertEqual(first["terrain_elevation_msl_m"], "120.00")
            self.assertEqual(first["trajectory_height_agl_m"], "500.00")
            self.assertEqual(first["trajectory_height_msl_m_est"], "620.00")
            self.assertAlmostEqual(
                float(first["trajectory_geopotential_height_m_est"]),
                _geopotential_height_estimate(620.0), delta=0.005,
            )

    def test_no_terrain_no_fictitious_geo_height(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            group = make_group(root, "case", terrain=False)
            report = export_run_csv(root, group_folders=[group])
            self.assertEqual(report.missing_terrain_endpoints, 4)
            with report.path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["trajectory_height_agl_m"], "500.00")
            self.assertEqual(rows[0]["terrain_elevation_msl_m"], "")
            self.assertEqual(rows[0]["trajectory_height_msl_m_est"], "")
            self.assertEqual(rows[0]["trajectory_geopotential_height_m_est"], "")

    def test_missing_track_rejects_whole_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            group = make_group(root, "case", complete=False)
            with self.assertRaisesRegex(ValueError, "Missing trajectories"):
                export_run_csv(root, group_folders=[group])
            self.assertFalse((root / "trajectories.csv").exists())
            self.assertFalse((root / "trajectories.csv.part").exists())

    def test_invalid_diagnostics_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            group = make_group(root, "case")
            file = group / "tdump"
            file.write_text(file.read_text().replace("2 PRESSURE TERR_MSL", "2 PRESSURE"),
                            encoding="ascii")
            with self.assertRaisesRegex(ValueError, "diagnostic"):
                export_run_csv(root, group_folders=[group])
            self.assertFalse((root / "trajectories.csv").exists())

    def test_spherical_geopotential_estimate_is_below_geometric(self):
        for z in (100.0, 2000.0, 10000.0, 30000.0):
            estimate = _geopotential_height_estimate(z)
            self.assertTrue(math.isfinite(estimate))
            self.assertLess(estimate, z)


if __name__ == "__main__":
    unittest.main()
