"""HYSPLIT trajectory output interval and integration-step regression tests."""
import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.core import (
    StartPoint, group_starts, validate_time_controls, write_group,
)
from hysplit_tool.csv_export import export_run_csv


class TestTimeStepControls(unittest.TestCase):
    def test_defaults_and_valid_divisors(self):
        self.assertEqual(validate_time_controls(), (60,0))
        self.assertEqual(validate_time_controls(10,0),(10,0))
        self.assertEqual(validate_time_controls(10,5),(10,5))
        self.assertEqual(validate_time_controls(60,15),(60,15))
        self.assertEqual(validate_time_controls(1440,60),(1440,60))

    def test_reject_fractional_inconsistent_or_out_of_range_minutes(self):
        for tout,delt in (
            (0,0),(-1,0),(1441,0),
            (10,7),(10,15),(10,-1),(10,61),
            ("10.5",0),(10,"4.2"),("abc",0),
        ):
            with self.subTest(tout=tout,delt=delt):
                with self.assertRaises(ValueError):
                    validate_time_controls(tout,delt)

    def test_write_group_writes_tout_delt_without_changing_legacy_default(self):
        t=datetime(2019,1,5,3,tzinfo=timezone.utc)
        points=[StartPoint(t,-69.0,40.0,1000)]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            groups=group_starts(points)
            folder=write_group(
                groups[0],root,[root/"met.arl"],3,20000,"AGL",
                output_interval_minutes=10,integration_step_minutes=5
            )
            text=(folder/"SETUP.CFG").read_text(encoding="ascii")
            self.assertIn(" TOUT = 10,",text)
            self.assertIn(" DELT = 5.0,",text)
            self.assertIn(" TM_PRES = 1,",text)
            self.assertIn(" TM_TERR = 1,",text)
            with self.assertRaises(ValueError):
                write_group(groups[0],root,[root/"met.arl"],3,20000,"AGL",
                            output_interval_minutes=10,integration_step_minutes=15)

    def test_default_gui_auto_step_advanced_toggle(self):
        from hysplit_tool.gui import App
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/"start.csv"
            source.write_text("2019/01/05 12:00,-69.0,40.0,1000\n",encoding="utf-8")
            app=App()
            app.withdraw()
            try:
                app.v["csv"].set(str(source))
                conf,*_=app._data()
                self.assertEqual(conf["output_interval_minutes"],"10")
                self.assertEqual(conf["integration_step_minutes"],"0")
                self.assertFalse(app.show_advanced_timestep.get())
                self.assertFalse(app.advanced_time_frame.winfo_ismapped())

                app.show_advanced_timestep.set(True)
                app._toggle_advanced_time()
                app.v["integration_step_minutes"].set("5")
                conf,*_=app._data()
                self.assertEqual(conf["integration_step_minutes"],"5")
                app.v["integration_step_minutes"].set("15")
                with self.assertRaises(ValueError):
                    app._data()

                app.show_advanced_timestep.set(False)
                app._toggle_advanced_time()
                conf,*_=app._data()
                self.assertEqual(conf["integration_step_minutes"],"0")
            finally:
                app.destroy()

    def test_ten_minute_age_uses_timestamps_not_tdump_rounded_hours(self):
        # HYSPLIT TFMT=2 writes -0.17 h for -10 min, so reconstruct from
        # endpoint UTC timestamp to preserve numerical integrity in CSV.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            group=root/"20190105T0300Z_00000"
            group.mkdir()
            (group/"starts.csv").write_text(
                "trajectory_number,time_utc,latitude,longitude,height_m,origin\n"
                "1,2019-01-05T03:00:00+00:00,-69.0,40.0,1000,csv:1\n",
                encoding="utf-8",
            )
            data=(
                "1 1\n"
                "ECMG 19 1 5 0 0\n"
                "1 BACKWARD OMEGA\n"
                "19 1 5 3 -69.000 40.000 1000.00\n"
                "2 PRESSURE TERR_MSL\n"
                "1 1 19 1 5 3 0 0 0.00 -69.000 40.000 1000.00 820.00 100.00\n"
                "1 1 19 1 5 2 50 0 -0.17 -69.020 39.980 995.00 823.00 101.00\n"
            )
            (group/"tdump").write_text(data,encoding="ascii")
            report=export_run_csv(root,group_folders=[group],height_reference="AGL")
            with report.path.open(encoding="utf-8-sig",newline="") as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(rows[0]["age_hours"],"0.000000")
            self.assertEqual(rows[1]["age_hours"],"-0.166667")
            self.assertEqual(rows[1]["endpoint_time_utc"],"2019-01-05T02:50+00:00")


if __name__ == "__main__":
    unittest.main()

