import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from hysplit_tool.era5_convert import _ceil_hour, _converter_command, convert_cds_to_arl
from hysplit_tool.validation import CoverageReport

UTC = timezone.utc


class TestEra5ConversionBridge(unittest.TestCase):
    def test_ceil_hour(self):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(_ceil_hour(base), base)
        self.assertEqual(_ceil_hour(base + timedelta(minutes=25)), base + timedelta(hours=1))

    def test_external_converter_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(FileNotFoundError):
                convert_cds_to_arl([], root, datetime(2026, 1, 1, tzinfo=UTC),
                                   datetime(2026, 1, 2, tzinfo=UTC),
                                   converter="/not/existing/api2arl", filedates=root / "filedates")

    def test_native_command(self):
        with tempfile.TemporaryDirectory() as td:
            executable = Path(td) / "api2arl"
            executable.write_bytes(b"fake")
            command = _converter_command(str(executable), Path(td), "input.grib2", "output.arl")
            self.assertEqual(command[-3:], ["-iinput.grib2", "-ooutput.arl", "-t50"])

    def test_stages_monthly_arl_and_calls_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            converter, filedates = root / "api2arl", root / "filedates"
            converter.write_bytes(b"tool")
            filedates.write_bytes(b"tool")
            date_a = datetime(2026, 1, 31, 23, tzinfo=UTC)
            date_b = datetime(2026, 2, 1, 0, tzinfo=UTC)
            seen = []

            def fake_split(source, work, earliest, latest, notify):
                work.mkdir(parents=True)
                paths = {}
                for when in (date_a, date_b):
                    file = work / f"{when:%Y%m%dT%H}.grib2"
                    file.write_bytes(b"fake GRIB2")
                    paths[when] = file
                return paths

            def fake_run(args, cwd, capture_output, text, errors, timeout):
                seen.append(tuple(args))
                Path(cwd, "output.arl").write_bytes(b"ARL hourly record")
                return CompletedProcess(args, 0, stdout="OK", stderr="")

            def fake_check(paths, earliest, latest, filedates):
                self.assertEqual(len(paths), 2)
                self.assertTrue(all(p.name.endswith(".part") for p in paths))
                return CoverageReport(True, ("verified",))

            with patch("hysplit_tool.era5_convert.convert_grib_to_hourly_grib2", fake_split), \
                 patch("hysplit_tool.era5_convert.subprocess.run", fake_run), \
                 patch("hysplit_tool.era5_convert.check_coverage", fake_check):
                paths = convert_cds_to_arl([root / "input.grib"],
                    root / "dest", date_a, date_b,
                    converter=str(converter), filedates=filedates)
            self.assertEqual([p.name for p in paths],
                             ["era5_cds_202601.arl", "era5_cds_202602.arl"])
            self.assertEqual(len(seen), 2)
            self.assertTrue(all(p.read_bytes() == b"ARL hourly record" for p in paths))


if __name__ == "__main__":
    unittest.main()
