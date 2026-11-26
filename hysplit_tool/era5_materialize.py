"""ERA5 local source/ARL materialization: content, not request filename.
Existing complete inputs are read-only. New source retrieval and conversion
are separated, and ARL work is published by verified time range.
"""
from __future__ import annotations
from contextlib import nullcontext, contextmanager
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import os
import tempfile

from .arl_catalog import (select_arl, inspect_arl, compatible_grid,
                          consecutive_hours, required_hours, HOUR, native_files)
from .grib_catalog import index_grib,write_pair,UPPER_KEYS,SURFACE_KEYS
from .era5 import (ARCO_URL, ARCO_PRESSURE_FIELDS, ARCO_SURFACE_FIELDS,
                   ARCO_HUMIDITY_ALTERNATIVES, ARCO_PRESSURE_LEVELS,
                   _subset_arco_domain,download_cds)
from .arco_convert import netcdf_to_grib1
from .arco_stream import export_hourly_netcdf
from .native_era5 import run_native_grib1_pair
from .validation import check_coverage
from .time_windows import TimeWindow

UTC=timezone.utc
CDS_ALL_FIELDS=frozenset((int(l),p) for l in ARCO_PRESSURE_LEVELS
                        for p in (60,75,76,129,130,131,132,133,135,138,155,157,203,246,247,248))

@contextmanager
def cache_writer(root):
    """OS-owned lock released automatically on process exit; no stale PID lock."""
    root=Path(root)
    root.mkdir(parents=True,exist_ok=True)
    with (root/".meteorology.lock").open("a+b") as f:
        if f.tell()==0:
            f.write(b"\0");f.flush()
        f.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another process is preparing meteorology in {root}. "
                               "Existing data is safe; retry after it finishes.") from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name=="nt":
                msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(f,fcntl.LOCK_UN)


class Era5Materializer:
    def __init__(self,provider,root,bbox,executable,field_profile,keep_sources,notify,
                 conversion_slot=None):
        if provider not in ("arco_era5","era5_cds") or field_profile not in ("hysplit","allvars"):
            raise ValueError("Unknown ERA5 provider or field profile")
        self.provider=provider
        self.root=Path(root)
        self.bbox=tuple(bbox)
        self.executable=Path(executable).resolve()
        self.profile=field_profile
        self.keep_sources=keep_sources
        self.notify=notify
        self.slot=conversion_slot or nullcontext
        self.arl=self.root/"arl"
        self.stage=self.root/"staging"
        self.source=self.root/"source"
        self.remote=None
        self.created_sources=set()
        self.created_gribs=set()

    def close(self):
        if self.remote is not None:
            self.remote.close()
            self.remote=None

    def _remote(self):
        if self.remote is None:
            import xarray as xr
            self.notify("Opening public Google ARCO ERA5 store once for missing data")
            self.remote=xr.open_zarr(ARCO_URL,storage_options={"token":"anon"},chunks=None)
        return self.remote

    def _archive_fields(self):
        schema_path=self.root/"arco_schema.json"
        if schema_path.is_file():
            try:
                schema=json.loads(schema_path.read_text(encoding="utf-8"))
                fields=set(schema["variables"])
                if (schema["source"]==ARCO_URL and
                    set(ARCO_PRESSURE_FIELDS+ARCO_SURFACE_FIELDS).issubset(fields)
                    and fields.intersection(ARCO_HUMIDITY_ALTERNATIVES)):
                    return fields
            except (OSError,ValueError,KeyError,TypeError):
                pass
        fields=set(self._remote().data_vars)
        self.root.mkdir(parents=True,exist_ok=True)
        tmp=schema_path.with_suffix(".json.part")
        tmp.write_text(json.dumps({"source":ARCO_URL,"variables":sorted(fields)},
                                  indent=2),encoding="utf-8")
        tmp.replace(schema_path)
        return fields

    def _nc_inventory(self,needed,archive=False):
        import numpy as np
        import xarray as xr
        needed=set(needed)
        # Source metadata is small; array values remain lazy.
        files=sorted(set(self.root.glob("*.nc"))|set(self.source.glob("*.nc"))|
                     set(self.root.glob("*.part"))|set(self.source.glob("*.part")))
        coverage={}
        all_fields=self._archive_fields() if archive else None
        for path in files:
            try:
                with xr.open_dataset(path,engine="netcdf4") as ds:
                    if not {"time","level","latitude","longitude"}.issubset(ds.coords):
                        continue
                    fields=set(ds.data_vars)
                    if not set(ARCO_PRESSURE_FIELDS+ARCO_SURFACE_FIELDS).issubset(fields):
                        continue
                    if not fields.intersection(ARCO_HUMIDITY_ALTERNATIVES):
                        continue
                    if all_fields is not None and not all_fields.issubset(fields):
                        continue
                    if not set(ARCO_PRESSURE_LEVELS).issubset(set(map(int,ds.level.values))):
                        continue
                    sel=_subset_arco_domain(ds,self.bbox)
                    lat=np.sort(np.asarray(sel.latitude.values))
                    lon=np.sort(np.asarray(sel.longitude.values))
                    n,w,s,e=self.bbox
                    expected_nx=1440 if abs(e-w-360)<1e-8 else round((e-w)/.25)+1
                    if (len(lat)!=round((n-s)/.25)+1 or len(lon)!=expected_nx
                        or not np.isclose(lat[0],s) or not np.isclose(lat[-1],n)
                        or not np.allclose(np.diff(lat),.25,rtol=0,atol=1e-5)):
                        continue
                    # Compare the actual longitude coordinate set modulo 360,
                    # including regions crossing Greenwich.
                    expected_lon=np.sort((w+np.arange(expected_nx)*.25)%360)
                    if not np.allclose(np.sort(lon%360),expected_lon,rtol=0,atol=1e-5):
                        continue
                    values=ds.time.values
                    if path.suffix==".part":
                        checkpoint=path.with_name(path.name+".json")
                        state=json.loads(checkpoint.read_text(encoding="utf-8"))
                        count=int(state["completed_hours"])
                        if state.get("format")!="arco-hourly-stream-v1" or not 0<=count<=len(values):
                            continue
                        values=values[:count]
                    times=[datetime.fromisoformat(str(np.datetime_as_string(t,unit="s"))).replace(tzinfo=UTC)
                           for t in values]
                    for t in times:
                        if t in needed:
                            # Prefer complete NC to checkpoint; larger continuous
                            # coverage reduces I/O. Preserve all other archives.
                            rank=(path.suffix!=".part",len(times))
                            old=coverage.get(t)
                            if old is None or rank>old[0]:
                                coverage[t]=(rank,path)
            except (OSError,ValueError,KeyError,TypeError,IndexError) as exc:
                self.notify(f"NetCDF source not eligible; retained {path.name}: {exc}")
        return {t:p for t,(_,p) in coverage.items()}

    def _fetch_arco(self,window,progress=None,archive=False):
        import numpy as np
        ds=self._remote()
        wanted=required_hours(window)
        time_values=[np.datetime64(t.replace(tzinfo=None),"s") for t in wanted]
        selected=_subset_arco_domain(ds.sel(time=time_values),self.bbox).sel(level=ARCO_PRESSURE_LEVELS)
        if not archive:
            humidity=next((x for x in ARCO_HUMIDITY_ALTERNATIVES if x in selected.data_vars),None)
            if humidity is None:raise ValueError("ARCO humidity is missing")
            if humidity=="specific_humidity":
                self.notify("ARCO specific_humidity: relative_humidity will be derived during GRIB conversion")
            selected=selected[ARCO_PRESSURE_FIELDS+ARCO_SURFACE_FIELDS+[humidity]]
        elif not self._archive_fields().issubset(set(selected.data_vars)):
            raise ValueError("ARCO source schema changed; check source product before continuing")
        self.source.mkdir(parents=True,exist_ok=True)
        token=hashlib.sha256(repr((self.bbox,self.profile,tuple(wanted))).encode()).hexdigest()[:12]
        path=self.source/f"arco_era5_{window.start:%Y%m%dT%H%M}_{window.end:%Y%m%dT%H%M}_{token}_{self.profile}.nc"
        if path.exists():
            raise RuntimeError(f"Existing NetCDF was not validated; preserved: {path}")
        result=export_hourly_netcdf(selected,path,source=ARCO_URL,notify=self.notify,progress=progress)
        self.created_sources.add(result.resolve())
        return result

    def _finalize_download_parts(self,nc,wanted,progress):
        import xarray as xr
        import numpy as np
        partials={}
        for t in wanted:
            path=nc[t]
            if path.suffix==".part":
                partials.setdefault(path,[]).append(t)
        for path,hours in partials.items():
            with xr.open_dataset(path,engine="netcdf4") as ds:
                for interval in consecutive_hours(hours,by_day=True):
                    times=required_hours(interval)
                    selected=ds.sel(time=[np.datetime64(t.replace(tzinfo=None),"s") for t in times])
                    selected=_subset_arco_domain(selected,self.bbox)
                    token=hashlib.sha256(repr((str(path.resolve()),tuple(times),self.bbox)).encode()).hexdigest()[:16]
                    self.source.mkdir(parents=True,exist_ok=True)
                    output=self.source/f"local_{interval.start:%Y%m%dT%H%M}_{token}.nc"
                    if output.exists():
                        raise RuntimeError(f"Existing output was not validated; preserved: {output}")
                    output=export_hourly_netcdf(selected,output,source=f"local:{path.resolve()}",
                                               notify=self.notify,progress=progress)
                    nc.update({t:output for t in times})
        return tuple(dict.fromkeys(nc[t] for t in wanted))

    def _grib_paths(self):
        return tuple(sorted(set(self.root.glob("*.grib"))|set(self.arl.glob("*.grib"))|
                            set(self.source.glob("*.grib"))|set(self.stage.glob("*.grib"))))

    def _exact_pair(self,records,hours):
        # A full already-generated source pair can be passed directly, without
        # a redundant local copy. Only do this when its complete message order
        # is the order required for this conversion.
        paths=[]
        for keys in (UPPER_KEYS,SURFACE_KEYS):
            seq=[records[t][k] for t in hours for k in keys]
            path=seq[0].path
            if any(m.path!=path for m in seq):
                return None
            pos=0
            for m in seq:
                if m.offset!=pos:
                    return None
                pos+=m.size
            if pos!=path.stat().st_size:
                return None
            paths.append(path)
        return tuple(paths)

    def _day_outputs(self,records,window):
        """Convert one contiguous work interval, splitting only on actual failure.
        Do not impose a maximum user duration or arbitrary byte limit.
        """
        wanted=required_hours(window)
        self.arl.mkdir(parents=True,exist_ok=True)
        self.stage.mkdir(parents=True,exist_ok=True)
        signature=repr((self.provider,self.bbox,window.as_pair(),self.profile))
        token=hashlib.sha256(signature.encode()).hexdigest()[:12]
        dest=self.arl/f"{self.provider}_{window.start:%Y%m%dT%H%M}_{window.end:%Y%m%dT%H%M}_{token}.arl"
        staged=dest.with_suffix(".arl.part")
        if dest.exists():
            info=inspect_arl(dest)
            if info.times!=wanted or not compatible_grid(info,self.bbox):
                raise RuntimeError(f"Existing output differs from requested ARL; preserved {dest}")
            return (dest,)
        converter=self.executable.parent/"grib2arl.exe"
        filedates=self.executable.parent/"filedates.exe"
        generated_stage=not staged.exists()
        validation_failed=False
        try:
            if generated_stage:
                # Reuse a full original pair directly only when it exactly
                # matches this requested interval. Otherwise copy the packed
                # messages into a task-owned scratch pair without re-encoding.
                grid=records[wanted[0]][UPPER_KEYS[0]].grid
                # All input messages and the expected output grid are known.
                arl_bytes=len(wanted)*(1+37*6+6)*(int(grid[0])*int(grid[1])+50)
                direct=self._exact_pair(records,wanted)
                with dest.with_suffix(".arl.log").open("a",encoding="utf-8") as f:
                    f.write(f"\nRequested UTC: {wanted[0].isoformat()} .. {wanted[-1].isoformat()}; "
                            f"{len(wanted)} hourly records; grid={grid}; "
                            f"method={'existing pair' if direct else 'byte-copy slice'}\n")
                    for path in sorted(set(m.path for t in wanted for m in records[t].values())):
                        f.write(f"Source GRIB: {path}; bytes={path.stat().st_size}\n")
                if direct is not None:
                    with self.slot():
                        staged=run_native_grib1_pair(*direct,dest,converter,
                            notify=self.notify,expected_arl_bytes=arl_bytes)
                else:
                    with tempfile.TemporaryDirectory(prefix="grib_slice_",dir=self.stage) as tmp:
                        p=Path(tmp)/"pressure.grib";s=Path(tmp)/"surface.grib"
                        # Do not begin a copy if the required bytes cannot fit.
                        import shutil
                        copied=sum(records[t][k].size for t in wanted for k in UPPER_KEYS+SURFACE_KEYS)
                        if shutil.disk_usage(tmp).free < copied+arl_bytes:
                            raise OSError("Not enough free disk space for GRIB slice and ARL output")
                        write_pair(wanted,records,p,s)
                        with self.slot():
                            staged=run_native_grib1_pair(p,s,dest,converter,
                                notify=self.notify,expected_arl_bytes=arl_bytes)
            try:
                report=check_coverage([staged],window.start,window.end,filedates,max_gap_hours=1.0)
                info=inspect_arl(staged)
                if not report.verified or info.times!=wanted or not compatible_grid(info,self.bbox):
                    raise ValueError("ARL content/time coverage differs from request")
            except (ValueError,RuntimeError) as exc:
                validation_failed=True
                log=dest.with_suffix(".arl.log")
                with log.open("a",encoding="utf-8") as f:
                    f.write(f"\nARL validation failure: {type(exc).__name__}: {exc}\n")
                raise RuntimeError(f"ARL validation failure: {exc}; log: {log}") from exc
            staged.replace(dest)
            metadata={"provider":self.provider,"field_profile":self.profile,
                      "first":wanted[0].isoformat(),"last":wanted[-1].isoformat(),
                      "time_count":len(wanted),"pressure_levels_hpa":ARCO_PRESSURE_LEVELS,
                      "input_grib_files":sorted(set(str(m.path) for t in wanted for m in records[t].values())),
                      "validation":"ARL index/time/grid/levels plus NOAA filedates"}
            dest.with_suffix(".json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
            return (dest,)
        except (RuntimeError,OSError) as exc:
            # Narrow recovery path: split a failed converter work unit, not
            # the trajectory, and never repeat a failing same-size command.
            recoverable=("grib2arl returned" in str(exc) or
                         "Not enough free disk space" in str(exc) or
                         validation_failed)
            # Only discard an invalid derived scratch file from this exact
            # attempt, never an existing ARL or input GRIB/NetCDF.
            if validation_failed and staged.exists():
                if generated_stage:
                    staged.unlink()
                else:
                    # The previous run may have stopped after conversion.
                    # Keep its invalid output as evidence, not a permanent
                    # blocker for recovery from intact raw data.
                    import uuid
                    quarantine=self.stage/f"{staged.name}.{uuid.uuid4().hex}.invalid"
                    staged.replace(quarantine)
                    self.notify(f"Unusable staged ARL retained: {quarantine}")
                    return self._day_outputs(records,window)
            if not recoverable or len(wanted)==1 or staged.exists():
                raise
            self.notify(f"Conversion of {len(wanted)} hourly records failed: {exc}. "
                        "Trying smaller consecutive work units from the same local GRIB data.")
            cut=len(wanted)//2
            a=TimeWindow(wanted[0],wanted[cut-1])
            b=TimeWindow(wanted[cut],wanted[-1])
            return self._day_outputs(records,a)+self._day_outputs(records,b)

    def _encode_nc(self,path,hours):
        self.stage.mkdir(parents=True,exist_ok=True)
        st=path.stat()
        token=hashlib.sha256(repr((str(path.resolve()),st.st_size,st.st_mtime_ns,tuple(hours),self.bbox)).encode()).hexdigest()[:16]
        p=self.stage/f"arco_{token}_pressure.grib";s=self.stage/f"arco_{token}_surface.grib"
        if p.exists() and s.exists():
            return p,s
        # Do not overwrite an existing source. If an interrupted generated
        # pair has only one component, keep it for inspection.
        if p.exists() or s.exists():
            import uuid
            # Generated scratch is not a scientific archive; preserve an
            # interrupted pair under a unique name and rebuild missing input.
            for old in (p,s):
                if old.exists():
                    old.replace(old.with_name(old.name+"."+uuid.uuid4().hex+".incomplete"))
        q={}
        netcdf_to_grib1(path,p,s,notify=self.notify,quality_report=q,
                         wanted_times=tuple(hours),bbox=self.bbox)
        p.with_suffix(".quality.json").write_text(json.dumps(q,indent=2),encoding="utf-8")
        self.created_gribs.update((p.resolve(),s.resolve()))
        return p,s

    def _download_cds(self,part):
        self.source.mkdir(parents=True,exist_ok=True)
        before=set(self.source.glob("*.grib"))
        paths=download_cds(part.start,part.end,self.source,self.bbox,
                           notify=self.notify,field_profile=self.profile,exact_hours=True)
        self.created_sources.update(p.resolve() for p in map(Path,paths) if p not in before)
        return paths

    def _cleanup_created(self,paths):
        # Only files generated by this materializer are eligible. A preexisting
        # student archive is never removed. Preserve failed-day input and logs.
        for path in set(map(Path,paths)):
            resolved=path.resolve()
            transient=resolved in self.created_gribs
            source=resolved in self.created_sources and not self.keep_sources and self.profile!="allvars"
            if not (transient or source):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self.notify(f"Temporary source retained: {path.name}: {exc}")
                continue
            self.created_gribs.discard(resolved)
            self.created_sources.discard(resolved)

    def prepare(self,window,*,action,progress=None):
        with cache_writer(self.root):
            return self._prepare(window,action=action,progress=progress)

    def _prepare(self,window,*,action,progress=None):
        wanted=required_hours(window)
        archive=self.profile=="allvars"
        if action=="run":
            existing,missing=select_arl(self.arl,window,self.bbox,self.notify)
            if existing and not missing and not archive:
                self.notify("Reusing verified ARL files without download or conversion: "+", ".join(p.name for p in existing))
                return native_files(existing,window,self.arl/"native_views")
        else:
            existing=();missing=wanted
        self.notify(f"{self.provider}: {len(wanted)} required UTC hours, {len(wanted)-len(missing)} ARL-covered hours")

        needed=set(missing)
        records=index_grib(self._grib_paths(),self.bbox,needed,self.notify) if needed and action=="run" else {}
        # Full-variable source retrieval remains distinct from the ARL-only
        # request: ARL never counts as a full scientific archive.
        if self.provider=="arco_era5":
            nc_hours=set(wanted) if archive or action=="download" else needed-set(records)
            nc=self._nc_inventory(nc_hours,archive=archive) if nc_hours else {}
            source_needed=set(wanted if archive or action=="download" else ())
            for part in consecutive_hours(source_needed-set(nc),by_day=True):
                path=self._fetch_arco(part,progress,archive=archive)
                nc.update({t:path for t in required_hours(part)})
            if action=="download":
                return self._finalize_download_parts(nc,wanted,progress)
        else:
            nc={}

        # CDS source-only and archival requests use actual GRIB field
        # coverage, not a matching filename.
        if self.provider=="era5_cds" and (archive or action=="download"):
            from .grib_catalog import REQUIRED_KEYS
            source_keys=REQUIRED_KEYS|CDS_ALL_FIELDS if archive else REQUIRED_KEYS
            source_records=index_grib(self._grib_paths(),self.bbox,wanted,self.notify,keys=source_keys)
            for part in consecutive_hours(set(wanted)-set(source_records),by_day=True):
                paths=self._download_cds(part)
                source_records.update(index_grib(paths,self.bbox,required_hours(part),self.notify,keys=source_keys))
            if set(wanted)-set(source_records):
                raise RuntimeError("CDS source archive incomplete")
            if action=="download":
                return tuple(dict.fromkeys(m.path for t in wanted for m in source_records[t].values()))
            records.update({t:v for t,v in source_records.items() if t in needed})

        # Work one day at a time, but only for missing records. Existing
        # multi-day NC/GRIB remains in place and no time is downloaded twice.
        for part in consecutive_hours(needed,by_day=True):
            times=required_hours(part)
            absent=set(times)-set(records)
            if self.provider=="arco_era5":
                # NC exists but GRIB does not: encode only missing times.
                for gap in consecutive_hours(absent-set(nc),by_day=True):
                    path=self._fetch_arco(gap,progress,archive=archive)
                    nc.update({t:path for t in required_hours(gap)})
                by_path={}
                for t in sorted(absent):
                    by_path.setdefault(nc[t],[]).append(t)
                for path,ts in by_path.items():
                    for interval in consecutive_hours(ts):
                        h=required_hours(interval)
                        with self.slot():
                            pair=self._encode_nc(path,h)
                        records.update(index_grib(pair,self.bbox,h,self.notify))
            else:
                for gap in consecutive_hours(absent,by_day=True):
                    paths=self._download_cds(gap)
                    records.update(index_grib(paths,self.bbox,required_hours(gap),self.notify))
            incomplete=set(times)-set(records)
            if incomplete:
                raise RuntimeError(f"Missing complete GRIB records: {sorted(incomplete)}")
            self._day_outputs(records,part)
            # New raw files are created only for these requested day/hour
            # intervals. Remove generated intermediates after verified ARL
            # publication; never remove data that existed before this run.
            used={m.path for t in times for m in records[t].values()}
            used.update(nc[t] for t in times if t in nc)
            remaining=set(needed)-set(times)
            needed.difference_update(times)
            active={m.path for t in remaining if t in records for m in records[t].values()}
            active.update(nc[t] for t in remaining if t in nc)
            self._cleanup_created(used-active)
        selected,still_missing=select_arl(self.arl,window,self.bbox,self.notify)
        if still_missing:
            raise RuntimeError(f"ARL is incomplete after materialization: {still_missing}")
        return native_files(selected,window,self.arl/"native_views")
