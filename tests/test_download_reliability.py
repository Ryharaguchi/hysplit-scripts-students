"""Test public S3 precedence, retry, resume and GUI download-only paths offline."""
import io
import importlib.util
import os
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.meteo import (
    RemoteFile, download_arl_file, plan_archives, _download_error_hint,
)
from hysplit_tool.era5 import download_google_arco

UTC=timezone.utc


class ControlledResponse:
    def __init__(self, data: bytes, *, status=200, headers=None,
                 fail_after_chunk=False):
        self.data=data
        self.status=status
        self.headers=headers or {}
        self.offset=0
        self.fail_after_chunk=fail_after_chunk
        self.failed=False

    def __enter__(self):
        return self

    def __exit__(self,*_):
        return False

    def read(self, size):
        if self.fail_after_chunk and self.offset > 0 and not self.failed:
            self.failed=True
            raise ConnectionResetError("simulated connection dropped")
        data=self.data[self.offset:self.offset+size]
        self.offset+=len(data)
        return data


class TestPortableNoaaDownload(unittest.TestCase):
    def test_s3_primary_and_valid_year_month(self):
        gdas=plan_archives("gdas1",
                           datetime(2026,10,10,tzinfo=UTC),
                           datetime(2026,10,10,tzinfo=UTC))
        self.assertTrue(gdas[0].urls[0].startswith(
            "https://noaa-oar-arl-hysplit-pds.s3.amazonaws.com/gdas1/2026/"))
        self.assertTrue(any("www.ready.noaa.gov" in x for x in gdas[0].urls))
        gfs=plan_archives("gfs0p25",
                          datetime(2026,10,10,tzinfo=UTC),
                          datetime(2026,10,10,tzinfo=UTC))
        self.assertTrue(any("/gfs0p25/2026/10/20261010_gfs0p25" in x
                            for item in gfs for x in item.urls))

    def test_retries_resumes_with_if_range_etag_and_logs(self):
        payload=b"NOAAARLFILE"*100
        split=300
        seen=[]
        logs=[]
        def opener(req,timeout):
            seen.append((req.full_url,dict(req.headers)))
            if len(seen)==1:
                return ControlledResponse(
                    payload[:split], headers={"ETag":'"etag1"',
                                              "Content-Length":str(len(payload))},
                    fail_after_chunk=True)
            self.assertEqual(req.get_header("Range"),f"bytes={split}-")
            self.assertEqual(req.get_header("If-range"),'"etag1"')
            return ControlledResponse(
                payload[split:],status=206,headers={
                    "ETag":'"etag1"', "Content-Range":
                    f"bytes {split}-{len(payload)-1}/{len(payload)}",
                    "Content-Length":str(len(payload)-split)})
        with tempfile.TemporaryDirectory() as td, \
             patch("hysplit_tool.meteo.time.sleep",return_value=None), \
             patch("hysplit_tool.meteo.PROGRESS_INTERVAL_SECONDS",0):
            spec=RemoteFile("test.arl",("https://s3.example/test.arl",))
            downloaded=download_arl_file(
                spec,Path(td),notify=logs.append,opener=opener,max_retries=2)
            self.assertEqual(downloaded.read_bytes(),payload)
            self.assertEqual(len(seen),2)
            self.assertTrue(any("Resuming" in v for v in logs),logs)
            self.assertTrue(any("MiB" in v for v in logs),logs)
            self.assertFalse((Path(td)/"test.arl.part").exists())

    def test_http_404_falls_back_without_retry(self):
        calls=[]
        def opener(req,timeout):
            calls.append(req.full_url)
            if "s3." in req.full_url:
                raise urllib.error.HTTPError(req.full_url,404,"Not Found",None,None)
            return io.BytesIO(b"VALID_ARL_SAMPLE")
        with tempfile.TemporaryDirectory() as td:
            spec=RemoteFile("some.arl",(
                "https://s3.example/test","https://ready.example/test"))
            path=download_arl_file(spec,Path(td),opener=opener)
            self.assertEqual(path.read_bytes(),b"VALID_ARL_SAMPLE")
            self.assertEqual(len(calls),2)

    def test_certificate_error_message_never_suggests_disabling_ssl(self):
        hint=_download_error_hint(
            urllib.error.URLError("CERTIFICATE_VERIFY_FAILED"))
        self.assertIn("trusted CA",hint)
        self.assertIn("Do not disable",hint)


@unittest.skipUnless(
    os.name == "nt" and all(
        importlib.util.find_spec(name) is not None
        for name in ("xarray", "netCDF4", "numpy")
    ),
    "Optional Windows ERA5 xarray/netCDF4/numpy dependencies unavailable",
)
class TestArcoCache(unittest.TestCase):
    def test_second_acquisition_reuses_netcdf_without_opening_remote_zarr(self):
        import xarray as xr
        import numpy as np
        from hysplit_tool.arco_convert import PRESSURE_LEVELS, PRESSURE_PARAMS, SURFACE_PARAMS
        coords={"time":np.array(["2019-01-05T00:00","2019-01-05T01:00"],
                                  dtype="datetime64[m]"),
                "latitude":[35.25,35.0],
                "longitude":[134.75,135.0],
                "level":list(PRESSURE_LEVELS)}
        fields={}
        for name,*_ in PRESSURE_PARAMS:
            if name=="relative_humidity":continue
            fields[name]=(("time","level","latitude","longitude"),
                         np.ones((2,12,2,2),dtype="float32"))
        fields["specific_humidity"]=(("time","level","latitude","longitude"),
                                     np.ones((2,12,2,2),dtype="float32")*0.001)
        for name,*_ in SURFACE_PARAMS:
            if name=="geopotential_at_surface":
                fields[name]=(("latitude","longitude"),np.ones((2,2),dtype="float32"))
            else:
                fields[name]=(("time","latitude","longitude"),
                             np.ones((2,2,2),dtype="float32"))
        ds=xr.Dataset(fields,coords=coords)
        a=datetime(2019,1,5,0,tzinfo=UTC)
        b=datetime(2019,1,5,1,tzinfo=UTC)
        bbox=(35.25,134.75,35.0,135.0)
        with tempfile.TemporaryDirectory() as td:
            with patch("xarray.open_zarr",return_value=ds):
                original=download_google_arco(a,b,Path(td),bbox)
            self.assertTrue(original.is_file())
            with patch("xarray.open_zarr",side_effect=RuntimeError(
                "Network unavailable on this Windows PC")) as mocked:
                reused=download_google_arco(a,b,Path(td),bbox)
            self.assertEqual(reused,original)
            mocked.assert_not_called()


if __name__=="__main__":
    unittest.main()

