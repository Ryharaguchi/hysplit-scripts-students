# v0.1.1 — 2026-11-26

## Release scope

- Retain the current installed NOAA HYSPLIT and grib2arl.exe.
- ERA5 uses a content-based UTC time index. Reuse existing ARL, then local GRIB1, then local NetCDF; download only missing times. UTC day is the usual processing unit, not a trajectory or request duration limit.
- Windows GRIB record positions are obtained using 64-bit unbuffered file positions; ecCodes offset values beyond 2 GiB were incorrect in the actual environment.
- Keep the existing raw data and previous verified ARL intact. No data migration is required. Existing user work directory and settings stay unchanged.
- Retain northern hemisphere, 0.25-degree, all 37 pressure levels, hourly resolution, selected variables and model settings.

## Verified results

Real CDS-derived 1440 x 361, 37-pressure-level GRIB1, 48 hours: one 48-hour conversion and two 24-hour conversions with the installed grib2arl produced exactly identical 48 ARL time blocks. Both original GRIB hashes were unchanged; concatenated split inputs matched both original files. One-pass conversion 383.52 s; 24-hour conversions 189.42 s + 178.88 s, excluding splitting/verification. See release-0.1.1-grib-split-validation.json.

Windows regression before release: 132 passed, 1 skipped, 41 subtests passed (4b4f31f). Previous real 72-hour HYSPLIT trajectory test yielded 433 identical output rows with old and new CONTROL.

## Known limitations

The user authorized distribution of the daily split implementation under the working hypothesis that the student's 124-hour failure was related to file size. The original student's 124-hour ARCO GRIB and error 1131 log are unavailable, so this is not a confirmed cause or a guarantee of recovery. Independent short-window CFG_ARL error 64 was reproduced even with correct input; it remains unresolved. -q1 config reuse failed and is not included. No fixed 6-hour or byte cap; no automatic scientific field/grid reduction. On failure, keep original input and inspect .arl.log.

## Deployment

Replace the application EXE and _internal together. Existing NOAA model installations and HYSPLIT_Work data remain unchanged. The source of this release is the time-indexed meteorology development branch.
