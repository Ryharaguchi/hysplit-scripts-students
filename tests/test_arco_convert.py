"""Windows-only ERA5 ARCO NetCDF -> GRIB1 regression tests."""
import importlib.util
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.arco_convert import (
    PRESSURE_LEVELS, PRESSURE_PARAMS, SURFACE_PARAMS,
    cleanup_verified_hysplit_netcdf_sources, estimate_grib1_pair_size_bytes,
)
from hysplit_tool.core import StartPoint, automatic_model_top
from hysplit_tool.validation import CoverageReport

HAS_LIBS = os.name == "nt" and all(
    importlib.util.find_spec(name) is not None
    for name in ("eccodes", "numpy", "xarray", "netCDF4")
)


class TestModelTop(unittest.TestCase):
    def test_top_hidden_and_derived_from_csv_heights(self):
        dt = datetime(2026, 10, 10, tzinfo=timezone.utc)
        points = [StartPoint(dt, 35, 135, h) for h in (500, 600, 1000)]
        self.assertEqual(automatic_model_top(points), 25000.0)
        high = [StartPoint(dt, 35, 135, 24001)]
        self.assertEqual(automatic_model_top(high), 26000.0)
        with self.assertRaises(ValueError):
            automatic_model_top([])

    def test_grib1_size_estimate_accounts_for_all_pressure_and_surface_fields(self):
        estimate = estimate_grib1_pair_size_bytes(1, 1, 1)
        pressure_bits = 24 + 5 * 16
        surface_bits = 24 + 6 * 16
        payload = (37 * pressure_bits + surface_bits) / 8
        messages = 37 * len(PRESSURE_PARAMS) + len(SURFACE_PARAMS)
        expected = math.ceil(payload * 1.05) + messages * 4096
        self.assertEqual(estimate, expected)
        self.assertGreater(
            estimate_grib1_pair_size_bytes(2, 1, 1), estimate,
        )
        with self.assertRaises(ValueError):
            estimate_grib1_pair_size_bytes(1, 0, 1)


class TestArcoCacheCleanup(unittest.TestCase):
    def test_only_verified_hysplit_netcdf_source_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            arl_dir = root / "arl"
            arl_dir.mkdir()
            filedates = root / "filedates.exe"
            filedates.write_bytes(b"stub")
            first = datetime(2026, 6, 2, 10, tzinfo=timezone.utc)
            last = datetime(2026, 6, 5, 11, tzinfo=timezone.utc)
            source = root / "arco_era5_20260602T1025_20260605T1135_northern37_hysplit.nc"
            source.write_bytes(b"netcdf")
            output = arl_dir / "arco_era5_cache.arl"
            output.write_bytes(b"arl")
            metadata = {
                "source": str(source),
                "pressure_levels_hpa": list(PRESSURE_LEVELS),
                "first": first.isoformat(),
                "last": last.isoformat(),
                "time_count": 76,
                "validation": "NOAA Windows filedates (timestamps only)",
                "humidity_quality_control": {
                    "policy": "Negative q is floored only for RH. "
                             "No magnitude or prevalence cutoff is applied."
                },
            }
            output.with_suffix(".json").write_text(
                json.dumps(metadata), encoding="utf-8",
            )
            allvars = root / "arco_era5_20260602T1025_20260605T1135_northern37_allvars.nc"
            allvars.write_bytes(b"archive")

            with patch(
                "hysplit_tool.arco_convert.check_coverage",
                return_value=CoverageReport(True, ("verified",), first, last, 76),
            ):
                removed, freed = cleanup_verified_hysplit_netcdf_sources(
                    arl_dir, root, filedates,
                )
            self.assertEqual((removed, freed), (1, len(b"netcdf")))
            self.assertFalse(source.exists())
            self.assertTrue(allvars.exists())

    def test_old_rh_policy_does_not_authorize_netcdf_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            arl_dir = root / "arl"
            arl_dir.mkdir()
            filedates = root / "filedates.exe"
            filedates.write_bytes(b"stub")
            first = datetime(2026, 6, 2, 10, tzinfo=timezone.utc)
            last = datetime(2026, 6, 5, 11, tzinfo=timezone.utc)
            source = root / "arco_era5_20260602T1025_20260605T1135_northern37_hysplit.nc"
            source.write_bytes(b"netcdf")
            output = arl_dir / "arco_era5_old_policy.arl"
            output.write_bytes(b"arl")
            output.with_suffix(".json").write_text(json.dumps({
                "source": str(source),
                "pressure_levels_hpa": list(PRESSURE_LEVELS),
                "first": first.isoformat(),
                "last": last.isoformat(),
                "time_count": 76,
                "validation": "NOAA Windows filedates (timestamps only)",
                "humidity_quality_control": {"policy": "legacy cutoff"},
            }), encoding="utf-8")

            with patch(
                "hysplit_tool.arco_convert.check_coverage",
                return_value=CoverageReport(True, ("verified",), first, last, 76),
            ):
                removed, freed = cleanup_verified_hysplit_netcdf_sources(
                    arl_dir, root, filedates,
                )
            self.assertEqual((removed, freed), (0, 0))
            self.assertTrue(source.exists())


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
                np.full((2, len(PRESSURE_LEVELS), 2, 3), value, dtype=np.float32),
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
            for path, expected in ((pressure,2*len(PRESSURE_LEVELS)*6),(surface,2*7)):
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

