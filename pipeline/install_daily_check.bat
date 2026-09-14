@echo off
rem Registers a Windows scheduled task that checks every day at 09:30 whether Foursquare,
rem Overture or OpenStreetMap published a newer release, and shows a popup if so.
rem Run once (double-click). Remove with:  schtasks /Delete /TN "Leads Explorer data check" /F
cd /d "%~dp0"
set PY=python
where pythonw >nul 2>&1 && set PY=pythonw
schtasks /Create /F /SC DAILY /ST 09:30 /TN "Leads Explorer data check" ^
  /TR "\"%PY%\" \"%CD%\update.py\" --check --notify"
if errorlevel 1 (
  echo Could not create the scheduled task.
) else (
  echo Daily check installed. It runs at 09:30 and shows a popup when new data is available.
)
pause
