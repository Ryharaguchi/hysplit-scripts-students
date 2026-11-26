"""The download button must not require the NOAA HYSPLIT converter on a new PC."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hysplit_tool.gui import App


class TestDownloadOnly(unittest.TestCase):
    def test_cds_and_arco_download_without_hysplit(self):
        for provider, patch_name, fake_name in (
            ("ERA5 CDS (0.25°)", "download_cds", "sample.grib"),
            ("ERA5 Google ARCO (0.25°)", "download_google_arco", "sample.nc"),
        ):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                obs=root/"starts.csv"
                obs.write_text("2026/10/10 09:00,35.2,135.4,500\n",
                               encoding="utf-8-sig")
                app=App()
                app.withdraw()
                try:
                    app.v["provider"].set(provider)
                    app.v["csv"].set(str(obs))
                    app.v["root"].set(str(root/"out"))
                    app.v["executable"].set(str(root/"NOAA_NOT_INSTALLED"/"hyts_std.exe"))
                    data=app._data()
                    def source_only(materializer,window,*,action,progress):
                        self.assertEqual(action,"download")
                        self.assertFalse(materializer.executable.is_file())
                        return (root/fake_name,)
                    with patch("hysplit_tool.era5_materialize.Era5Materializer.prepare",source_only), \
                         patch("hysplit_tool.era5_materialize.run_native_grib1_pair",
                               side_effect=AssertionError("converter must not run")):
                        app._background("download",data)
                    events=[]
                    while not app.events.empty():
                        events.append(app.events.get_nowait())
                    errors=[event for event in events if event[0]=="error"]
                    self.assertFalse(errors,errors)
                    self.assertEqual([kind for kind,_ in events].count("done"),1)
                    self.assertFalse((root/"out"/"runs").exists())
                finally:
                    app.destroy()


if __name__=="__main__":
    unittest.main()

