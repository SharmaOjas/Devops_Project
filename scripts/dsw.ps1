<#
  Start or stop Dark Ship Watch.
    powershell -ExecutionPolicy Bypass -File scripts\dsw.ps1 start      database, tables, collector and website
    powershell -ExecutionPolicy Bypass -File scripts\dsw.ps1 stop       stop the collector and website (the database keeps running)
    powershell -ExecutionPolicy Bypass -File scripts\dsw.ps1 db-start   only start the portable database
    powershell -ExecutionPolicy Bypass -File scripts\dsw.ps1 db-stop    stop the portable database (data is safe)
  Add -UseExistingDb to "start" if you run your own PostgreSQL (set the DSW_DB_* environment variables first).
#>
param(
    [ValidateSet('start', 'stop', 'db-start', 'db-stop')][string]$Action = 'start',
    [switch]$UseExistingDb, [switch]$NoBrowser, [int]$Port = 5433)
$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$Root = Join-Path $Project '.pgportable'
$Bin = Join-Path $Root 'pg\bin'

function Start-Db {
    if (-not (Test-Path (Join-Path $Bin 'pg_ctl.exe'))) { throw "Portable database not found - run scripts\setup_portable_db.ps1 first." }
    & (Join-Path $Bin 'pg_isready.exe') -h localhost -p $Port | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "PostgreSQL already running on port $Port."; return }
    $env:GDAL_DATA = Join-Path $Root 'pg\gdal-data'
    # pg_ctl hangs if its output is piped, so it is started as its own hidden process
    Start-Process -FilePath (Join-Path $Bin 'pg_ctl.exe') -ArgumentList @('-D', "`"$(Join-Path $Root 'data')`"", '-l', "`"$(Join-Path $Root 'postgres.log')`"", 'start') -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        & (Join-Path $Bin 'pg_isready.exe') -h localhost -p $Port | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host "PostgreSQL started on port $Port."; return }
    }
    throw "Server did not start - see $(Join-Path $Root 'postgres.log')"
}

switch ($Action) {
    'db-start' { Start-Db }
    'db-stop' { & (Join-Path $Bin 'pg_ctl.exe') -D (Join-Path $Root 'data') -m fast stop }
    'stop' {
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -match 'collector\.py|app\.py' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped $($_.ProcessId): $($_.CommandLine)" }
    }
    'start' {
        Set-Location $Project
        New-Item -ItemType Directory -Force logs | Out-Null
        if (-not $UseExistingDb) { Start-Db }
        python setup_db.py
        if ($LASTEXITCODE -ne 0) { throw 'setup_db.py failed' }
        Start-Process python -ArgumentList 'collector.py' -WorkingDirectory $Project -WindowStyle Minimized -RedirectStandardOutput logs\collector.log -RedirectStandardError logs\collector.err
        Start-Process python -ArgumentList 'app.py' -WorkingDirectory $Project -WindowStyle Minimized -RedirectStandardOutput logs\app.log -RedirectStandardError logs\app.err
        Start-Sleep -Seconds 4
        Write-Host 'Collector and website started (logs in .\logs).  Open http://127.0.0.1:8000'
        if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8000' }
    }
}
