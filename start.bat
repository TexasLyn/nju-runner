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
  echo   ---- what this machine actually has ----
  echo.
  echo   [py launcher]   py -0p
  py -0p 2>&1
  echo.
  echo   [python on PATH]   where python
  where python 2>&1
  echo.
  echo   [python3 on PATH]  where python3
  where python3 2>&1
  echo.
  echo   ---- how to fix ----
  echo.
  echo   A. no real Python listed above - install the x64 build:
  echo.
  echo        winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements
  echo.
  echo      note: that picks the ARM64 build on Windows on ARM, which is enough
  echo         to draw and export, but NOT enough for real-device injection.
  echo         For that, use the x64 installer - see README, "Windows".
  echo.
  echo      Then OPEN A NEW terminal - PATH only refreshes in a new one - and run:
  echo.
  echo        py -3.13 -m pip install numpy
  echo.
  echo   B. a Python is listed above but numpy is missing:
  echo.
  echo        py -3.13 -m pip install numpy
  echo.
  echo   C. if "python" resolves to ...\WindowsApps\python.exe, that is the
  echo      Microsoft Store stub, not a real Python. Turn the two Python
  echo      app execution aliases off in Settings, Apps, Advanced app
  echo      settings, App execution aliases - then open a new terminal.
  echo.
  echo   Optional, only needed for real-device location injection:
  echo        py -3.13 -m pip install pymobiledevice3
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
