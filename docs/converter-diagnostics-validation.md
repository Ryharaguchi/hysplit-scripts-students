# Converter stability and simple conversion log — 2026-11-26

Development branch, not a deployed update. v0.1.1 distribution remains unchanged.

## Application

At the user's request, retain the existing single .arl.log per ARL target. No additional diagnostic ZIP, JSON hierarchy, GUI controls or settings. Append rather than overwrite. Record requested UTC window, source and actual converter-input names/sizes, converter SHA256, command, exit code, elapsed time, disk space, complete MESSAGE/WARNING/CFG_ARL text. On conversion failure only, scan existing GRIB headers to report field/level/time coverage, missing/duplicate required keys, grid and file offsets; list anomalous hours instead of repeating all complete hours. Reuse existing 64-bit metadata reader. No additional raw archive or scientific data changes. The log is a local diagnostic (may contain local file paths). No binary CFG_GRIB copy.

Tests: 133 passed, 1 skipped, 41 subtests passed. Regression explicitly checks that 80 warning lines and previous attempts survive without additional diagnostic files. Real patched Windows converter invoked via the revised app runner, with both complete pressure and surface inputs, yielded the reference ARL SHA256 below. The single text log contained both pressure-level tables and native text files (31,261 bytes, 721 lines).

## Native converter candidate

Upstream NOAA hysplit_data2arl commit 5f66398aa35a4c0830729ea47d91dfb92fe9ec11. Source and build retained locally, separate from the distributed converter. In legacy/grib2arl.f ANALYZE, allocate SIGP(NLVL) instead of SIGP(SIZE(SIGL,1)) to prevent sorting uninitialized extra levels. Add the valid level and EXIST1 field flags before/after sorting to MESSAGE. No physical/packing changes. Compiled with GNU Fortran 11.4.0 on Linux and GNU Fortran 10-posix MinGW-w64 cross compiler on Linux, statically linked Windows x64 executable (KERNEL32 and msvcrt only), bounds checking enabled. The private toolchain was extracted to a separate workspace, not installed system-wide. Upstream dependency library built from the repository metprog/library. Full flags: -O2 -g -ffree-form -fconvert=big-endian -frecord-marker=4 -falign-commons -finit-local-zero -fcheck=bounds -fbacktrace (Windows link additionally -static).

Real CDS 1440x361, all 37 pressure levels, 2026-04-30 20–23 UTC. Previously the 22–23 UTC pair failed with CFG_ARL error 64 (two retries); patched Linux and Windows both succeeded, 238,109,620 B, reference SHA256 88baececf968bf11c1ab44465a3d83f5602e84c6e36c784e4e71cc15b4c19eb9. Windows also passed the previously successful first 2h and full 4h inputs. All outputs exactly match corresponding bytes from successful legacy 4h ARL, all required UTC hours/38 layers validated with filedates and actual ARL headers. See converter-windows-validation.json.

This confirms the candidate works on the previously failing real input, not that the student's 124h ARCO error 1131 is fixed. The exact installed binary's source provenance is not proven. Full-scale 48h/124h ARCO comparison still pending. NOAA LICENSE.txt refers to the HYSPLIT Use Agreement; distribution permission for modified converter remains to be confirmed. No NOAA source or binary is committed or redistributed. Original source meteorology and deployed files unchanged.
