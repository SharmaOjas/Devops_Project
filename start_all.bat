@echo off
rem Double-click to start the database, collector and website. Run "start_all.bat stop" to stop the collector and website.
if /i "%~1"=="stop" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dsw.ps1" stop
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dsw.ps1" start
)
