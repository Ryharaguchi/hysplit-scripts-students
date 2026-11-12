import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.meteo import RemoteFile, download_arl_file
from hysplit_tool.validation import check_coverage, check_signature, parse_filedates
from hysplit_tool.plot import create_map, read_tdump

UTC = timezone.utc


class MockResponse(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class PartialResponse(MockResponse):
    def __init__(self):
        super().__init__(b"01234", headers={"ETag": '"unchanged"', "Content-Length": "10"})
        self.calls = 0

    def read(self, amount=-1):
        self.calls += 1
        if self.calls > 1:
            raise OSError("connection dropped")
        return super().read(amount)


class TestResume(unittest.TestCase):
    def test_resume_range_with_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            seen = []

            def opener(request, timeout):
                seen.append(request)
                if len(seen) == 1:
                    return PartialResponse()
                return MockResponse(b"56789", status=206,
                                    headers={"Content-Range": "bytes 5-9/10", "Content-Length": "5"})

            spec = RemoteFile("test.arl", ("https://example.test/archive",))
            with self.assertRaises(RuntimeError):
                download_arl_file(spec, target, opener=opener)
            self.assertEqual((target / "test.arl.part").read_bytes(), b"01234")
            finished = download_arl_file(spec, target, opener=opener)
            self.assertEqual(finished.read_bytes(), b"0123456789")
            self.assertEqual(seen[-1].get_header("Range"), "bytes=5-")
            self.assertEqual(seen[-1].get_header("If-range"), '"unchanged"')
            self.assertFalse((target / "test.arl.part.json").exists())

    def test_range_ignored_overwrites_not_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            target.joinpath("test.arl.part").write_bytes(b"BADPRE")
            target.joinpath("test.arl.part.json").write_text(
                '{"url":"https://example.test/file","etag":"123"}')
            def opener(request, timeout):
                self.assertEqual(request.get_header("Range"), "bytes=6-")
                return MockResponse(b"GOOD", status=200, headers={"Content-Length": "4"})
            path = download_arl_file(RemoteFile("test.arl", ("https://example.test/file",)),
                                     target, opener=opener)
            self.assertEqual(path.read_bytes(), b"GOOD")

    def test_mismatched_range_does_not_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            folder.joinpath("a.part").write_bytes(b"abc")
            folder.joinpath("a.part.json").write_text('{"url":"https://x.test/a","etag":"v1"}')
            def opener(request, timeout):
                return MockResponse(b"DEF", status=206, headers={"Content-Range":"bytes 1-3/4"})
            with self.assertRaises(RuntimeError):
                download_arl_file(RemoteFile("a", ("https://x.test/a",)), folder, opener=opener)
            self.assertEqual(folder.joinpath("a.part").read_bytes(), b"abc")


class TestValidation(unittest.TestCase):
    def test_parse_filedates_forecast_and_century(self):
        dates = parse_filedates("date\n26 10 10 00 03\n26 10 10 06 00\n99 12 31 23 00\n")
        self.assertEqual(dates[-1], datetime(2026, 10, 10, 6, tzinfo=UTC))
        self.assertEqual(dates[0], datetime(1999, 12, 31, 23, tzinfo=UTC))

    def test_format_guard_and_optional_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "met.arl"
            path.write_bytes(b"GRIB\x00")
            with self.assertRaises(ValueError):
                check_signature(path)
            path.write_bytes(b"ARLTEST")
            report = check_coverage([path], datetime(2026, 1, 1, tzinfo=UTC),
                                    datetime(2026, 1, 2, tzinfo=UTC))
            self.assertFalse(report.verified)

    def test_filedates_invoked_from_arl_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            met = directory / "met.arl"
            exe = directory / "filedates.exe"
            met.write_bytes(b"ARLTEST")
            exe.write_bytes(b"dummy")
            from subprocess import CompletedProcess
            with patch("hysplit_tool.validation.subprocess.run",
                       return_value=CompletedProcess([], 0,
                           stdout="26 10 10 00 00\n26 10 10 03 00\n26 10 10 06 00", stderr="")) as runner:
                report = check_coverage(
                    [met], datetime(2026, 10, 10, 0, tzinfo=UTC),
                    datetime(2026, 10, 10, 6, tzinfo=UTC), exe)
            self.assertTrue(report.verified)
            command, = runner.call_args.args
            self.assertEqual(command[-1], met.name)
            self.assertEqual(runner.call_args.kwargs["cwd"], met.resolve().parent)

    def test_valid_coverage_and_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            met, tool = d / "met.arl", d / "filedates.exe"
            met.write_bytes(b"ARLTEST")
            tool.write_bytes(b"dummy")
            start = datetime(2026, 10, 10, 0, tzinfo=UTC)
            end = datetime(2026, 10, 10, 6, tzinfo=UTC)
            from subprocess import CompletedProcess
            with patch("hysplit_tool.validation.subprocess.run",
                       return_value=CompletedProcess([], 0,
                           stdout="26 10 10 00 00\n26 10 10 03 00\n26 10 10 06 00", stderr="")):
                report = check_coverage([met], start, end, tool, max_gap_hours=3)
            self.assertTrue(report.verified)
            with patch("hysplit_tool.validation.subprocess.run",
                       return_value=CompletedProcess([], 0,
                           stdout="26 10 10 00 00\n26 10 10 06 00", stderr="")):
                with self.assertRaisesRegex(ValueError, "Gap"):
                    check_coverage([met], start, end, tool, max_gap_hours=3)


DUMP = """     1     2
GDAS     26 10 10 00 00
     1 BACKWARD OMEGA
 26 10 10 00 35.000 135.000 500.0
     1 PRESSURE
     1     1    26    10    10    00    00    00     0.0 35.0 135.0 500.0 900.0
     1     1    26    10    09    23    00    00    -1.0 35.2 134.7 490.0 905.0
"""


class TestPlot(unittest.TestCase):
    def test_geojson_html_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "runs" / "20261010"
            case = run / "20261010T0000Z_00000"
            case.mkdir(parents=True)
            (case / "tdump").write_text(DUMP, encoding="ascii")
            points = read_tdump(case / "tdump")
            self.assertEqual(points[1][0][:2], (35.0, 135.0))
            page, geo, shown, total = create_map(run)
            self.assertEqual((shown, total), (1, 1))
            self.assertIn("FeatureCollection", geo.read_text(encoding="utf-8"))
            html = page.read_text(encoding="utf-8").lower()
            self.assertIn("<svg", html)
            self.assertIn("offline", html)
            self.assertNotIn("https://", html)
            # The standard SVG XML namespace is an identifier, not a network request.
            self.assertNotIn("http://", html.replace('xmlns="http://www.w3.org/2000/svg"', ""))
            self.assertNotIn("<script", html)

    def test_offline_plot_crosses_dateline_safely(self):
        from hysplit_tool.offline_plot import offline_html, _unwrap
        features = [{
            "type": "Feature",
            "properties": {"trajectory": 1, "run": "test", "start_altitude_agl_m": 500},
            "geometry": {"type": "LineString",
                         "coordinates": [[179.9, 35, 500], [-179.9, 35.1, 500]]},
        }]
        html = offline_html(features, "test", "1 of 1")
        self.assertIn("<path", html)
        self.assertEqual(_unwrap([179.9, -179.9], 179.9), [179.9, 180.1])
        self.assertNotIn("tile.openstreetmap", html)
        self.assertNotIn("unpkg.com", html)

    def test_invalid_tdump_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tdump"
            path.write_text("garbage", encoding="ascii")
            with self.assertRaises(ValueError):
                read_tdump(path)


if __name__ == "__main__":
    unittest.main()
