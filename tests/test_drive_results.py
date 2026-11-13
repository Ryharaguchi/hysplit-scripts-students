"""Optional Google Drive result-copy handling on native Windows.

Tests use temporary local folders. An H: Google Drive account is never
required in CI and no NOAA HYSPLIT executable is redistributed or copied.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from hysplit_tool.drive_results import (
    copy_completed_run, suggested_drive_results_root,
)


class TestGoogleDriveCopy(unittest.TestCase):
    @staticmethod
    def _completed_run(root: Path) -> Path:
        group=root/"20261010T0000Z_00000"
        group.mkdir(parents=True)
        (group/"tdump").write_text("tdump test\n",encoding="ascii")
        (group/"MESSAGE").write_text("HYSPLIT test\n",encoding="utf-8")
        (root/"trajectories.csv").write_text(
            "run_group,endpoint_time_utc\n"
            "20261010T0000Z_00000,2026-10-10T00:00Z\n",encoding="utf-8-sig",
        )
        (root/"manifest.json").write_text(
            json.dumps({"status":"complete"}),encoding="utf-8",
        )
        (root/"observations.csv").write_text(
            "2026/10/10 09:00,35,135,500\n",encoding="utf-8",
        )
        mapdir=root/"map"
        mapdir.mkdir()
        (mapdir/"index.html").write_text("<html>test</html>",encoding="utf-8")
        return root

    def test_correct_run_tree_is_copied_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            t=Path(tmp)
            src=self._completed_run(t/"local"/"runs"/"20261010T0000Z_123abc")
            dest=t/"drive"/"HYSPLIT"
            dest.parent.mkdir()
            logs=[]
            copied=copy_completed_run(src,dest,notify=logs.append)
            self.assertEqual(copied,dest/"runs"/src.name)
            self.assertEqual((copied/"observations.csv").read_bytes(),
                             (src/"observations.csv").read_bytes())
            self.assertEqual((copied/"trajectories.csv").read_bytes(),
                             (src/"trajectories.csv").read_bytes())
            self.assertTrue((copied/"map"/"index.html").is_file())
            self.assertTrue((copied/"20261010T0000Z_00000"/"MESSAGE").is_file())
            self.assertFalse(any(x.name.startswith(".") for x in (dest/"runs").iterdir()))
            self.assertTrue(any("保存完了" in line for line in logs))

    def test_do_not_overwrite_existing_science_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            t=Path(tmp)
            src=self._completed_run(t/"source"/"RUNID")
            dest=t/"drive"/"HYSPLIT"
            dest.parent.mkdir()
            copy_completed_run(src,dest)
            with self.assertRaises(FileExistsError):
                copy_completed_run(src,dest)
            self.assertEqual((dest/"runs"/"RUNID"/"manifest.json").read_text(encoding="utf-8"),
                             '{"status": "complete"}')

    def test_missing_local_outputs_and_disconnected_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            t=Path(tmp)
            src=self._completed_run(t/"source"/"RUNID")
            with self.assertRaisesRegex(FileNotFoundError,"unavailable"):
                copy_completed_run(src,t/"drive_missing"/"HYSPLIT")
            (src/"trajectories.csv").unlink()
            with self.assertRaisesRegex(ValueError,"not a complete"):
                copy_completed_run(src,t/"drive_missing"/"HYSPLIT")

    def test_drive_suggestion_never_falls_back_to_other_google_account(self):
        candidate=suggested_drive_results_root()
        self.assertTrue(candidate=="" or Path(candidate)==Path("H:/マイドライブ/HYSPLIT"))
        self.assertNotIn("G:",candidate)

    def test_gui_can_disable_or_change_destination(self):
        from hysplit_tool.gui import App
        with tempfile.TemporaryDirectory() as td:
            app=App()
            app.withdraw()
            try:
                self.assertIn("cloud_copy_root",app.v)
                app.v["cloud_copy_root"].set("")
                self.assertEqual(app.v["cloud_copy_root"].get(),"")
                app.v["cloud_copy_root"].set(str(Path(td)/"HYSPLIT"))
                self.assertEqual(app.v["cloud_copy_root"].get(),
                                 str(Path(td)/"HYSPLIT"))
            finally:
                app.destroy()


if __name__=="__main__":
    unittest.main()

