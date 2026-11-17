<#
.SYNOPSIS
  Windows developer setup: create the venv, run lint + unit tests, then a live extract into a local lake.
.EXAMPLE
  .\scripts\dev.ps1                 # setup + tests + live run (no databases needed)
  .\scripts\dev.ps1 -SkipRun        # setup + tests only
#>
param([switch]$SkipRun, [int]$SinceDays = 30)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .venv)) { python -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "lint failed" }
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "unit tests failed" }

if (-not $SkipRun) {
    $env:GEOFUSE_STORE = "file://data/lake"
    & $py -m geofuse.pipeline run --no-load --since-days $SinceDays
    if ($LASTEXITCODE -ne 0) { throw "ETL run failed" }
    $latest = Get-ChildItem data\lake\runs\*.json | Sort-Object Name | Select-Object -Last 1
    Write-Host "Run report: $($latest.FullName)"
    Get-Content $latest.FullName -Raw | ConvertFrom-Json | Select-Object -ExpandProperty quality | Format-List
}
