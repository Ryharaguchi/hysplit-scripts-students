"""Regression for robust Google ARCO hour-by-hour NetCDF export.

A small in-memory ARCO-shaped dataset stands in for the *real* 154 MB
per-variable-hour worldwide chunks; no external service or cost is involved.
"""
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.era5 import download_google_arco, ARCO_PRESSURE_LEVELS
from hysplit_tool.arco_stream import export_hourly_netcdf


@unittest.skipUnless(
    os.name == "nt" and all(
        importlib.util.find_spec(name) is not None
        for name in ("numpy", "xarray", "netCDF4")
    ),
    "Optional Windows ERA5 xarray/netCDF4/numpy libraries unavailable",
)
class TestArcoHourlyExport(unittest.TestCase):
    @staticmethod
    def synthetic():
        import numpy as np
        import xarray as xr
        from hysplit_tool.era5 import ARCO_PRESSURE_FIELDS, ARCO_SURFACE_FIELDS

        hours=4
        coords={
            "time": np.array([
                "2019-01-05T00:00", "2019-01-05T01:00",
                "2019-01-05T02:00", "2019-01-05T03:00"
            ], dtype="datetime64[m]"),
            "level": ARCO_PRESSURE_LEVELS,
            "latitude": [35.25, 35.0],
            "longitude": [134.75, 135.0, 135.25],
        }
        fields={}
        for j,name in enumerate(ARCO_PRESSURE_FIELDS + ["specific_humidity"]):
            data=(np.arange(hours*12*2*3,dtype="float32").reshape(hours,12,2,3)
                  + 100*j)
            if name=="specific_humidity":
                data = data*0 + 0.0005
            fields[name]=(
                ("time","level","latitude","longitude"), data,
            )
        for j,name in enumerate(ARCO_SURFACE_FIELDS):
            if name=="geopotential_at_surface":
                fields[name]=(
                    ("latitude","longitude"),
                    np.full((2,3), 100.0+j, dtype="float32")
                )
            else:
                fields[name]=(
                    ("time","latitude","longitude"),
                    np.full((hours,2,3), 200.0+j, dtype="float32")
                )
        return xr.Dataset(fields,coords=coords)

    def test_interrupted_export_resumes_only_remaining_hours(self):
        import numpy as np
        import xarray as xr

        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            output=root/"arco_2019.nc"
            ds=self.synthetic()
            events=[]
            def interrupted(done,total):
                events.append((done,total))
                if done == 2:
                    raise RuntimeError("simulated Windows shutdown after checkpoint")
            with self.assertRaisesRegex(RuntimeError,"simulated Windows shutdown"):
                export_hourly_netcdf(ds,output,source="fake://test",
                                     progress=interrupted)
            self.assertFalse(output.exists())
            partial=output.with_suffix(".part")
            state=Path(str(partial)+".json")
            self.assertTrue(partial.exists())
            self.assertTrue(state.exists())
            progress=[]
            logs=[]
            path=export_hourly_netcdf(
                self.synthetic(), output, source="fake://test",
                notify=logs.append,progress=lambda done,total:progress.append((done,total))
            )
            self.assertEqual(path,output)
            self.assertTrue(output.is_file())
            self.assertFalse(partial.exists())
            self.assertFalse(state.exists())
            self.assertEqual(progress[0],(2,4))
            self.assertEqual(progress[-1],(4,4))
            self.assertTrue(any("resuming" in msg for msg in logs),logs)
            with xr.open_dataset(output,engine="netcdf4") as result:
                self.assertEqual(result.sizes["time"],4)
                self.assertEqual(result.sizes["latitude"],2)
                self.assertEqual(result.sizes["level"],12)
                for name in ds.data_vars:
                    np.testing.assert_allclose(
                        result[name].values, ds[name].values,
                        rtol=1e-6,atol=1e-7
                    )
                np.testing.assert_array_equal(result.time.values,ds.time.values)

    def test_export_is_compatible_with_native_grib_converter_validation(self):
        from hysplit_tool.arco_convert import _utc_times, _axes
        import numpy as np
        import xarray as xr
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            output=export_hourly_netcdf(
                self.synthetic(),root/"smoke.nc",source="fake://test",
            )
            with xr.open_dataset(output,engine="netcdf4") as ds:
                times=_utc_times(ds,np)
                lat_order,lon_order,lat,lon=_axes(ds,np)
                self.assertEqual(len(times),4)
                self.assertEqual(times[0],datetime(2019,1,5,tzinfo=timezone.utc))
                self.assertEqual(len(lat),2)
                self.assertEqual(len(lon),3)
                self.assertIn("specific_humidity",ds)
                self.assertIn("geopotential_at_surface",ds)

    def test_google_download_progress_and_reuse_completed_cache(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            ds=self.synthetic()
            progress=[]
            with patch("xarray.open_zarr",return_value=ds) as remote:
                path=download_google_arco(
                    datetime(2019,1,5,0,tzinfo=timezone.utc),
                    datetime(2019,1,5,2,tzinfo=timezone.utc),
                    root,(35.25,134.75,35.0,135.25),
                    notify=lambda _:None,
                    progress=lambda done,total:progress.append((done,total))
                )
            self.assertTrue(path.is_file())
            self.assertEqual(progress[-1],(4,4))
            remote.assert_called_once()
            with patch("xarray.open_zarr",side_effect=AssertionError("Network must not open")):
                again=download_google_arco(
                    datetime(2019,1,5,0,tzinfo=timezone.utc),
                    datetime(2019,1,5,2,tzinfo=timezone.utc),
                    root,(35.25,134.75,35.0,135.25),
                )
            self.assertEqual(again,path)


if __name__=="__main__":
    unittest.main()

