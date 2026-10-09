@echo off
REM NJU Runner - Windows launcher
REM Usage:  start.bat          (default port 8788)
REM         start.bat 9000     (custom port)
REM
REM Console messages are ASCII on purpose: .bat files get decoded with the OEM
REM codepage, so non-ASCII text turns into mojibake on some systems.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PORT=%1
if "%PORT%"=="" set PORT=8788

REM ---- Pick a Python that has numpy.
REM      Note: "if cond cmd && set VAR=x" does NOT behave like you expect in
REM      cmd.exe, so every step is an explicit block and errorlevel test.
set PYEXE=

py -3 -c "import numpy" >nul 2>&1
if not errorlevel 1 set PYEXE=py -3

if not defined PYEXE (
  python -c "import numpy" >nul 2>&1
  if not errorlevel 1 set PYEXE=python
)

if not defined PYEXE (
  python3 -c "import numpy" >nul 2>&1
  if not errorlevel 1 set PYEXE=python3
)

if not defined PYEXE (
  echo.
  echo   [X] No Python with numpy found.
  echo.
  echo   Install Python 3.11+ from https://www.python.org/downloads/windows/
  echo   ^(use the ARM64 installer if this is Windows on ARM^), then run:
  echo.
  echo       pip install numpy
  echo.
  echo   Optional, only needed for real-device location injection:
  echo       pip install pymobiledevice3
  echo.
  pause
  exit /b 1
)

echo.
echo   NJU Runner - starting with "%PYEXE%"
echo   Serving at http://127.0.0.1:%PORT%
echo   Browser opens in ~3s.  Press Ctrl+C here to stop.
echo.

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:%PORT%'"

%PYEXE% server.py %PORT%

echo.
echo   Server stopped.
pause
