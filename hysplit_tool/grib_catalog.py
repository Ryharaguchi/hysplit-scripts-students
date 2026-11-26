"""Read GRIB metadata and copy complete time records without re-encoding.
Used for both newly downloaded and existing GRIB inputs, irrespective of name.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime,timezone
from functools import lru_cache
from pathlib import Path
import math
import os
from .era5 import PRESSURE_LEVELS

UTC=timezone.utc
PRESSURE_IDS=(129,130,131,132,135,157)
SURFACE_IDS=(167,165,166,134,228,159,129)
UPPER_KEYS=tuple((int(l),p) for l in PRESSURE_LEVELS for p in PRESSURE_IDS)
SURFACE_KEYS=tuple((0,p) for p in SURFACE_IDS)
REQUIRED_KEYS=frozenset(UPPER_KEYS+SURFACE_KEYS)

@dataclass(frozen=True)
class Message:
    path: Path
    offset: int
    size: int
    grid: tuple
    when: datetime
    field: tuple

def _grid(e,h):
    return tuple(e.codes_get(h,k) for k in (
        "Ni","Nj","latitudeOfFirstGridPointInDegrees","latitudeOfLastGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees","longitudeOfLastGridPointInDegrees",
        "iDirectionIncrementInDegrees","jDirectionIncrementInDegrees",
        "iScansNegatively","jScansPositively","jPointsAreConsecutive",
    ))

def matches_grid(g,bbox):
    nx,ny,la1,la2,lo1,lo2,dx,dy,neg,pos,jfirst=g
    north,west,south,east=map(float,bbox)
    eq=lambda x,y:math.isclose(float(x),float(y),abs_tol=1e-5,rel_tol=0)
    if not (eq(dx,.25) and eq(dy,.25) and eq(min(la1,la2),south) and eq(max(la1,la2),north)
            and eq((ny-1)*dy,north-south)):
        return False
    if eq(east-west,360):
        return nx==1440 and eq((lo2-lo1)%360,(nx-1)*dx if not neg else dx)
    return eq((nx-1)*dx,east-west) and eq(((lo2 if neg else lo1)-west+180)%360-180,0)

@lru_cache(maxsize=128)
def _scan(path_text,size,mtime):
    import eccodes as e
    path=Path(path_text)
    records=[]
    with path.open("rb") as f:
        while True:
            h=e.codes_grib_new_from_file(f)
            if h is None:break
            try:
                if e.codes_get(h,"edition")!=1:
                    raise ValueError(f"GRIB edition other than 1: {path.name}")
                t=datetime.strptime(f"{e.codes_get(h,'validityDate'):08d}{e.codes_get(h,'validityTime'):04d}","%Y%m%d%H%M").replace(tzinfo=UTC)
                if t.minute:
                    continue
                p=int(e.codes_get(h,"paramId"))
                level_type=e.codes_get(h,"typeOfLevel")
                if level_type=="isobaricInhPa":
                    level=int(e.codes_get(h,"level"))
                    key=(level,p)
                else:
                    key=(0,p)
                # Keep only HYSPLIT fields in this index. The source archive
                # retains every other variable unchanged.
                if key[0] in map(int,PRESSURE_LEVELS) or key in REQUIRED_KEYS:
                    records.append(Message(path,int(e.codes_get_message_offset(h)),
                        int(e.codes_get_message_size(h)),_grid(e,h),t,key))
            finally:
                e.codes_release(h)
    return tuple(records)

def scan_grib(path):
    p=Path(path).resolve()
    s=p.stat()
    return _scan(str(p),s.st_size,s.st_mtime_ns)

def message_bytes(record):
    with record.path.open("rb") as f:
        f.seek(record.offset)
        b=f.read(record.size)
    if len(b)!=record.size or not b.startswith(b"GRIB") or not b.endswith(b"7777"):
        raise ValueError(f"Invalid or changed GRIB record: {record.path}")
    return b

def index_grib(paths,bbox,wanted,notify=None,*,keys=REQUIRED_KEYS):
    """A complete hour must have all required fields on one identical grid.
    Ambiguous overlapping GRIB records are compared as bytes, not renamed.
    """
    wanted=set(wanted)
    by_hour=defaultdict(lambda:defaultdict(dict))
    for path in sorted(set(map(Path,paths))):
        try:
            records=scan_grib(path)
        except (ValueError,OSError,RuntimeError) as exc:
            if notify:notify(f"GRIB source not eligible; retained {path.name}: {exc}")
            continue
        for m in records:
            if m.field not in keys or m.when not in wanted or not matches_grid(m.grid,bbox):
                continue
            fields=by_hour[m.when][m.grid]
            prev=fields.get(m.field)
            if prev and prev!=m and message_bytes(prev)!=message_bytes(m):
                raise RuntimeError(f"Conflicting GRIB field {m.field} at {m.when}; {prev.path.name}, {m.path.name}; retained both")
            fields[m.field]=m
    complete={}
    for t,grids in by_hour.items():
        groups=[v for v in grids.values() if keys.issubset(v)]
        if len(groups)>1:
            raise RuntimeError(f"Multiple complete but differently oriented GRIB grids at {t}; retained all")
        if groups:complete[t]=groups[0]
    return complete

def write_pair(hours,records,pressure,surface):
    """Write exact message bytes in valid-time, pressure and variable order."""
    from contextlib import ExitStack
    pressure,surface=Path(pressure),Path(surface)
    with ExitStack() as stack:
        streams={}
        upper=stack.enter_context(pressure.open("wb"))
        lower=stack.enter_context(surface.open("wb"))
        for t in hours:
            for key in UPPER_KEYS+SURFACE_KEYS:
                m=records[t][key]
                if m.path not in streams:
                    streams[m.path]=stack.enter_context(m.path.open("rb"))
                src=streams[m.path]
                src.seek(m.offset);b=src.read(m.size)
                if len(b)!=m.size or not b.startswith(b"GRIB") or not b.endswith(b"7777"):
                    raise ValueError(f"Incomplete GRIB message in {m.path}")
                (upper if key[0] else lower).write(b)
        for f in (upper,lower):
            f.flush()
            os.fsync(f.fileno())
