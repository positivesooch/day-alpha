<#
.SYNOPSIS
  Build DayTraderBot-Setup-<version>.exe on Windows.

.DESCRIPTION
  1. pip installs PyInstaller (into the current Python; a venv is fine).
  2. PyInstaller freezes trader-bot\bot.py plus the pack into dist\DayTraderBot\.
  3. Inno Setup (ISCC.exe) wraps that folder into installer\Output\DayTraderBot-Setup-<version>.exe.

  Needs: Python 3.8+ on PATH, and Inno Setup 6 (https://jrsoftware.org/isinfo.php,
  or `winget install JRSoftware.InnoSetup`, or `choco install innosetup`).

.PARAMETER SkipInstaller
  Stop after PyInstaller: dist\DayTraderBot\DayTraderBot.exe is enough to run or zip.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File installer\build.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$BotPy = Join-Path $Root "trader-bot\bot.py"
$Version = (Select-String -Path $BotPy -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
if (-not $Version) { throw "could not read VERSION from trader-bot\bot.py" }
Write-Host "Day Trader Bot $Version" -ForegroundColor Cyan

& $Python --version
& $Python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

Write-Host "`n== PyInstaller" -ForegroundColor Cyan
if (Test-Path "dist\DayTraderBot") { Remove-Item -Recurse -Force "dist\DayTraderBot" }
& $Python -m PyInstaller --noconfirm --clean "installer\DayTraderBot.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "`n== Smoke test: the frozen app replays the reel day" -ForegroundColor Cyan
$env:DAYTRADERBOT_HOME = Join-Path $env:TEMP "DayTraderBot-smoke"
if (Test-Path $env:DAYTRADERBOT_HOME) { Remove-Item -Recurse -Force $env:DAYTRADERBOT_HOME }
& "dist\DayTraderBot\DayTraderBot.exe" --version
& "dist\DayTraderBot\DayTraderBot.exe" replay --fixtures --fresh --mode auto --quiet
if ($LASTEXITCODE -ne 0) { throw "the frozen app failed its replay" }
Remove-Item Env:\DAYTRADERBOT_HOME

if ($SkipInstaller) {
    Write-Host "`nDone: dist\DayTraderBot\DayTraderBot.exe (installer skipped)" -ForegroundColor Green
    exit 0
}

Write-Host "`n== Inno Setup" -ForegroundColor Cyan
$Iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $Iscc) {
    foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                             "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
                             "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $candidate) { $Iscc = $candidate; break }
    }
}
if (-not $Iscc) {
    throw "Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) or rerun with -SkipInstaller."
}
& $Iscc "/DMyAppVersion=$Version" "installer\DayTraderBot.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$Setup = Join-Path $Root "installer\Output\DayTraderBot-Setup-$Version.exe"
Write-Host "`nDone: $Setup" -ForegroundColor Green
