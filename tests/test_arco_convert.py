"""Windows-only ERA5 ARCO NetCDF -> GRIB1 regression tests."""
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.arco_convert import PRESSURE_LEVELS, PRESSURE_PARAMS, SURFACE_PARAMS
from hysplit_tool.core import StartPoint, automatic_model_top

HAS_LIBS = os.name == "nt" and all(
    importlib.util.find_spec(name) is not None
    for name in ("eccodes", "numpy", "xarray", "netCDF4")
)


class TestModelTop(unittest.TestCase):
    def test_top_hidden_and_derived_from_csv_heights(self):
        dt = datetime(2026, 10, 10, tzinfo=timezone.utc)
        points = [StartPoint(dt, 35, 135, h) for h in (500, 600, 1000)]
        self.assertEqual(automatic_model_top(points), 20000.0)
        high = [StartPoint(dt, 35, 135, 24001)]
        self.assertEqual(automatic_model_top(high), 26000.0)
        with self.assertRaises(ValueError):
            automatic_model_top([])


@unittest.skipUnless(HAS_LIBS, "Windows ecCodes, xarray, numpy and netCDF4 optional")
class TestArcoNetcdfToGrib1(unittest.TestCase):
    def _sample(self, path, *, bad_grid=False, gap=False, missing=False):
        import numpy as np
        import xarray as xr
        longitude = np.array([134.75, 135.0, 135.50 if bad_grid else 135.25])
        time = np.array(
            ["2019-01-05T00:00", "2019-01-05T02:00" if gap else "2019-01-05T01:00"],
            dtype="datetime64[m]")
        levels = list(PRESSURE_LEVELS)
        coords = {
            "time": time,
            "level": levels,
            "latitude": [35.25, 35.0],
            "longitude": longitude,
        }
        variables = {}
        for name, param, _ in PRESSURE_PARAMS:
            if missing and name == "vertical_velocity":
                continue
            value = (55000 if name == "geopotential" else
                     275 if name == "temperature" else
                     54 if name == "relative_humidity" else 1.0)
            variables[name] = (
                ("time", "level", "latitude", "longitude"),
                np.full((2, 12, 2, 3), value, dtype=np.float32),
            )
        for name, param, _ in SURFACE_PARAMS:
            value = (100000 if name == "surface_pressure" else
                     280 if name == "2m_temperature" else
                     0.001 if name == "total_precipitation" else
                     1.0)
            if name == "geopotential_at_surface":
                variables[name] = (
                    ("latitude", "longitude"),
                    np.full((2,3), 500.0, dtype=np.float32),
                )
            else:
                variables[name] = (
                    ("time", "latitude", "longitude"),
                    np.full((2,2,3),value,dtype=np.float32),
                )
        ds = xr.Dataset(variables, coords=coords)
        ds.to_netcdf(path, engine="netcdf4")
        ds.close()

    def test_creates_complete_grib1_message_sequence(self):
        import eccodes as e
        from hysplit_tool.arco_convert import netcdf_to_grib1
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, pressure, surface = (
                root/"sample.nc",root/"pres.grib",root/"sfc.grib")
            self._sample(source)
            first,last=netcdf_to_grib1(source,pressure,surface)
            self.assertEqual(first, datetime(2019,1,5,0,tzinfo=timezone.utc))
            self.assertEqual(last, datetime(2019,1,5,1,tzinfo=timezone.utc))
            for path, expected in ((pressure,2*12*6),(surface,2*7)):
                data=path.read_bytes()
                fields=[]
                cursor=0
                while cursor < len(data):
                    self.assertEqual(data[cursor:cursor+4], b"GRIB")
                    size=int.from_bytes(data[cursor+4:cursor+7], "big")
                    self.assertGreaterEqual(size,12)
                    self.assertEqual(data[cursor+size-4:cursor+size], b"7777")
                    gid=e.codes_new_from_message(data[cursor:cursor+size])
                    try:
                        fields.append((e.codes_get(gid,"edition"),
                                       e.codes_get(gid,"paramId"),
                                       e.codes_get(gid,"validityDate"),
                                       e.codes_get(gid,"validityTime")))
                    finally:
                        e.codes_release(gid)
                    cursor += size
                self.assertEqual(cursor,len(data))
                self.assertEqual(len(fields), expected)
                self.assertTrue(all(f[0]==1 for f in fields))
                self.assertEqual({(row[2],row[3]) for row in fields},
                                 {(20190105,0),(20190105,100)})

    def test_missing_field_rejected_and_no_partial(self):
        from hysplit_tool.arco_convert import netcdf_to_grib1
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source,pressure,surface=root/"s.nc",root/"a.grib",root/"b.grib"
            self._sample(source,missing=True)
            with self.assertRaisesRegex(ValueError,"vertical_velocity"):
                netcdf_to_grib1(source,pressure,surface)
            self.assertFalse(pressure.exists())
            self.assertFalse(surface.exists())

    def test_time_gap_and_wrong_grid_rejected(self):
        from hysplit_tool.arco_convert import netcdf_to_grib1
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source,pressure,surface=root/"s.nc",root/"a.grib",root/"b.grib"
            self._sample(source,gap=True)
            with self.assertRaisesRegex(ValueError,"continuous"):
                netcdf_to_grib1(source,pressure,surface)
            self._sample(source,bad_grid=True)
            with self.assertRaisesRegex(ValueError,"Longitude"):
                netcdf_to_grib1(source,pressure,surface)


if __name__ == "__main__":
    unittest.main()

