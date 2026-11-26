"""Content-based ERA5 ARL inventory (NOAA packed regular lat/lon files).
Never modifies existing meteorological files. A time record is indexed only
when its INDX structure, data extent, level list and boundary labels agree.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib
import math
from functools import lru_cache
from .era5 import PRESSURE_LEVELS
from .time_windows import TimeWindow

UTC=timezone.utc
HOUR=timedelta(hours=1)
UPPER={"HGTS","TEMP","UWND","VWND","WWND","RELH"}
SURFACE={"SHGT","U10M","V10M","T02M","PBLH","TPP6"}

@dataclass(frozen=True)
class ArlInfo:
    path: Path
    grid: tuple
    levels: tuple
    variables: tuple
    block_bytes: int
    times: tuple

    @property
    def identity(self):
        return self.grid,self.levels,self.variables

def _decode_shape(label,index):
    nx,ny,nz=(int(index[i:i+3]) for i in (93,96,99))
    a,b=label[12:14]
    if 64<=a<=90 and 64<=b<=90:
        nx+=1000*(a-64)
        ny+=1000*(b-64)
    return nx,ny,nz

def _when(label,index):
    yy,mo,day,hour=(int(label[i:i+2]) for i in (0,2,4,6))
    year=2000+yy if yy<=69 else 1900+yy
    # Index forecast and minute fields are explicitly represented. ERA5
    # analysis indices have forecast=0. Do not accept forecast as analysis.
    if int(index[4:7])!=0 or int(index[7:9])!=0:
        raise ValueError("ERA5 ARL is not an hourly analysis index")
    return datetime(year,mo,day,hour,tzinfo=UTC)

def _index(stream,offset):
    stream.seek(offset)
    label=stream.read(50)
    ix=stream.read(108)
    if len(label)!=50 or len(ix)!=108 or label[14:18]!=b"INDX":
        raise ValueError("Missing complete ARL INDX")
    nx,ny,nz=_decode_shape(label,ix)
    if min(nx,ny,nz)<=0:
        raise ValueError("Invalid ARL dimensions")
    index_len=int(ix[104:108])
    if index_len<108 or nx*ny<108:
        raise ValueError("Incomplete ERA5 index record")
    # NOAA permits an index to span multiple fixed-length records.
    payload=ix+stream.read(min(index_len,nx*ny)-108)
    index_records=(index_len+nx*ny-1)//(nx*ny)
    for i in range(1,index_records):
        stream.seek(offset+i*(50+nx*ny))
        extra=stream.read(50)
        if extra[:14]!=label[:14] or extra[14:18]!=b"INDX":
            raise ValueError("ARL continuation index label mismatch")
        payload+=stream.read(min(index_len-len(payload),nx*ny))
    if len(payload)!=index_len:
        raise ValueError("Incomplete ARL index payload")
    levels=[]; variables=[]; pos=108
    for _ in range(nz):
        level=float(payload[pos:pos+6])
        count=int(payload[pos+6:pos+8])
        pos+=8
        names=tuple(payload[pos+j*8:pos+j*8+4].decode("ascii") for j in range(count))
        pos+=count*8
        if len(set(names))!=count or pos>index_len:
            raise ValueError("Invalid ARL level/variable table")
        levels.append(level);variables.append(names)
    if pos!=index_len:
        raise ValueError("ARL index length differs from variable table")
    grid=(nx,ny,int(ix[102:104]),*(float(ix[i:i+7]) for i in range(9,93,7)))
    return _when(label,ix),grid,tuple(levels),tuple(variables),index_records

@lru_cache(maxsize=512)
def _scan(path_text,size,mtime):
    path=Path(path_text)
    with path.open("rb") as f:
        first,grid,levels,variables,nindex=_index(f,0)
        if levels!=(0.0,*map(float,PRESSURE_LEVELS)):
            raise ValueError("ERA5 ARL does not contain surface and all 37 pressure levels")
        if not SURFACE.issubset(variables[0]) or any(not UPPER.issubset(x) for x in variables[1:]):
            raise ValueError("ERA5 ARL required fields missing")
        nx,ny=grid[:2]
        rec=50+nx*ny
        block=rec*(nindex+sum(map(len,variables)))
        if size%block:
            raise ValueError("Truncated ERA5 ARL time block")
        times=[]
        for offset in range(0,size,block):
            when,g,l,v,ni=_index(f,offset)
            if (g,l,v,ni)!=(grid,levels,variables,nindex):
                raise ValueError("ARL grid/levels/variables vary within file")
            if times and when!=times[-1]+HOUR:
                raise ValueError("Non-continuous or duplicate ERA5 ARL record")
            # Read boundary field labels rather than every packed grid value.
            f.seek(offset+rec*nindex)
            left=f.read(50)
            f.seek(offset+block-rec)
            right=f.read(50)
            if left[14:18].decode("ascii")!=variables[0][0] or right[14:18].decode("ascii")!=variables[-1][-1]:
                raise ValueError("ARL boundary data records differ from index")
            times.append(when)
    return ArlInfo(path,grid,levels,variables,block,tuple(times))

def inspect_arl(path):
    p=Path(path).resolve()
    stat=p.stat()
    return _scan(str(p),stat.st_size,stat.st_mtime_ns)

def compatible_grid(info,bbox):
    north,west,south,east=map(float,bbox)
    nx,ny,vertical,pole_lat,pole_lon,dy,dx,spacing,orientation,cone,sx,sy,slat,slon,reserved=info.grid
    # The grid is regular latitude/longitude. Check both exact dimensions
    # and coordinates; full longitude origins may differ, but mixing
    # differently oriented files is prevented by identity.
    close=lambda a,b:math.isclose(a,b,abs_tol=1e-5,rel_tol=0)
    if not (vertical==2 and close(dx,.25) and close(dy,.25) and close(spacing,0) and close(orientation,0)):
        return False
    if not (close(slat+(1-sy)*dy,south) and close(slat+(ny-sy)*dy,north)):
        return False
    full=close(east-west,360)
    if full:
        return nx==1440
    return close((nx-1)*dx,east-west) and close(((slon+(1-sx)*dx-west+180)%360)-180,0)

def required_hours(window):
    start=window.start.replace(minute=0,second=0,microsecond=0)
    end=window.end.replace(minute=0,second=0,microsecond=0)
    if end<window.end:
        end+=HOUR
    return tuple(start+i*HOUR for i in range((end-start)//HOUR+1))

def consecutive_hours(hours,*,by_day=False):
    """Return inclusive, continuous time groups; day boundary is optional."""
    groups=[]
    for t in sorted(set(hours)):
        if not groups or t!=groups[-1][-1]+HOUR or (by_day and t.date()!=groups[-1][-1].date()):
            groups.append([t])
        else:
            groups[-1].append(t)
    return tuple(TimeWindow(g[0],g[-1]) for g in groups)

@lru_cache(maxsize=128)
def _block_hash(path,size,mtime,offset,length):
    digest=hashlib.sha256()
    with Path(path).open("rb") as f:
        f.seek(offset)
        remaining=length
        while remaining:
            b=f.read(min(8*1024*1024,remaining))
            if not b:
                raise ValueError("ARL changed during overlap verification")
            digest.update(b);remaining-=len(b)
    return digest.digest()

def _same_record(a,b,t):
    if a.block_bytes!=b.block_bytes:
        return False
    def h(i):
        s=i.path.stat()
        return _block_hash(str(i.path),s.st_size,s.st_mtime_ns,
                           i.times.index(t)*i.block_bytes,i.block_bytes)
    return h(a)==h(b)

def select_arl(arl_dir,window,bbox,notify=None):
    """Return a compatible file cover and missing hourly records.
    Provider provenance is scoped by the provider directory, not filename.
    No file deletion, no guessing at ambiguous overlapping records.
    """
    needed=set(required_hours(window))
    groups={}
    for p in sorted(Path(arl_dir).glob("*.arl")):
        try:
            info=inspect_arl(p)
            if compatible_grid(info,bbox) and needed.intersection(info.times):
                groups.setdefault(info.identity,[]).append(info)
        except (OSError,ValueError,UnicodeError) as e:
            if notify: notify(f"ERA5 ARL not eligible; preserved {p.name}: {e}")
    if not groups:
        return (),tuple(sorted(needed))
    if len(groups)>1:
        raise RuntimeError("Multiple incompatible ERA5 ARL grids/variable layouts overlap requested period; preserved all")
    pool=next(iter(groups.values()))
    missing=needed-set(t for i in pool for t in i.times)
    remaining=needed-missing
    selected=[]
    while remaining:
        t=min(remaining)
        candidates=[i for i in pool if t in i.times]
        # Prefer the file providing the longest continuous forward coverage.
        chosen=min(candidates,key=lambda i:(-len(remaining.intersection(i.times)),str(i.path)))
        selected.append(chosen)
        remaining.difference_update(chosen.times)
    # Compare only records in the requested range, including a duplicate
    # candidate that was not selected. Never delete or silently choose a
    # conflicting meteorological value.
    checked=set()
    for a in selected:
        for b in pool:
            if a==b:continue
            pair=frozenset((a.path,b.path))
            if pair in checked:continue
            checked.add(pair)
            for t in sorted(needed.intersection(a.times,b.times)):
                if not _same_record(a,b,t):
                    raise RuntimeError(f"Different ARL values for {t.isoformat()} in {a.path.name} and {b.path.name}; preserved both")
    return tuple(i.path for i in sorted(selected,key=lambda i:i.times[0])),tuple(sorted(missing))


def files_for_window(paths,window):
    """Restrict an already verified set to the times required by one group."""
    needed=set(required_hours(window))
    chosen=[]
    for p in paths:
        info=inspect_arl(p)
        if needed.intersection(info.times):
            chosen.append(Path(p))
            needed.difference_update(info.times)
    if needed:
        raise RuntimeError(f"Trajectory group has missing ARL times: {sorted(needed)}")
    return chosen


def native_files(paths,window,view_dir):
    """HYSPLIT needs a time interval within each input file.
    Make a two-record view only for a singleton, never rewrite source files.
    All records are copied unchanged; a neighboring multi-hour ARL is not
    recopied in full. Views live outside the primary inventory.
    """
    import os
    import tempfile
    needed=set(required_hours(window))
    infos=[inspect_arl(p) for p in paths]
    if not infos:
        raise ValueError("No verified ERA5 ARLs")
    if len({i.identity for i in infos})!=1:
        raise ValueError("Native file views require one identical ARL grid")
    locations={}
    for info in infos:
        for t in info.times:
            if t in needed:
                locations.setdefault(t,info)
    if not needed.issubset(locations):
        raise ValueError("Missing required ARL hours before creating native views")
    result=[]
    for info in infos:
        if len(info.times)!=1:
            result.append(info.path)
            continue
        t=info.times[0]
        neighbor=next((x for x in (t-HOUR,t+HOUR) if x in locations),None)
        if neighbor is None:
            raise ValueError(f"No neighbor for singleton ARL {t.isoformat()}")
        pair=sorted((t,neighbor))
        loc=[(locations[h],h) for h in pair]
        identity=[]
        for a,h in loc:
            stat=a.path.stat()
            identity.append((str(a.path),stat.st_size,stat.st_mtime_ns,h.isoformat()))
        key=hashlib.sha256(repr(identity).encode()).hexdigest()[:20]
        directory=Path(view_dir)
        directory.mkdir(parents=True,exist_ok=True)
        output=directory/f"native_{key}.arl"
        if not output.exists():
            tmp=None
            try:
                with tempfile.NamedTemporaryFile(dir=directory,suffix=".part",delete=False) as out:
                    tmp=Path(out.name)
                    for a,h in loc:
                        remaining=a.block_bytes
                        with a.path.open("rb") as src:
                            src.seek(a.times.index(h)*a.block_bytes)
                            while remaining:
                                data=src.read(min(8*1024*1024,remaining))
                                if not data:raise ValueError("Source ARL changed during view creation")
                                out.write(data);remaining-=len(data)
                    out.flush();os.fsync(out.fileno())
                check=inspect_arl(tmp)
                if check.times!=tuple(pair) or check.identity!=info.identity:
                    raise ValueError("Native view validation failed")
                os.replace(tmp,output)
            finally:
                if tmp is not None:tmp.unlink(missing_ok=True)
        check=inspect_arl(output)
        if check.times!=tuple(pair) or check.identity!=info.identity:
            raise RuntimeError(f"Invalid existing native view preserved: {output}")
        result.append(output)
    return tuple(dict.fromkeys(sorted(result,key=lambda p:inspect_arl(p).times[0])))
