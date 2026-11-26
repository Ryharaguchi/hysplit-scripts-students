
from __future__ import annotations
import importlib.util
import sys, tempfile, types, unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.era5 import (
    NORTHERN_HEMISPHERE_BBOX, GLOBAL_ERA5_BBOX,
    PRESSURE_LEVELS, ARCO_PRESSURE_LEVELS, ARCO_PRESSURE_FIELDS,
    ARCO_SURFACE_FIELDS, download_cds,download_google_arco
)
UTC=timezone.utc

class TestNorthernHemisphere(unittest.TestCase):
    def test_requested_geometry(self):
        self.assertEqual(NORTHERN_HEMISPHERE_BBOX,(90.0,-180.0,0.0,180.0))
        self.assertEqual(len(PRESSURE_LEVELS),37)
        self.assertEqual((90.0-0.0)/0.25+1,361)
        self.assertEqual((180.0-(-180.0))/0.25,1440)

    def test_cds_all_levels_all_longitudes_cache_reuse_and_old_preserved(self):
        requests=[]
        class FakeCDS:
            def __init__(self, **kwargs):
                pass
            def retrieve(self,dataset,request,target):
                requests.append((dataset,request))
                Path(target).write_bytes(b"GRIB"+b"\0"*100)
        start=datetime(2019,1,5,tzinfo=UTC)
        end=datetime(2019,1,5,1,tzinfo=UTC)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            old=p/"era5_pressure_201901_05-05_global.grib"
            old.write_bytes(b"legacy untouched")
            with patch.dict(sys.modules,{"cdsapi":types.SimpleNamespace(Client=FakeCDS)}):
                outputs=download_cds(start,end,p,NORTHERN_HEMISPHERE_BBOX)
            self.assertEqual(len(outputs),2)
            self.assertTrue(all("_northern_hemisphere.grib" in q.name for q in outputs))
            self.assertEqual(len(requests),2)
            self.assertTrue(all(req["area"]==[90.0,-180.0,0.0,180.0] for _,req in requests))
            pressure=[r for d,r in requests if "pressure-levels" in d][0]
            self.assertEqual(len(pressure["pressure_level"]),37)
            self.assertEqual(pressure["pressure_level"],PRESSURE_LEVELS)
            self.assertEqual(old.read_bytes(),b"legacy untouched")
            with patch.dict(sys.modules,{"cdsapi":types.SimpleNamespace(Client=lambda **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))}):
                again=download_cds(start,end,p,NORTHERN_HEMISPHERE_BBOX)
            self.assertEqual(outputs,again)

    @unittest.skipUnless(
        all(importlib.util.find_spec(name) is not None for name in ("xarray", "numpy")),
        "Optional ERA5 xarray/numpy unavailable",
    )
    def test_arco_north_only_no_meridian_loss_37_levels_all_fields(self):
        import numpy as np, xarray as xr
        coords={
            "time":np.array(["2019-01-05T00:00","2019-01-05T01:00"],dtype="datetime64[m]"),
            "level":list(ARCO_PRESSURE_LEVELS),
            "latitude":[90.0,45.0,0.0,-0.25,-45.0],
            "longitude":[0.0,0.25,359.5,359.75],
        }
        fields={}
        for name in ARCO_PRESSURE_FIELDS+["specific_humidity"]:
            fields[name]=(("time","level","latitude","longitude"),np.ones((2,37,5,4),dtype="f4"))
        for name in ARCO_SURFACE_FIELDS:
            if name=="geopotential_at_surface":
                fields[name]=(("latitude","longitude"),np.ones((5,4),dtype="f4"))
            else:
                fields[name]=(("time","latitude","longitude"),np.ones((2,5,4),dtype="f4"))
        fields["fraction_of_cloud_cover"]=(("time","level","latitude","longitude"),np.ones((2,37,5,4),dtype="f4"))
        ds=xr.Dataset(fields,coords=coords)
        extracted=[]
        def fake_export(selected,path,*,source,notify,progress,migrate_from=None):
            extracted.append(selected)
            self.assertEqual(list(selected.latitude.values),[90,45,0])
            self.assertEqual(list(selected.longitude.values),[0,.25,359.5,359.75])
            self.assertEqual(set(map(int,selected.level.values)),set(ARCO_PRESSURE_LEVELS))
            self.assertIn("fraction_of_cloud_cover",selected.data_vars)
            self.assertIn("specific_humidity",selected.data_vars)
            return path
        with tempfile.TemporaryDirectory() as td:
            with patch("xarray.open_zarr",return_value=ds),patch("hysplit_tool.arco_stream.export_hourly_netcdf",side_effect=fake_export):
                p=download_google_arco(datetime(2019,1,5,tzinfo=UTC), datetime(2019,1,5,1,tzinfo=UTC),Path(td),NORTHERN_HEMISPHERE_BBOX)
            self.assertTrue(p.name.endswith("_northern37_allvars.nc"))
        self.assertEqual(len(extracted),1)

if __name__=="__main__":
    unittest.main()
