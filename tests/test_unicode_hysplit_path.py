"""Regression: private transfer folder may contain Japanese path components.

NOAA's FORTRAN ASCDATA.CFG accepts only ASCII paths; tests ensure that the
required small terrain tables are copied beside the run and referred to
through a relative ASCII path. The model itself remains native Windows.
"""
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path

from hysplit_tool.core import StartPoint,group_starts,write_group


class TestUnicodeHysplitPath(unittest.TestCase):
    @staticmethod
    def _group():
        return group_starts([
            StartPoint(datetime(2019,1,5,3,tzinfo=timezone.utc),
                       -69.0174,40.0856,1000)
        ])[0]

    @staticmethod
    def _bdy(root: Path):
        root.mkdir(parents=True)
        for name in ("LANDUSE.ASC","ROUGLEN.ASC","TERRAIN.ASC"):
            (root/name).write_bytes((name+"TEST").encode("ascii"))
        return root

    def test_japanese_drive_path_creates_portable_ascii_relative_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            bdy=self._bdy(root/"マイドライブ"/"配布"/"HYSPLIT"/"bdyfiles")
            dest=root/"work"
            dest.mkdir()
            folder=write_group(self._group(),dest,[root/"fake.arl"],
                               2,20000,"AGL",bdy)
            contents=(folder/"ASCDATA.CFG").read_bytes()
            self.assertTrue(contents.isascii())
            self.assertIn(b"'bdyfiles/'",contents)
            for name in ("LANDUSE.ASC","ROUGLEN.ASC","TERRAIN.ASC"):
                self.assertEqual((folder/"bdyfiles"/name).read_bytes(),
                                 (bdy/name).read_bytes())

    def test_ascii_model_path_preserves_existing_direct_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            bdy=self._bdy(root/"HYSPLIT"/"bdyfiles")
            dest=root/"work"
            dest.mkdir()
            folder=write_group(self._group(),dest,[root/"fake.arl"],
                               2,20000,"AGL",bdy)
            contents=(folder/"ASCDATA.CFG").read_text(encoding="ascii")
            self.assertIn(bdy.resolve().as_posix().rstrip("/")+"/",contents)
            self.assertFalse((folder/"bdyfiles").exists())


if __name__=="__main__":
    unittest.main()

