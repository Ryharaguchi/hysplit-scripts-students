# Converter stability and v0.1.2 app update — 2026-11-26

## Additional diagnosis

On the first patched Windows build, the 124-hour 40x40 synthetic GRIB pair no longer failed on pressure-level sorting, but an all-at-once conversion and the 6-hour/22-hour edge slices yielded invalid forecast/duplicate time indices. The four complete 24-hour slices passed. The native MESSAGE trace showed repeated upper and surface records after the expected final hour, consistent with the cached direct-access input buffer being reused after an unsuccessful read.

Local candidate now also patches metprog/library/fcread.f. It obtains file size via INQUIRE(..., SIZE=...), computes the requested read range in 64-bit integers and returns EOF if that range lies outside the file. It does not prefetch/cache a next buffer when the requested read ends at the current buffer boundary. Scientific field values, GRIB input headers, accumulation time encoding, grids and ARL packing are unchanged. The isolated patch is stored locally as grib2arl-stability.patch against upstream NOAA commit 5f66398aa35a4c0830729ea47d91dfb92fe9ec11.

## Verification

- Windows final native candidate SHA256: 39493a7e1e0bae1c4a95323ab3650875873ff54e24fc46c7ca5f0b8c978a5771.
- Native reader tests: 2048-, 4096-, and 4113-byte files. Full reads, exact boundary reads, repeated EOF requests, final-byte rereads passed.
- Synthetic 124 hours: one-shot 124 and six daily chunks (6+24+24+24+24+22) all passed, all 124 hourly ARL records byte-identical. App materializer using final candidate and existing GRIB offline also passed, six daily ARLs equal to one-shot. This is not the student's actual 124-hour ARCO dataset.
- Real full northern hemisphere CDS GRIB1, 48 UTC hours (2026-04-29 00 to 04-30 23), 1440x361, 37 pressure levels: first and final candidate both yielded 5,714,630,880-byte ARL, 48/48 hourly blocks identical to successful legacy reference. Final candidate 241.41 s including structural/filedates/hash verification.
- Deliberate missing-geopotential test on two synthetic hours: converter returned 1131 twice, app retained both executions and all input gaps in the same existing .arl.log (30,600 B); no diagnostic bundle or extra log hierarchy. Actual student's missing-field condition has not been established.
- Final app regression: 133 passed, 1 skipped, 41 subtests passed.
- Intermediate duplicate 48-hour ARL created for this test only was removed after equality was established. Prior reference, final candidate ARL, original GRIB/NetCDF and existing distribution are retained.

## App-only update

Source commit 63f3206. Windows x64 standalone build and self-test passed. ZIP 3,704 files, CRC and each entry SHA256 verified, local and Drive ZIP SHA256 identical. Version 0.1.2 app-only ZIP placed alongside v0.1.1; existing installed distribution is not replaced. User can exchange EXE and _internal together; same work folder and existing NOAA installation. Conversion logs continue to use the same .arl.log filenames, append full diagnostic text, and report successful structural/time verification. No change to physical settings.

## Distribution restriction

The public data2arl LICENSE.txt refers to the HYSPLIT Use Agreement. NOAA's agreement (https://hysplit2.arl.noaa.gov/HYSPLIT_agreement.php) states that software redistribution requires NOAA ARL permission. The revised NOAA converter has not been distributed or copied to the app ZIP, Drive distribution, or GitHub; local testing only. Permission or an upstream corrected release is required before student distribution. No request has been sent to NOAA. Student-specific 124-hour ARCO/1131 reproduction remains unverified. Cloud synchronization of the app ZIP is unverified.

See converter-stability-final-validation.json for numeric results.
