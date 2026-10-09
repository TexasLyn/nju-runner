# NJU Runner - Windows self-test launcher
#
#   powershell -ExecutionPolicy Bypass -File tools\windows-selftest.ps1
#
# ASCII-only on purpose: PowerShell 5.1 decodes .ps1 files without a BOM using
# the legacy codepage, which turns non-ASCII text into mojibake.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host ""
Write-Host "NJU Runner - Windows self-test" -ForegroundColor Cyan
Write-Host ("working dir: {0}" -f $root)
Write-Host ""

function Test-Python {
    param([string]$Exe, [string]$Pre)
    $global:LASTEXITCODE = 0
    try {
        if ($Pre) { & $Exe $Pre -c "import numpy" *> $null }
        else      { & $Exe -c "import numpy" *> $null }
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$pyExe = $null
$pyPre = ""
if (Test-Python "py" "-3")      { $pyExe = "py";      $pyPre = "-3" }
elseif (Test-Python "python" "")  { $pyExe = "python";  $pyPre = "" }
elseif (Test-Python "python3" "") { $pyExe = "python3"; $pyPre = "" }

if (-not $pyExe) {
    Write-Host "[X] No Python with numpy found." -ForegroundColor Red
    Write-Host ""
    Write-Host "    Install Python 3.11+ (ARM64 build on Windows on ARM) from"
    Write-Host "      https://www.python.org/downloads/windows/"
    Write-Host "    then run:"
    Write-Host "      pip install numpy"
    Write-Host "      pip install pymobiledevice3    (optional, real device only)"
    exit 1
}

Write-Host ("[i] using: {0} {1}" -f $pyExe, $pyPre)
Write-Host ""

if ($pyPre) { & $pyExe $pyPre "tools/selftest.py" }
else        { & $pyExe        "tools/selftest.py" }
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "SELF-TEST PASSED" -ForegroundColor Green
} else {
    Write-Host "SELF-TEST FAILED (exit $code)" -ForegroundColor Red
}
Write-Host ""
Write-Host "Next step - start the web UI:  .\start.bat" -ForegroundColor Cyan
exit $code
