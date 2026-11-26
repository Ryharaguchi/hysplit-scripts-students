"""Full ERA5 horizontal grid, all 37 pressure levels and explicit model top.

Offline regression: never contact ECMWF or Google services, overwrite cached
regional files, or mistake legacy 12-pressure-level GRIBs for new data.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.era5 import (
    GLOBAL_ERA5_BBOX, NORTHERN_HEMISPHERE_BBOX, PRESSURE_LEVELS, ARCO_PRESSURE_LEVELS,
    HYSPLIT_PRESSURE_FIELDS, PRESSURE_FIELDS, SURFACE_FIELDS,
    download_cds, download_google_arco
)

UTC=timezone.utc

class TestGlobalEra5(unittest.TestCase):
    def test_all_official_37_pressure_levels_in_acquisition_and_converter(self):
        from hysplit_tool.arco_convert import PRESSURE_LEVELS as CONVERTED
        from hysplit_tool.native_era5 import run_native_grib1_pair, convert_cds_grib1_to_arl
        self.assertEqual(len(PRESSURE_LEVELS),37)
        self.assertEqual(tuple(map(int,PRESSURE_LEVELS)), tuple(CONVERTED))
        self.assertEqual(CONVERTED[0],1000)
        self.assertEqual(CONVERTED[-1],1)
        self.assertEqual(len(ARCO_PRESSURE_LEVELS),37)
        self.assertEqual(run_native_grib1_pair.__kwdefaults__["level_count"],38)
        self.assertEqual(convert_cds_grib1_to_arl.__kwdefaults__["level_count"],38)

    def test_cds_global_request_preserves_37_and_does_not_reuse_12_level_file(self):
        requests=[]
        class CDSClient:
            def __init__(self, **kwargs):
                pass
            def retrieve(self,dataset,request,target):
                requests.append((dataset,request))
                Path(target).write_bytes(b"GRIB"+b"\x00"*100)
        fake=types.SimpleNamespace(Client=CDSClient)
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules,{"cdsapi":fake}):
            d=Path(td)
            # Prior saved file from the 12-level release. Never delete or reuse it.
            old=d/"era5_pressure_201901_05-05_global.grib"
            old.write_bytes(b"GRIB"+"old 12 layers".encode())
            start=datetime(2019,1,5,0,tzinfo=UTC)
            end=datetime(2019,1,5,1,tzinfo=UTC)
            out=download_cds(start,end,d,GLOBAL_ERA5_BBOX)
            self.assertEqual(len(out),2)
            self.assertEqual(len(requests),2)
            self.assertEqual(old.read_bytes(),b"GRIBold 12 layers")
            self.assertNotIn(old,out)
            self.assertTrue(all("area" not in req for _,req in requests))
            pressure=next(req for name,req in requests if "pressure" in name)
            self.assertEqual(pressure["pressure_level"],PRESSURE_LEVELS)
            self.assertEqual(pressure["grid"],[0.25,0.25])
            self.assertEqual(len(pressure["pressure_level"]),37)
            with patch.dict(sys.modules,{"cdsapi":types.SimpleNamespace(Client=lambda **kwargs: (_ for _ in ()).throw(AssertionError("Should use matching 37-level cache")))}):
                again=download_cds(start,end,d,GLOBAL_ERA5_BBOX)
            self.assertEqual(again,out)

    def test_cds_hysplit_profile_requests_only_required_fields_and_has_distinct_cache(self):
        requests=[]
        class CDSClient:
            def __init__(self, **kwargs):
                pass
            def retrieve(self,dataset,request,target):
                requests.append((dataset,request))
                Path(target).write_bytes(b"GRIB"+b"\x00"*100)
        fake=types.SimpleNamespace(Client=CDSClient)
        start=datetime(2019,1,5,0,tzinfo=UTC)
        end=datetime(2019,1,5,1,tzinfo=UTC)
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules,{"cdsapi":fake}):
            root=Path(td)
            selected=download_cds(
                start,end,root,NORTHERN_HEMISPHERE_BBOX,field_profile="hysplit"
            )
            self.assertEqual(len(selected),2)
            self.assertTrue(all("_hysplit.grib" in path.name for path in selected))
            pressure=next(request for name,request in requests if "pressure-levels" in name)
            surface=next(request for name,request in requests if "single-levels" in name)
            self.assertEqual(pressure["variable"],HYSPLIT_PRESSURE_FIELDS)
            self.assertEqual(pressure["pressure_level"],PRESSURE_LEVELS)
            self.assertEqual(surface["variable"],SURFACE_FIELDS)

            allvars=download_cds(start,end,root,NORTHERN_HEMISPHERE_BBOX)
            self.assertEqual(len(allvars),2)
            self.assertTrue(all("_hysplit.grib" not in path.name for path in allvars))
            self.assertEqual(len(requests),4)
            all_pressure=next(request for name,request in requests[2:] if "pressure-levels" in name)
            self.assertEqual(all_pressure["variable"],PRESSURE_FIELDS)

    def test_gui_uses_northern_hemisphere_and_exposes_model_top(self):
        from hysplit_tool.gui import App
        from hysplit_tool.core import automatic_model_top,StartPoint
        t=datetime(2026,1,1,tzinfo=UTC)
        self.assertEqual(automatic_model_top([StartPoint(t,35,135,1000)]),25000.0)
        with tempfile.TemporaryDirectory() as td:
            csv=Path(td)/"starts.csv"
            csv.write_text("2026/01/02 09:00,35,135,1000\n",encoding="utf-8")
            app=App()
            app.withdraw()
            try:
                app.v["csv"].set(str(csv))
                app.v["provider"].set("ERA5 Google ARCO (0.25°)")
                conf,*rest=app._data()
                self.assertEqual(conf["era5_field_profile"],"hysplit")
                self.assertEqual(conf["workers"],"自動")
                self.assertGreaterEqual(int(conf["effective_workers"]),1)
                self.assertLessEqual(int(conf["effective_workers"]),8)
                self.assertEqual(tuple(rest[-1]),NORTHERN_HEMISPHERE_BBOX)
                self.assertEqual(conf["internal_model_top_m_agl"],"50000.0")
                self.assertEqual(conf["model_top_m"],"")
                app.v["provider"].set("ERA5 CDS (0.25°)")
                conf,*rest=app._data()
                self.assertEqual(conf["era5_field_profile"],"hysplit")
                app.v["model_top_m"].set("48000")
                conf,*rest=app._data()
                self.assertEqual(conf["internal_model_top_m_agl"],"48000.0")
                for invalid in ("0","-100","NaN","999"):
                    app.v["model_top_m"].set(invalid)
                    with self.assertRaises(ValueError):
                        app._data()
            finally:
                app.destroy()

    @unittest.skipUnless(
        all(importlib.util.find_spec(name) is not None for name in ("xarray","numpy")),
        "Optional ERA5 xarray/numpy unavailable"
    )
    def test_google_arco_global_mode_never_crops_latitude_longitude(self):
        # A small test fixture represents the WHOLE requested test-grid.
        # Real production data are 721x1440; mock exporter avoids downloading.
        import numpy as np
        import xarray as xr
        from hysplit_tool.era5 import ARCO_PRESSURE_FIELDS,ARCO_SURFACE_FIELDS
        coords={
            "time":np.array(["2019-01-05T00:00","2019-01-05T01:00"],dtype="datetime64[m]"),
            "level":list(ARCO_PRESSURE_LEVELS),
            "latitude":[89.75,89.5,89.25],
            "longitude":[0,0.25,0.5,0.75],
        }
        vars={}
        for name in ARCO_PRESSURE_FIELDS+["specific_humidity"]:
            vars[name]=(("time","level","latitude","longitude"),
                        np.ones((2,37,3,4),dtype="float32"))
        for name in ARCO_SURFACE_FIELDS:
            if name=="geopotential_at_surface":
                vars[name]=(("latitude","longitude"),np.ones((3,4),dtype="float32"))
            else:
                vars[name]=(("time","latitude","longitude"),
                            np.ones((2,3,4),dtype="float32"))
        # Additional scientific fields must survive the archive step even
        # though HYSPLIT's ARL converter does not use them.
        vars["fraction_of_cloud_cover"] = (
            ("time", "level", "latitude", "longitude"),
            np.full((2,37,3,4),0.25,dtype="float32"),
        )
        vars["total_cloud_cover"] = (
            ("time", "latitude", "longitude"),
            np.full((2,3,4),0.5,dtype="float32"),
        )
        ds=xr.Dataset(vars,coords=coords)
        observed=[]
        def fake_export(selected,path,*,source,notify,progress,migrate_from=None):
            observed.append(selected)
            self.assertEqual(selected.sizes["latitude"],3)
            self.assertEqual(selected.sizes["longitude"],4)
            self.assertEqual(selected.sizes["level"],37)
            self.assertIn("specific_humidity",selected.data_vars)
            self.assertIn("fraction_of_cloud_cover",selected.data_vars)
            self.assertIn("total_cloud_cover",selected.data_vars)
            return path
        with tempfile.TemporaryDirectory() as td, \
             patch("xarray.open_zarr",return_value=ds), \
             patch("hysplit_tool.arco_stream.export_hourly_netcdf",side_effect=fake_export):
            result=download_google_arco(
                datetime(2019,1,5,0,tzinfo=UTC),
                datetime(2019,1,5,1,tzinfo=UTC),
                Path(td),GLOBAL_ERA5_BBOX
            )
            self.assertTrue(result.name.endswith("_global37_allvars.nc"))
        self.assertEqual(len(observed),1)


if __name__=="__main__":
    unittest.main()

