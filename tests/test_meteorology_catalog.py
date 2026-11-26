"""Offline regression tests for the active content-indexed meteorology pipeline."""
from __future__ import annotations
from datetime import datetime,timezone,timedelta
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from hysplit_tool.arl_catalog import (inspect_arl,select_arl,native_files,
    required_hours,consecutive_hours,files_for_window)
from hysplit_tool.era5 import PRESSURE_LEVELS
from hysplit_tool.era5_materialize import Era5Materializer
from hysplit_tool.time_windows import TimeWindow
from hysplit_tool.batch_meteorology import prepare_meteorology,preparation_count
from hysplit_tool.progress import TaskProgress

UTC=timezone.utc
T=datetime(2026,7,16,0,tzinfo=UTC)
H=timedelta(hours=1)
BBOX=(35.25,134.75,25.5,144.5)
SURF=("SHGT","TPP6","U10M","V10M","T02M","PBLH")
UPPER=("HGTS","TEMP","UWND","VWND","WWND","RELH")

def arl_fixture(path,times,fill=127):
    """A small structural fixture, not physical meteorology."""
    nx=ny=40
    grid=(35.25,144.5,.25,.25,0,0,0,1,1,25.5,134.75,0)
    levels=(0,*map(int,PRESSURE_LEVELS))
    variables=(SURF,*([UPPER]*37))
    table="".join(f"{v:6.1f}{len(names):2d}"+"".join(f"{n:4s}{0:3d} " for n in names)
                  for v,names in zip(levels,variables))
    index="ECMG  0 0"+"".join(f"{v:7.3f}" for v in grid)+f"{nx:3d}{ny:3d}{38:3d}{2:2d}{108+len(table):4d}"+table
    assert len(index)==108+len(table)
    def label(t,lev,name):
        return (f"{t.year%100:2d}{t.month:2d}{t.day:2d}{t.hour:2d}{0:2d}{lev:2d}99{name:4s}"
                f"{0:4d}{0.:14.7E}{0.:14.7E}").encode("ascii")
    path=Path(path)
    with path.open("wb") as f:
        for t in times:
            for a in range(0,len(index),nx*ny):
                f.write(label(t,0,"INDX"))
                f.write(index[a:a+nx*ny].encode().ljust(nx*ny,b"\0"))
            for lev,names in enumerate(variables):
                for name in names:
                    f.write(label(t,lev,name))
                    f.write(bytes([fill])*nx*ny)
    return path

class TestCatalog(unittest.TestCase):
    def test_floor_ceil_and_day_boundary_without_empty_days(self):
        window=TimeWindow(T-H+timedelta(minutes=30),T+timedelta(minutes=5))
        self.assertEqual(required_hours(window),(T-H,T,T+H))
        days=consecutive_hours(required_hours(window),by_day=True)
        self.assertEqual(days,(TimeWindow(T-H,T-H),TimeWindow(T,T+H)))

    def test_old_names_and_multiple_files_with_partial_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            a=arl_fixture(d/"arbitrary_old_name.arl",[T-H,T])
            b=arl_fixture(d/"other_name.arl",[T+H,T+2*H])
            w=TimeWindow(T-H,T+3*H)
            files,missing=select_arl(d,w,BBOX)
            self.assertEqual(files,(a.resolve(),b.resolve()))
            self.assertEqual(missing,(T+3*H,))
            self.assertEqual(len(inspect_arl(a).levels),38)

    def test_duplicate_identical_preserved_and_conflict_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            a=arl_fixture(d/"a.arl",[T,T+H])
            b=arl_fixture(d/"b.arl",[T,T+H])
            w=TimeWindow(T,T+H)
            self.assertEqual(select_arl(d,w,BBOX)[1],())
            self.assertTrue(a.exists() and b.exists())
            arl_fixture(b,[T,T+H],fill=126)
            with self.assertRaisesRegex(RuntimeError,"Different ARL values"):
                select_arl(d,w,BBOX)
            self.assertTrue(a.exists() and b.exists())

    def test_single_hour_view_copies_only_required_neighbor(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            a=arl_fixture(d/"a.arl",[T])
            b=arl_fixture(d/"b.arl",[T+H,T+2*H,T+3*H])
            raw=[a.read_bytes(),b.read_bytes()]
            w=TimeWindow(T,T+3*H)
            result=native_files((a,b),w,d/"views")
            self.assertEqual(len(result),2)
            view=next(p for p in result if p!=b.resolve())
            self.assertEqual(inspect_arl(view).times,(T,T+H))
            self.assertEqual(view.read_bytes(),raw[0]+raw[1][:inspect_arl(b).block_bytes])
            self.assertEqual([a.read_bytes(),b.read_bytes()],raw)

    def test_ready_arls_skip_remote_and_local_conversion_for_both_providers(self):
        for provider in ("arco_era5","era5_cds"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                d=Path(tmp);(d/"arl").mkdir()
                a=arl_fixture(d/"arl"/"one.arl",[T,T+H])
                b=arl_fixture(d/"arl"/"two.arl",[T+2*H,T+3*H])
                w=TimeWindow(T,T+3*H)
                m=Era5Materializer(provider,d,BBOX,d/"hyts_std.exe","hysplit",False,lambda _:None)
                with patch.object(m,"_remote",side_effect=AssertionError("network forbidden")), \
                     patch("hysplit_tool.era5_materialize.index_grib",side_effect=AssertionError("conversion forbidden")):
                    result=m.prepare(w,action="run")
                self.assertEqual(result,(a.resolve(),b.resolve()))

    def test_periods_published_in_sequence_with_shared_materializer(self):
        w=(TimeWindow(T,T+H),TimeWindow(T+30*H,T+31*H))
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp); events=[]
            def prepare(_self,window,*,action,progress):
                events.append(("prepare",window))
                return (d/f"{window.start:%Y%m%d}.arl",)
            with patch.object(Era5Materializer,"prepare",prepare):
                progress=TaskProgress(preparation_count("arco_era5",w,action="run"),lambda _:None)
                prepare_meteorology("arco_era5",w,d,BBOX,action="run",local_met="",
                    hysplit_executable=d/"hyts_std.exe",notify=lambda _:None,
                    progress=progress,on_window_ready=lambda w,_:events.append(("ready",w)))
            self.assertEqual(events,[("prepare",w[0]),("ready",w[0]),("prepare",w[1]),("ready",w[1])])
            self.assertEqual(progress.done,4)

if __name__=="__main__":unittest.main()
