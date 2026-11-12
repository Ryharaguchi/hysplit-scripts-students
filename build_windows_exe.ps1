# Build a self-contained Windows GUI. NOAA HYSPLIT is deliberately excluded.
param(
    [string]$Python = "",
    [string]$OutDir = "",
    [switch]$SkipZip
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not $Python) {
    $Python = Join-Path $root ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Build Python not found: $Python. Create a Windows venv and install requirements-era5.txt plus PyInstaller."
}
& $Python -c "import PyInstaller, xarray, eccodes, zarr, gcsfs, netCDF4; print('build-dependencies-ok')"
if ($LASTEXITCODE -ne 0) {
    throw "Missing packaging dependencies."
}
$dist = Join-Path $root "dist"
$build = Join-Path $root "build\pyinstaller"
& $Python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $build (Join-Path $root "HYSPLIT_Batch.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed (exit $LASTEXITCODE)"
}
$bundle = Join-Path $dist "HYSPLIT_Batch"
$exe = Join-Path $bundle "HYSPLIT_Batch.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Portable GUI EXE not found: $exe"
}
if (Test-Path -LiteralPath (Join-Path $bundle "HYSPLIT\exec\hyts_std.exe")) {
    throw "NOAA HYSPLIT binary accidentally included. Redistribution requires NOAA permission."
}
Copy-Item -LiteralPath (Join-Path $root "PORTABLE_README_JA.txt") -Destination (Join-Path $bundle "README_最初にお読みください.txt") -Force
$sampleDir = Join-Path $bundle "samples"
New-Item -ItemType Directory -Force -Path $sampleDir | Out-Null
Get-ChildItem (Join-Path $root "samples") -Filter "*.csv" -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $sampleDir -Force
}
$diag = Join-Path $root "build\portable_self_test.json"
& $exe --self-test $diag
if ($LASTEXITCODE -ne 0) {
    throw "Standalone EXE self-test failed. Open $diag for details."
}
if (-not (Test-Path -LiteralPath $diag)) {
    throw "EXE did not create diagnostics file: $diag"
}
$report = Get-Content -LiteralPath $diag -Encoding UTF8 -Raw | ConvertFrom-Json
if (-not $report.packaging_passed -or -not $report.frozen -or -not $report.gui_loaded) {
    throw "EXE failed dependency/Tkinter self-test. Inspect $diag."
}
Write-Host "Portable EXE standalone self-test passed."
if (-not $SkipZip) {
    if (-not $OutDir) {
        $OutDir = Join-Path $root "releases"
    }
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $archive = Join-Path $OutDir "HYSPLIT_Batch_Windows_x64.zip"
    if (Test-Path $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
    Compress-Archive -LiteralPath $bundle -DestinationPath $archive -CompressionLevel Optimal
    $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
    [IO.File]::WriteAllText("$archive.sha256", ($sha + "  HYSPLIT_Batch_Windows_x64.zip" + [Environment]::NewLine))
    Write-Host "PORTABLE_ZIP=$archive"
    Write-Host "ARCHIVE_BYTES=$((Get-Item -LiteralPath $archive).Length)"
    Write-Host "SHA256=$sha"
}
Write-Host "PORTABLE_EXE=$exe"

