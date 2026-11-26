import csv
import gc
import json
import queue
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hysplit_tool.core import StartPoint, group_starts
from hysplit_tool.gui import App
from hysplit_tool.progress import TaskProgress
from hysplit_tool.time_windows import windows_for_starts


UTC = timezone.utc


class TestPeriodPipeline(unittest.TestCase):
    def test_next_period_prepares_while_ready_period_runs_and_exports_separately(self):
        # Earlier GUI tests leave Tk variables for cyclic GC. Collect them on
        # the main thread before this test starts executor worker threads.
        gc.collect()
        starts = [
            datetime(2026, 10, 3, 12, tzinfo=UTC),
            datetime(2026, 10, 21, 12, tzinfo=UTC),
        ]
        points = [StartPoint(when, 35.0, 135.0, 500.0, f"csv:{i}")
                  for i, when in enumerate(starts, 1)]
        groups = group_starts(points, max_points=1)
        windows = windows_for_starts(starts, 1)
        self.assertEqual(len(windows), 2)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "model" / "exec" / "hyts_std.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"test")
            bdyfiles = executable.parent.parent / "bdyfiles"
            bdyfiles.mkdir()
            input_csv = root / "starts.csv"
            input_csv.write_text("test input\n", encoding="utf-8")
            met_dir = root / "met" / "era5_cds"
            met_dir.mkdir(parents=True)
            met_files = [met_dir / "period1.arl", met_dir / "period2.arl"]
            for path in met_files:
                path.write_bytes(b"ARL")
                path.with_suffix(".json").write_text(json.dumps({
                    "validation": "NOAA Windows filedates (timestamps only)",
                    "time_count": 2,
                }), encoding="utf-8")

            conf = {
                "executable": str(executable),
                "csv": str(input_csv),
                "effective_workers": 1,
                "height_ref": "AGL",
                "internal_model_top_m_agl": "50000",
                "met_diagnostics": [],
                "hours": 1,
                "output_interval_minutes": 60,
                "integration_step_minutes": 0,
                "era5_field_profile": "hysplit",
                "local_met": "",
            }
            notes = []
            app = SimpleNamespace(events=queue.Queue(), _notify=notes.append)
            events = []
            first_group_started = threading.Event()

            def fake_execute(_executable, folder, _notify):
                folder = Path(folder)
                with (folder / "starts.csv").open(newline="", encoding="utf-8") as handle:
                    start = next(csv.DictReader(handle))
                when = datetime.fromisoformat(start["time_utc"])
                year, month, day = when.year % 100, when.month, when.day
                start_header = (
                    f"{year:02d} {month:02d} {day:02d} {when.hour:02d} "
                    f"{when.minute:02d} {float(start['latitude']):.3f} "
                    f"{float(start['longitude']):.3f} {float(start['height_m']):.1f}"
                )
                endpoint = when.replace(hour=(when.hour - 1) % 24)
                if when.hour == 0:
                    endpoint = endpoint.replace(day=when.day - 1)
                tdump = [
                    "1 1", "GDAS 26 10 01 00 0", "1 BACKWARD OMEGA",
                    start_header, "2 PRESSURE TERR_MSL",
                    f"1 1 {year:02d} {month:02d} {day:02d} {when.hour:02d} {when.minute:02d} 00 0.0 "
                    f"{float(start['latitude']):.3f} {float(start['longitude']):.3f} "
                    f"{float(start['height_m']):.1f} 950.0 100.0",
                    f"1 1 {endpoint.year % 100:02d} {endpoint.month:02d} {endpoint.day:02d} "
                    f"{endpoint.hour:02d} {when.minute:02d} 00 -1.0 "
                    f"{float(start['latitude']) - .1:.3f} {float(start['longitude']) - .1:.3f} "
                    f"{float(start['height_m']) + 1:.1f} 948.0 110.0",
                ]
                (folder / "tdump").write_text("\n".join(tdump) + "\n", encoding="ascii")
                events.append("trajectory_started")
                first_group_started.set()
                return folder, True

            def fake_prepare(_provider, requested_windows, _target, _bbox, **kwargs):
                callback = kwargs["on_window_ready"]
                for index, window in enumerate(requested_windows):
                    kwargs["progress"].complete(f"download {index + 1}")
                    kwargs["progress"].complete(f"convert {index + 1}")
                    callback(window, (met_files[index],))
                    if index == 0:
                        self.assertTrue(first_group_started.wait(2))
                        output_root = next((root / "runs").iterdir())
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            if list(output_root.glob("Trajectory_period_01_*.csv")):
                                events.append("period1_csv_written")
                                break
                            time.sleep(0.01)
                        self.assertIn("period1_csv_written", events)
                        events.append("period2_download_started")
                return SimpleNamespace(files=tuple(met_files))

            def fake_coverage(files, first, last, _filedates, **kwargs):
                return SimpleNamespace(
                    verified=True, messages=(), first=first, last=last, time_count=2,
                )

            with patch("hysplit_tool.gui.execute_group", side_effect=fake_execute), \
                 patch("hysplit_tool.gui.prepare_meteorology", side_effect=fake_prepare), \
                 patch("hysplit_tool.gui.check_coverage", side_effect=fake_coverage), \
                 patch("hysplit_tool.gui.files_for_window",side_effect=lambda paths,window:list(paths)), \
                 patch("hysplit_tool.gui.create_map", return_value=(
                     root / "map.html", root / "map.geojson", 2, 2,
                 )):
                progress = TaskProgress(11, lambda _event: None)
                App._run_period_pipeline(
                    app, conf, points, groups, min(starts), max(starts),
                    "era5_cds", (90.0, -180.0, 0.0, 180.0), root, met_dir,
                    windows, progress,
                )

            run_root = Path(app.events.get_nowait()[1])
            outputs = sorted(run_root.glob("Trajectory_period_*.csv"))
            self.assertEqual(len(outputs), 2)
            self.assertFalse((run_root / "trajectories.csv").exists())
            self.assertIn("period1_csv_written", events)
            self.assertIn("period2_download_started", events)
            manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_status"], "complete")
            self.assertEqual([item["status"] for item in manifest["periods"]],
                             ["complete", "complete"])
            self.assertEqual(manifest["observation_csv_copy"], "observations.csv")
            self.assertTrue(manifest["meteorology_cache_retained"])
            self.assertTrue(manifest["recalculation_assets"]["ready"])
            self.assertEqual(
                manifest["recalculation_assets"]["observation_csv"],
                "observations.csv",
            )
            self.assertEqual(
                len(manifest["recalculation_assets"]["meteorology_validation_records"]),
                2,
            )
            self.assertTrue((run_root / "observations.csv").is_file())
            self.assertFalse(list(run_root.glob("*/tdump")))
            self.assertTrue(all(path.is_file() for path in met_files))
            self.assertEqual(progress.done, progress.total)


if __name__ == "__main__":
    unittest.main()
