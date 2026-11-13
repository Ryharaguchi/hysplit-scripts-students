"""Excel CSV interoperability, strict four-column position and safe errors."""
import csv
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hysplit_tool.core import load_starts_csv, parse_jst

UTC=timezone.utc


class TestExcelCsv(unittest.TestCase):
    def test_excel_serial_jst(self):
        serial = (datetime(2026, 10, 10, 9, 0) - datetime(1899, 12, 30)).total_seconds() / 86400
        self.assertEqual(parse_jst(str(serial)),datetime(2026,10,10,0,tzinfo=UTC))
        self.assertEqual(parse_jst("202610100900.0"),datetime(2026,10,10,0,tzinfo=UTC))
        self.assertEqual(parse_jst("2026年10月10日 9時00分"),
                         datetime(2026,10,10,0,tzinfo=UTC))
        self.assertEqual(parse_jst("2026/10/10 9:00"),
                         datetime(2026,10,10,0,tzinfo=UTC))

    def test_utf16_excel_tab_and_cp932(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"starts.csv"
            text="日時\t緯度\t経度\t高度\n2026/10/10 9:00\t35.2\t135.4\t500\n"
            path.write_bytes(text.encode("utf-16"))
            a=load_starts_csv(path)
            self.assertEqual(len(a),1)
            self.assertEqual(a[0].height_m,500)
            path.write_bytes(text.encode("cp932"))
            self.assertEqual(load_starts_csv(path),a)

    def test_semicolon_excel_decimal_comma_and_sep_header(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"starts.csv"
            path.write_text(
                "sep=;\n時刻;緯度;経度;高度\n"
                "2026/10/10 09:00;35,2;135,4;500,5\n",
                encoding="utf-8-sig",
            )
            points=load_starts_csv(path)
            self.assertEqual(len(points),1)
            self.assertEqual((points[0].latitude,points[0].longitude,points[0].height_m),
                             (35.2,135.4,500.5))

    def test_comma_csv_with_extra_blank_columns_and_number_thousands(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"starts.csv"
            path.write_text(
                "時刻,緯度,経度,高度,\n"
                '2026/10/10 09:00,35.2,135.4,"1,000",\n',
                encoding="utf-8-sig",
            )
            points=load_starts_csv(path)
            self.assertEqual(len(points),1)
            self.assertEqual(points[0].height_m,1000.0)

    def test_bad_xlsx_rename_and_scientific_notation_clear_errors(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"starts.csv"
            path.write_bytes(b"PK\x03\x04" + b"\0"*32)
            with self.assertRaisesRegex(ValueError,"Excel workbook"):
                load_starts_csv(path)
            path.write_text("2.0261E+11,35.2,135.4,500\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"scientific notation"):
                load_starts_csv(path)

    def test_reject_extra_nonempty_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"starts.csv"
            path.write_text("2026/10/10 09:00,35,135,500,EXTRA\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"exactly four columns"):
                load_starts_csv(path)


if __name__=="__main__":
    unittest.main()

