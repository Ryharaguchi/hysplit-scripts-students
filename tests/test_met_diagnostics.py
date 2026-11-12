"""Selection, native namelist and exact NOAA tdump-label regression tests."""
from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.core import StartPoint, group_starts, write_group
from hysplit_tool.csv_export import OUTPUT_COLUMNS, export_run_csv
from hysplit_tool.met_diagnostics import (
    DEFAULT_MET_VARIABLES, MET_VARIABLES, flags_for_setup, selected_variables,
)


class TestMeteorologyDefinitions(unittest.TestCase):
    def test_eight_named_options_with_unique_technical_columns(self):
        self.assertEqual(len(MET_VARIABLES), 8)
        self.assertEqual(len({item.flag for item in MET_VARIABLES}), 8)
        self.assertEqual(len({item.tdump_label for item in MET_VARIABLES}), 8)
        self.assertEqual(len({item.csv_column for item in MET_VARIABLES}), 8)
        self.assertEqual(len(DEFAULT_MET_VARIABLES), 8)
        self.assertEqual(selected_variables(reversed(DEFAULT_MET_VARIABLES)), MET_VARIABLES)
        for bad in (["TM_INVALID"], ["TM_TAMB", "TM_TAMB"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                selected_variables(bad)

    def test_selected_flag_only_and_precision_for_tdump(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            group = group_starts([
                StartPoint(datetime(2019, 1, 5, 3, tzinfo=timezone.utc),
                           -69.0174, 40.0856, 1000, "csv:2"),
            ])[0]
            folder = write_group(
                group, root, [root/"fake_met.arl"], 3, 20000,
                "AGL", diagnostics=("TM_MIXR", "TM_TAMB"),
            )
            setup = (folder/"SETUP.CFG").read_text(encoding="ascii")
            for flag in ("KAGL", "TM_PRES", "TM_TERR", "TM_TAMB", "TM_MIXR"):
                self.assertIn(flag+" = 1,",setup)
            self.assertIn(" TFMT = 2,",setup)
            for flag in ("TM_RELH", "TM_SPHU", "TM_TPOT", "TM_UWND",
                         "TM_VWND", "TM_MIXD"):
                self.assertNotIn(flag+" = 1,",setup)
            self.assertNotEqual(flags_for_setup(("TM_TAMB",)), flags_for_setup(()))

    @staticmethod
    def _realistic_tdump() -> str:
        return (
            "     1     2\n"
            "    ECMG    19     1     5     0     0\n"
            "     1 BACKWARD OMEGA\n"
            "    19     1     5     3  -69.017   40.086   1000.00\n"
            "    10 PRESSURE THETA AIR_TEMP MIXDEPTH RELHUMID SPCHUMID "
            "H2OMIXRA TERR_MSL UWIND VWIND\n"
            "     1     1    19     1     5     3     0     0    0.00"
            "  -69.017   40.086   1000.00    815.32    277.80"
            "    262.02    31.22    69.42    1.41    1.41"
            "    459.34    1.54    1.25\n"
            "     1     1    19     1     5     2     0     0   -1.00"
            "  -69.058   39.928    990.58    829.26    277.49"
            "    263.01    29.03    68.08    1.47    1.47"
            "    341.79    1.94    1.29\n"
        )

    def test_csv_selected_columns_independent_of_tdump_label_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            group = root/"20190105T0300Z_00000"
            group.mkdir()
            (group/"starts.csv").write_text(
                "trajectory_number,time_utc,latitude,longitude,height_m,origin\n"
                "1,2019-01-05T03:00:00+00:00,-69.0174,40.0856,1000,csv:2\n",
                encoding="utf-8",
            )
            (group/"tdump").write_text(self._realistic_tdump(),encoding="ascii")
            selection=("TM_MIXR", "TM_TAMB", "TM_UWND")
            report=export_run_csv(root, group_folders=[group],
                                  height_reference="AGL",
                                  selected_diagnostics=selection)
            self.assertEqual((report.trajectories, report.endpoints),(1,2))
            with report.path.open(encoding="utf-8-sig",newline="") as f:
                reader=csv.DictReader(f)
                self.assertEqual(
                    tuple(reader.fieldnames),
                    OUTPUT_COLUMNS+
                    ("air_temperature_k",
                     "water_vapor_mixing_ratio_g_per_kg_dry_air",
                     "eastward_wind_m_s"),
                )
                rows=list(reader)
            self.assertEqual(rows[0]["air_temperature_k"],"262.02")
            self.assertEqual(rows[0]["water_vapor_mixing_ratio_g_per_kg_dry_air"],
                             "1.41")
            self.assertEqual(rows[0]["eastward_wind_m_s"],"1.54")
            self.assertEqual(rows[0]["pressure_hpa"],"815.32")
            self.assertEqual(rows[0]["terrain_elevation_msl_m"],"459.34")

    def test_header_missing_requested_variable_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            group=root/"20190105T0300Z_00000"
            group.mkdir()
            (group/"starts.csv").write_text(
                "trajectory_number,time_utc,latitude,longitude,height_m,origin\n"
                "1,2019-01-05T03:00:00+00:00,-69.0174,40.0856,1000,csv:2\n",
                encoding="utf-8",
            )
            data=self._realistic_tdump()
            data=data.replace("10 PRESSURE THETA", "9 PRESSURE THETA")
            data=data.replace("H2OMIXRA ", "")
            (group/"tdump").write_text(data,encoding="ascii")
            with self.assertRaisesRegex(ValueError,"H2OMIXRA"):
                export_run_csv(root,group_folders=[group],
                               selected_diagnostics=("TM_MIXR",))
            self.assertFalse((root/"trajectories.csv").exists())

    def test_gui_allows_unchecking_variables(self):
        from hysplit_tool.gui import App
        from hysplit_tool.core import load_starts_csv
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=root/"one.csv"
            source.write_text("2019/01/05 12:00,-69.0174,40.0856,1000\n",encoding="utf-8")
            app=App()
            app.withdraw()
            try:
                app.v["csv"].set(str(source))
                self.assertTrue(all(app.met_checks[x.flag].get() for x in MET_VARIABLES))
                app.met_checks["TM_RELH"].set(False)
                app.met_checks["TM_UWND"].set(False)
                conf,*rest = app._data()
                self.assertNotIn("TM_RELH",conf["met_diagnostics"])
                self.assertNotIn("TM_UWND",conf["met_diagnostics"])
                self.assertEqual(len(conf["met_diagnostics"]),6)
                self.assertEqual(len(load_starts_csv(source)),1)
            finally:app.destroy()


if __name__ == "__main__":
    unittest.main()

