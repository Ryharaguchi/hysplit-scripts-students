"""Regression for Google ARCO having q instead of relative_humidity.

Windows-native optional ecCodes/xarray test; no network, HYSPLIT executable or
remote Google download is required for these deterministic unit tests.
"""
from __future__ import annotations

import importlib.util
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.era5 import download_google_arco
from hysplit_tool.arco_convert import (
    PRESSURE_LEVELS, PRESSURE_PARAMS, SURFACE_PARAMS,
    relative_humidity_from_specific_humidity,
)

HAS_LIBS = os.name == "nt" and all(
    importlib.util.find_spec(name) is not None
    for name in ("xarray", "numpy", "eccodes", "netCDF4")
)


@unittest.skipUnless(HAS_LIBS, "Optional Windows ERA5 libraries unavailable")
class TestArcoSpecificHumidity(unittest.TestCase):
    @staticmethod
    def _sample(tmp: Path, q_only: bool = True, q_value: float = 0.001):
        import numpy as np
        import xarray as xr

        coords = {
            "time": np.array(
                ["2019-01-05T00:00", "2019-01-05T01:00"], dtype="datetime64[m]"
            ),
            "level": list(PRESSURE_LEVELS),
            "latitude": [35.25, 35.0],
            "longitude": [134.75, 135.0, 135.25],
        }
        variables = {}
        for name, _param, _unit in PRESSURE_PARAMS:
            if name == "relative_humidity" and q_only:
                continue
            base = 285.0 if name == "temperature" else \
                100.0 if name == "relative_humidity" else 1.0
            variables[name] = (
                ("time", "level", "latitude", "longitude"),
                np.full((2, len(PRESSURE_LEVELS), 2, 3), base, dtype=np.float32),
            )
        if q_only:
            variables["specific_humidity"] = (
                ("time", "level", "latitude", "longitude"),
                np.full((2, len(PRESSURE_LEVELS), 2, 3), q_value, dtype=np.float32),
            )
        for name, param, _unit in SURFACE_PARAMS:
            value = 101325.0 if name == "surface_pressure" else 275.0
            if name == "geopotential_at_surface":
                variables[name] = (
                    ("latitude", "longitude"),
                    np.full((2,3), 200.0, dtype=np.float32),
                )
            else:
                variables[name] = (
                    ("time", "latitude", "longitude"),
                    np.full((2,2,3), value, dtype=np.float32),
                )
        ds = xr.Dataset(variables, coords=coords)
        return ds

    def test_q_to_rh_at_known_mixed_saturation(self):
        import numpy as np
        t = np.array([[273.16, 250.16, 263.0]], dtype=np.float64)
        q = np.array([[0.0038, 0.0012, 0.0020]], dtype=np.float64)
        result = relative_humidity_from_specific_humidity(t, q, 1000.0, np)
        eps = 0.621981
        vapour = 100000 * q / (eps + (1-eps)*q)
        water = 611.21 * np.exp(17.502 * (t - 273.16) / (t - 32.19))
        ice = 611.21 * np.exp(22.587 * (t - 273.16) / (t + 0.7))
        alpha = np.clip((t-250.16)/23, 0, 1)**2
        expected = 100 * vapour / (alpha*water+(1-alpha)*ice)
        np.testing.assert_allclose(result, expected, rtol=1e-12)
        self.assertAlmostEqual(float(result[0,0]), 99.5, delta=1.0)
        self.assertGreater(float(result[0,1]), 100.0)

    def test_reject_wrong_q_or_temperature(self):
        import numpy as np
        for t, q in [
            (np.array([280.0]), np.array([-0.001])),
            (np.array([math.nan]), np.array([0.001])),
            (np.array([0.0]), np.array([0.001])),
            (np.array([280.0]), np.array([1.0])),
        ]:
            with self.subTest(temp=t, q=q), self.assertRaises(ValueError):
                relative_humidity_from_specific_humidity(t, q, 925, np)

    def test_remote_selection_accepts_specific_humidity_without_rh(self):
        import xarray as xr
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            ds=self._sample(root, q_only=True)
            self.assertIn("specific_humidity", ds)
            self.assertNotIn("relative_humidity", ds)
            notices=[]
            with patch("xarray.open_zarr", return_value=ds) as mocked:
                output=download_google_arco(
                    datetime(2019,1,5,0,tzinfo=timezone.utc),
                    datetime(2019,1,5,1,tzinfo=timezone.utc),
                    root / "downloads", (35.25,134.75,35.0,135.25),
                    notify=notices.append,
                )
            self.assertTrue(output.exists())
            mocked.assert_called_once()
            with xr.open_dataset(output, engine="netcdf4") as result:
                self.assertIn("specific_humidity", result.data_vars)
                self.assertNotIn("relative_humidity", result.data_vars)
            self.assertTrue(any("relative_humidity will be derived" in n for n in notices))

    def test_netcdf_q_generates_grib1_param157_rh(self):
        import eccodes as e
        import numpy as np
        from hysplit_tool.arco_convert import netcdf_to_grib1
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            input_file, upper, lower=(
                base/"era5_q_only.nc",base/"pressure.grib",base/"surface.grib")
            ds=self._sample(base, q_only=True)
            ds.to_netcdf(input_file, engine="netcdf4")
            ds.close()
            notices=[]
            first,last=netcdf_to_grib1(input_file,upper,lower,notify=notices.append)
            self.assertEqual(first,datetime(2019,1,5,0,tzinfo=timezone.utc))
            self.assertEqual(last,datetime(2019,1,5,1,tzinfo=timezone.utc))
            self.assertTrue(any("Deriving RH" in x for x in notices))
            grib=upper.read_bytes()
            at=0
            rh_values=[]
            nmessages=0
            while at<len(grib):
                self.assertEqual(grib[at:at+4],b"GRIB")
                n=int.from_bytes(grib[at+4:at+7],"big")
                h=e.codes_new_from_message(grib[at:at+n])
                try:
                    nmessages+=1
                    if e.codes_get(h,"paramId")==157:
                        vals=np.asarray(e.codes_get_values(h))
                        self.assertTrue(np.all(vals >= 0))
                        self.assertTrue(np.all(np.isfinite(vals)))
                        rh_values.extend(vals.tolist())
                finally:e.codes_release(h)
                at+=n
            self.assertEqual(at,len(grib))
            self.assertEqual(nmessages,2*len(PRESSURE_LEVELS)*len(PRESSURE_PARAMS))
            self.assertEqual(len(rh_values),2*len(PRESSURE_LEVELS)*2*3)
            self.assertTrue(any(0 < v < 100 for v in rh_values))

    def test_netcdf_no_humidity_still_fails_safely(self):
        import xarray as xr
        from hysplit_tool.arco_convert import netcdf_to_grib1
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            ds=self._sample(root,q_only=True).drop_vars("specific_humidity")
            path=root/"no_humidity.nc"
            ds.to_netcdf(path,engine="netcdf4")
            ds.close()
            output=root/"p.grib"
            with self.assertRaisesRegex(ValueError,"humidity"):
                netcdf_to_grib1(path,output,root/"s.grib")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

