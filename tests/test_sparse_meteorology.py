"""Sparse-time HYSPLIT meteorology: offline planning and mocked acquisition.

No external network/CDS credentials needed. These tests prevent a regression
to downloading every day between distant launches.
"""
from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.time_windows import (
    TimeWindow, merge_windows, windows_for_starts, find_window, split_window,
)
from hysplit_tool.meteo import plan_archive_windows
from hysplit_tool.progress import TaskProgress
from hysplit_tool.batch_meteorology import (
    prepare_meteorology, preparation_count,
)
from hysplit_tool.core import StartPoint, group_starts, write_group, JST

UTC = timezone.utc


def moment(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


class TestSparseWindows(unittest.TestCase):
    def test_no_empty_days_between_far_apart_starts(self):
        windows = windows_for_starts([moment(3), moment(21)], 3)
        self.assertEqual(windows, (
            TimeWindow(moment(3,9), moment(3)),
            TimeWindow(moment(21,9), moment(21)),
        ))
        self.assertEqual(find_window(windows,moment(21),3),windows[1])
        self.assertEqual(sum((w.end-w.start).total_seconds() for w in windows),6*3600)
        self.assertNotIn(moment(14), [
            w.start for w in windows
        ])

    def test_overlapping_times_merge_and_duplicate_starts_deduplicate(self):
        a=moment(3)
        b=moment(4)
        windows=windows_for_starts([a,a,b], 48)
        self.assertEqual(windows,(TimeWindow(a-timedelta(hours=48),b),))
        with self.assertRaisesRegex(ValueError,"Gap tolerance"):
            merge_windows(windows,max_gap=timedelta(hours=-1))

    def test_noaa_archive_subset_and_archive_dedup(self):
        windows=windows_for_starts(
            [moment(3),moment(3,13),moment(21)],3
        )
        gdas=plan_archive_windows("gdas1",windows)
        self.assertEqual([s.name for s in gdas],[
            "gdas1.oct26.w1", "gdas1.oct26.w3"
        ])
        gfs=plan_archive_windows("gfs0p25",windows)
        self.assertEqual([s.name for s in gfs],[
            "20261003_gfs0p25","20261021_gfs0p25"
        ])
        self.assertEqual(preparation_count("gdas1",windows,action="run"),2)

    def test_native_fortran_control_remains_utc_but_group_id_is_jst(self):
        start=datetime(2025,12,31,23,45,tzinfo=UTC)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            group=group_starts([StartPoint(start,35,135,2000)])[0]
            folder=write_group(
                group,root,[root/"local.arl"],3,20000,"AGL",
            )
            self.assertEqual(folder.name,"20260101T0845JST_00000")
            self.assertTrue((folder/"CONTROL").read_text(encoding="ascii")
                            .startswith("25 12 31 23 45\n"))
            self.assertNotIn("JST",(folder/"CONTROL").read_text(encoding="ascii"))

    def test_arco_split_supports_long_single_trajectory_period(self):
        window=TimeWindow(moment(1),moment(31))
        parts=split_window(window)
        self.assertGreater(len(parts),2)
        self.assertEqual(parts[0].start,window.start)
        self.assertEqual(parts[-1].end,window.end)
        self.assertTrue(all((p.end-p.start)<=timedelta(days=13) for p in parts))
        self.assertTrue(all(a.end > b.start for a,b in zip(parts,parts[1:])))


class TestSparseAcquisition(unittest.TestCase):
    def _progress(self, total):
        items=[]
        return TaskProgress(total,items.append),items

    def test_noaa_only_two_remote_files_and_correct_per_window_assignments(self):
        windows=windows_for_starts([moment(3),moment(21)],3)
        tracker, events=self._progress(2)
        called=[]
        def fake_fetch(spec,target_dir,notify,progress):
            called.append(spec.name)
            progress(64,128)
            return Path(target_dir)/spec.name
        with tempfile.TemporaryDirectory() as td, patch(
            "hysplit_tool.batch_meteorology.download_arl_file",
            side_effect=fake_fetch
        ):
            result=prepare_meteorology(
                "gdas1",windows,Path(td),(55,100,5,160),
                action="run",local_met="",hysplit_executable=Path(td)/"mock.exe",
                notify=lambda _: None,progress=tracker,
            )
        self.assertEqual(called,["gdas1.oct26.w1","gdas1.oct26.w3"])
        self.assertEqual(len(result.files),2)
        self.assertNotEqual(result.for_window(windows[0]),result.for_window(windows[1]))
        self.assertEqual(tracker.done,2)
        self.assertTrue(any(e["part"]==0.5 for e in events))

    def test_noaa_reuses_one_week_for_multiple_different_days(self):
        windows=windows_for_starts([moment(3),moment(4)],2)
        tracker,_=self._progress(1)
        called=[]
        def fake_fetch(spec,target_dir,notify,progress):
            called.append(spec.name)
            return Path(target_dir)/spec.name
        with tempfile.TemporaryDirectory() as td, patch(
            "hysplit_tool.batch_meteorology.download_arl_file",
            side_effect=fake_fetch
        ):
            result=prepare_meteorology(
                "gdas1",windows,Path(td),(55,100,5,160),
                action="run",local_met="",hysplit_executable=Path(td)/"mock.exe",
                notify=lambda _:None,progress=tracker,
            )
        self.assertEqual(called,["gdas1.oct26.w1"])
        self.assertEqual(len(result.files),1)
        self.assertEqual(tracker.done,1)

    def test_cds_multiple_windows_are_separate_for_download_and_conversion(self):
        windows=windows_for_starts([moment(3),moment(21)],3)
        tracker,_=self._progress(4)
        acquired=[];converted=[]
        def fake_download(earliest,latest,target_dir,bbox,notify):
            acquired.append((earliest,latest))
            return [Path(target_dir)/f"era5_pressure_{earliest:%Y%m%d}.grib",
                    Path(target_dir)/f"era5_single_{earliest:%Y%m%d}.grib"]
        def fake_convert(files,target_dir,earliest,latest,*,hysplit_executable,notify):
            converted.append((earliest,latest))
            return [Path(target_dir)/f"era5_cds_{earliest:%Y%m%d}.arl"]
        with tempfile.TemporaryDirectory() as td, \
             patch("hysplit_tool.batch_meteorology.download_cds",
                   side_effect=fake_download), \
             patch("hysplit_tool.batch_meteorology.convert_cds_grib1_to_arl",
                   side_effect=fake_convert):
            result=prepare_meteorology(
                "era5_cds",windows,Path(td),(55,100,5,160),
                action="run",local_met="",hysplit_executable=Path(td)/"mock.exe",
                notify=lambda _:None,progress=tracker,
            )
        self.assertEqual(acquired,[w.as_pair() for w in windows])
        self.assertEqual(converted,[w.as_pair() for w in windows])
        self.assertEqual(tracker.done,4)
        self.assertEqual(len(result.files),2)
        self.assertTrue(all(x.start.day in (3,21) for x in windows))
        self.assertFalse(any(x.start.day==14 for x in windows))

    def test_arco_multi_window_download_and_transform_stays_separate(self):
        windows=windows_for_starts([moment(3),moment(21)],3)
        tracker,_=self._progress(4)
        acquired=[]
        def fake_download(start,end,target_dir,bbox,notify,progress=None):
            acquired.append((start,end))
            return Path(target_dir)/f"era5_{start:%Y%m%d}.nc"
        with tempfile.TemporaryDirectory() as td, \
             patch("hysplit_tool.batch_meteorology.download_google_arco",
                   side_effect=fake_download), \
             patch("hysplit_tool.batch_meteorology.convert_arco_netcdf_to_arl",
                   side_effect=lambda nc,target_dir,start,end,**kw:[
                       Path(target_dir)/(nc.stem+".arl")
                   ]):
            result=prepare_meteorology(
                "arco_era5",windows,Path(td),(55,100,5,160),
                action="run",local_met="",hysplit_executable=Path(td)/"mock.exe",
                notify=lambda _:None,progress=tracker,
            )
        self.assertEqual(acquired,[w.as_pair() for w in windows])
        self.assertEqual(tracker.done,4)
        self.assertEqual(len(result.files),2)

    def test_cds_request_days_do_not_include_empty_gap(self):
        from hysplit_tool.era5 import download_cds
        windows=windows_for_starts([moment(3),moment(21)],3)
        requests=[]
        class FakeClient:
            def retrieve(self,dataset,request,path):
                requests.append((dataset,request))
                Path(path).write_bytes(b"GRIB\x00\x00\x00\x01"+b"x"*20)
        fake=types.SimpleNamespace(Client=lambda:FakeClient())
        with tempfile.TemporaryDirectory() as td, \
             patch.dict(sys.modules, {"cdsapi":fake}):
            for window in windows:
                download_cds(window.start,window.end,Path(td),
                             (55,100,5,160))
        self.assertEqual(len(requests),4)
        days={d for _,request in requests for d in request["day"]}
        self.assertEqual(days,{"03","21"})
        self.assertFalse(any("04" in request["day"] for _,request in requests))


class TestProgress(unittest.TestCase):
    def test_no_fake_percent_for_unmeasured_download(self):
        events=[]
        p=TaskProgress(2,events.append)
        p.start("CDS")
        self.assertTrue(events[-1]["active"])
        self.assertIsNone(events[-1]["part"])
        p.complete("CDS done")
        p.start("ARCO",part=0.5)
        self.assertEqual(events[-1]["part"],0.5)
        p.complete("ARCO done")
        self.assertEqual(events[-1]["done"],2)
        self.assertEqual(events[-1]["total"],2)
        with self.assertRaisesRegex(ValueError,"more operations"):
            p.complete("extra")


if __name__=="__main__":
    unittest.main()

