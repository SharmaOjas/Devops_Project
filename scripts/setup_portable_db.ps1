<#
  Dark Ship Watch - portable PostgreSQL 18 + PostGIS bootstrap (Windows).

  Builds a PRIVATE database cluster inside the project folder so the project runs without touching
  an existing PostgreSQL installation:
    1. copies bin/lib/share from an installed PostgreSQL 18 into .pgportable\pg
    2. overlays the official PostGIS Windows bundle (zip from download.osgeo.org) on top of the copy
    3. initdb's a fresh cluster (trust authentication, localhost only) on port 5433
    4. creates the "darkshipwatch" database

  Usage (from the project folder):
    powershell -ExecutionPolicy Bypass -File scripts\setup_portable_db.ps1
    powershell -ExecutionPolicy Bypass -File scripts\setup_portable_db.ps1 -PostgisZip C:\path\postgis-bundle-pg18-3.6.2x64.zip

  The script is idempotent: every step is skipped when its result already exists.
#>
param(
    [string]$PgSource   = 'C:\Program Files\PostgreSQL\18',
    [string]$PostgisZip = '',
    [int]$Port          = 5433,
    [string]$DbName     = 'darkshipwatch'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Root        = Join-Path $ProjectRoot '.pgportable'
$PgHome      = Join-Path $Root 'pg'
$DataDir     = Join-Path $Root 'data'
$LogFile     = Join-Path $Root 'postgres.log'
$BundleUrl   = 'https://download.osgeo.org/postgis/windows/pg18/postgis-bundle-pg18-3.6.2x64.zip'

New-Item -ItemType Directory -Force $Root | Out-Null

# --- 1. copy the PostgreSQL binaries --------------------------------------------------------------
if (-not (Test-Path (Join-Path $PgHome 'bin\postgres.exe'))) {
    if (-not (Test-Path (Join-Path $PgSource 'bin\postgres.exe'))) {
        throw "PostgreSQL 18 not found at $PgSource (use -PgSource to point at an installation)."
    }
    Write-Host '[1/4] Copying PostgreSQL binaries ...'
    foreach ($d in 'bin', 'lib', 'share') {
        robocopy (Join-Path $PgSource $d) (Join-Path $PgHome $d) /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $d (exit $LASTEXITCODE)" }
    }
    $global:LASTEXITCODE = 0
} else { Write-Host '[1/4] PostgreSQL binaries already copied.' }

# --- 2. overlay PostGIS ---------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $PgHome 'share\extension\postgis.control'))) {
    if (-not $PostgisZip) {
        $PostgisZip = Join-Path $Root 'postgis-bundle.zip'
        if (-not (Test-Path $PostgisZip)) {
            Write-Host "[2/4] Downloading PostGIS bundle (~118 MB) from $BundleUrl ..."
            Invoke-WebRequest -UseBasicParsing -Uri $BundleUrl -OutFile $PostgisZip
        }
    }
    Write-Host '[2/4] Overlaying PostGIS bundle ...'
    $Extract = Join-Path $Root 'postgis-extract'
    if (Test-Path $Extract) { Remove-Item -Recurse -Force $Extract }
    Expand-Archive -Path $PostgisZip -DestinationPath $Extract -Force
    $Bundle = Get-ChildItem $Extract -Directory | Select-Object -First 1
    foreach ($d in 'bin', 'lib', 'share') {
        robocopy (Join-Path $Bundle.FullName $d) (Join-Path $PgHome $d) /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $d (exit $LASTEXITCODE)" }
    }
    $global:LASTEXITCODE = 0
    if (Test-Path (Join-Path $Bundle.FullName 'gdal-data')) {
        robocopy (Join-Path $Bundle.FullName 'gdal-data') (Join-Path $PgHome 'gdal-data') /E /NFL /NDL /NJH /NJS /NP | Out-Null
        $global:LASTEXITCODE = 0
    }
    Remove-Item -Recurse -Force $Extract
} else { Write-Host '[2/4] PostGIS already overlaid.' }

# --- 3. initdb + start ----------------------------------------------------------------------------
$env:Path = (Join-Path $PgHome 'bin') + ';' + $env:Path
$env:GDAL_DATA = Join-Path $PgHome 'gdal-data'

if (-not (Test-Path (Join-Path $DataDir 'PG_VERSION'))) {
    Write-Host '[3/4] Initialising cluster ...'
    & (Join-Path $PgHome 'bin\initdb.exe') -D $DataDir -U postgres -E UTF8 --auth=trust | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
    Add-Content (Join-Path $DataDir 'postgresql.conf') @"

# --- Dark Ship Watch portable cluster ---
port = $Port
listen_addresses = 'localhost'
max_connections = 60
shared_buffers = 256MB
log_min_duration_statement = 500
track_io_timing = on
"@
} else { Write-Host '[3/4] Cluster already initialised.' }

& (Join-Path $PgHome 'bin\pg_isready.exe') -h localhost -p $Port | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      starting server on port $Port ..."
    # Start-Process (not a pipe): the server inherits stdout and would keep a pipe open forever.
    Start-Process -FilePath (Join-Path $PgHome 'bin\pg_ctl.exe') -ArgumentList @('-D', "`"$DataDir`"", '-l', "`"$LogFile`"", 'start') -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        & (Join-Path $PgHome 'bin\pg_isready.exe') -h localhost -p $Port | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) { throw "server did not start - see $LogFile" }
}

# --- 4. database ----------------------------------------------------------------------------------
$exists = & (Join-Path $PgHome 'bin\psql.exe') -h localhost -p $Port -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DbName'"
if (-not $exists) {
    & (Join-Path $PgHome 'bin\createdb.exe') -h localhost -p $Port -U postgres $DbName
    if ($LASTEXITCODE -ne 0) { throw 'createdb failed' }
    Write-Host "[4/4] Database $DbName created."
} else { Write-Host "[4/4] Database $DbName already exists." }

& (Join-Path $PgHome 'bin\psql.exe') -h localhost -p $Port -U postgres -d $DbName -c "CREATE EXTENSION IF NOT EXISTS postgis;" -c "SELECT postgis_full_version();"
Write-Host ''
Write-Host "Ready. Connection: host=localhost port=$Port dbname=$DbName user=postgres (trust auth)."
Write-Host "Next: python setup_db.py"
