@echo off
rem DayTraderBot-Update.cmd: the one-file updater for Day Trader Bot.
rem
rem Double-click it, or run it from a Command Prompt. It reads the release list
rem (latest.json), compares with the installed version, downloads the newer
rem version, checks its SHA-256 and size against the release list, and replaces
rem the files in the current install folder. Settings in
rem %USERPROFILE%\DayTraderBot are kept. Nothing is run that fails the check.
rem
rem   DayTraderBot-Update.cmd                         ask, then update this copy
rem   DayTraderBot-Update.cmd -Check                  only say whether a newer version exists
rem   DayTraderBot-Update.cmd -Yes                    update without asking
rem   DayTraderBot-Update.cmd -Url \\nas\share\latest.json   a release list somewhere else
rem
rem Where the release list lives: -Url, else the DAYTRADERBOT_UPDATE_URL
rem variable, else update.url in %USERPROFILE%\DayTraderBot\trader\bot.json,
rem else this project's GitHub releases. The logic below is PowerShell (built
rem into Windows); this .cmd wrapper only hands the file to it.
setlocal
set "DTB_ARGS=%*"
set "DTB_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f = Get-Content -LiteralPath $env:DTB_SELF -Raw; $i = $f.IndexOf('#' + 'POWERSHELL-BEGIN'); & ([scriptblock]::Create($f.Substring($i))); exit $LASTEXITCODE"
set "DTB_CODE=%ERRORLEVEL%"
echo %cmdcmdline% | find /i "%~0" >nul 2>&1 && (echo. & pause)
exit /b %DTB_CODE%

#POWERSHELL-BEGIN
param([string]$Url, [switch]$Check, [switch]$Yes, [switch]$DownloadOnly)

$ErrorActionPreference = 'Stop'
if ($env:DTB_ARGS -and -not $PSBoundParameters.Count) {
    # started by the .cmd wrapper: the raw command line, quotes respected, -Flag or /Flag
    $toks = @([regex]::Matches($env:DTB_ARGS, '"([^"]*)"|(\S+)') | ForEach-Object { if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value } })
    for ($i = 0; $i -lt $toks.Count; $i++) {
        switch -regex ($toks[$i]) {
            '^[-/]check$'        { $Check = $true }
            '^[-/]yes$'          { $Yes = $true }
            '^[-/]download-?only$' { $DownloadOnly = $true }
            '^[-/]url$'          { $i++; $Url = $toks[$i] }
            default              { Write-Host "DayTraderBot-Update: unknown option $($toks[$i]) (use -Check, -Yes, -DownloadOnly, -Url PATH)"; exit 2 }
        }
    }
}
$AppKey = '{7D2F3C7A-5B0E-4E2B-9B1A-0DA17BAD3B07}_is1'   # the installer's AppId; an upgrade keeps it
$DefaultUrl = 'https://github.com/positivesooch/day-alpha/releases/latest/download/latest.json'

function Get-Installed {
    foreach ($hive in 'HKCU:', 'HKLM:') {
        foreach ($k in "$hive\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppKey",
                       "$hive\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$AppKey") {
            try {
                if (Test-Path -LiteralPath $k) {
                    $p = Get-ItemProperty -LiteralPath $k
                    return @{ Version = [string]$p.DisplayVersion; Dir = [string]$p.InstallLocation }
                }
            } catch {}
        }
    }
    return $null
}

function Get-Text($loc) {
    if ($loc -match '^https?://') {
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
        return (Invoke-WebRequest -UseBasicParsing -Uri $loc -Headers @{ 'User-Agent' = 'DayTraderBot-Update.cmd' }).Content
    }
    return Get-Content -Raw -LiteralPath $loc
}

function Get-File($loc, $dest) {
    if ($loc -match '^https?://') {
        Invoke-WebRequest -UseBasicParsing -Uri $loc -OutFile $dest -Headers @{ 'User-Agent' = 'DayTraderBot-Update.cmd' }
    } else {
        Copy-Item -LiteralPath $loc -Destination $dest -Force
    }
}

function Resolve-Loc($base, $ref) {
    if ($ref -match '^https?://' -or $ref -match '^[A-Za-z]:[\\/]' -or $ref.StartsWith('\\') -or $ref.StartsWith('/')) { return $ref }
    if ($base -match '^https?://') { return ([Uri]::new([Uri]$base, $ref)).AbsoluteUri }
    return Join-Path (Split-Path -Parent (Resolve-Path -LiteralPath $base)) $ref
}

function As-Version($text) { return [version](($text -replace '^v', '') -replace '[^0-9.].*$', '') }

try {
    $inst = Get-Installed
    if (-not $Url) { $Url = $env:DAYTRADERBOT_UPDATE_URL }
    if (-not $Url -and $env:USERPROFILE) {
        $cfg = Join-Path $env:USERPROFILE 'DayTraderBot\trader\bot.json'
        if (Test-Path $cfg) {
            try { $j = Get-Content -Raw -LiteralPath $cfg | ConvertFrom-Json; if ($j.update.url) { $Url = [string]$j.update.url } } catch {}
        }
    }
    if (-not $Url) { $Url = $DefaultUrl }

    $m = Get-Text $Url | ConvertFrom-Json
    if (-not $m.version -or -not $m.installer -or -not $m.installer.url) { throw "the release list at $Url has no version and installer url" }
    $latest = As-Version $m.version
    $have = if ($inst -and $inst.Version) { As-Version $inst.Version } else { [version]'0.0' }
    $haveText = if ($inst -and $inst.Version) { $inst.Version } else { 'nothing yet' }
    Write-Host "Installed: Day Trader Bot $haveText"
    Write-Host "Release list: $($m.version) ($Url)"
    if ($latest -le $have) { Write-Host "Day Trader Bot $($inst.Version) is the newest version."; exit 0 }

    Write-Host ""
    Write-Host "Day Trader Bot $($m.version) is available$(if ($m.published) { ', published ' + $m.published })."
    if ($m.notes) { Write-Host ""; Write-Host $m.notes; Write-Host "" }
    if ($Check) { Write-Host "Run this file again without -Check to update this copy."; exit 0 }
    if (-not $Yes) {
        $r = Read-Host "Update to $($m.version) now? (yes/no)"
        if ($r -notmatch '^(y|yes)$') { Write-Host "Not now. Nothing was downloaded."; exit 0 }
    }

    $loc = Resolve-Loc $Url ([string]$m.installer.url)
    $name = if ($m.installer.file) { [string]$m.installer.file } else { Split-Path -Leaf ($loc -replace '[?#].*$', '') }
    if ($name -notmatch '^[\w.-]+\.exe$') { throw "the release list names an installer that is not an .exe: $name" }
    $tmp = if ($env:TEMP) { $env:TEMP } else { [IO.Path]::GetTempPath() }
    $dir = Join-Path $tmp 'DayTraderBot-update'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $path = Join-Path $dir $name
    Write-Host "Downloading $loc ..."
    Get-File $loc $path
    $size = (Get-Item -LiteralPath $path).Length
    if ($m.installer.size -and [int64]$m.installer.size -ne $size) {
        Remove-Item -LiteralPath $path -Force
        throw "the download is $size bytes; the release list says $($m.installer.size). Not installed."
    }
    if (-not $m.installer.sha256) { Remove-Item -LiteralPath $path -Force; throw "the release list has no sha256; refusing to run an unverified file" }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower()
    if ($hash -ne ([string]$m.installer.sha256).ToLower()) {
        Remove-Item -LiteralPath $path -Force
        throw "the download's SHA-256 does not match the release list (got $($hash.Substring(0,16))..., want $(([string]$m.installer.sha256).Substring(0,16))...). Not installed."
    }
    Write-Host "Verified $name ($size bytes, SHA-256 matches the release list)."
    if ($DownloadOnly) { Write-Host "Saved to $path."; exit 0 }

    $running = Get-Process -Name 'DayTraderBot', 'DayTraderBotCLI' -ErrorAction SilentlyContinue
    if ($running) { throw "Day Trader Bot is running. Close it first (open positions stay at the broker with their bracket orders), then run this again." }
    Write-Host "Updating Day Trader Bot to $($m.version)..."
    $setupArgs = @('/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES', '/CLOSEAPPLICATIONS')
    if ($inst -and $inst.Dir) { $setupArgs += ('/DIR=' + $inst.Dir) }
    $p = Start-Process -FilePath $path -ArgumentList $setupArgs -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "the update returned exit code $($p.ExitCode)" }
    $after = Get-Installed
    Write-Host "Done: Day Trader Bot $(if ($after) { $after.Version } else { $m.version }) is ready$(if ($after -and $after.Dir) { ' in ' + $after.Dir }). Open it from the Start menu."
    exit 0
} catch {
    Write-Host ""
    Write-Host "Update failed: $($_.Exception.Message)"
    exit 1
}
