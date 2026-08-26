@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo [1/7] Creating virtual environment...
if not exist .venv\Scripts\python.exe py -m venv .venv
if not exist .venv\Scripts\python.exe (
  echo Could not create the Python virtual environment.
  exit /b 1
)
set PY=.venv\Scripts\python.exe

echo [2/7] Installing desktop dependencies...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements-desktop.txt
if errorlevel 1 exit /b 1

echo [3/7] Installing bundled Chromium for hosted DNC filtering...
if exist playwright-browsers rmdir /s /q playwright-browsers
set PLAYWRIGHT_BROWSERS_PATH=%CD%\playwright-browsers
%PY% -m playwright install chromium
if errorlevel 1 exit /b 1

echo [4/7] Checking application source...
%PY% -m py_compile desktop_app.py db.py importers.py dnc_client.py
if errorlevel 1 exit /b 1

echo [5/7] Building ContactDirectory.exe...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name ContactDirectory desktop_app.py
if errorlevel 1 exit /b 1

echo [6/7] Copying the writable data directory and bundled browser...
if not exist dist\ContactDirectory\data mkdir dist\ContactDirectory\data
copy /y data\contacts.db dist\ContactDirectory\data\contacts.db >nul
copy /y data\normalized_merged_all.csv dist\ContactDirectory\data\normalized_merged_all.csv >nul
if exist README.md copy /y README.md dist\ContactDirectory\README.md >nul
if exist playwright-browsers xcopy /e /i /y playwright-browsers dist\ContactDirectory\playwright-browsers >nul

echo [7/7] Build complete.
echo   dist\ContactDirectory\ContactDirectory.exe
echo.
echo The complete dist\ContactDirectory folder is required on the target computer.
echo The optional hosted DNC pass needs an internet connection to the configured cleaner URL.
endlocal
