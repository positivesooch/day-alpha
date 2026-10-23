# Releases

Built Windows installers, for people who cannot build one themselves.

| file | version | SHA-256 |
| --- | --- | --- |
| DayTraderBot-Setup-2.4.3.exe | 2.4.3 | `20def38561250bad94c034c7d6332d98b67603464a8549a60626d3eccd80d9e7` |

Download the .exe and double-click it. It is not code-signed, so SmartScreen
will say "Windows protected your PC": click "More info", then "Run anyway".
It installs for your user only (no admin rights) and needs no Python. Your
settings live in %USERPROFILE%\DayTraderBot\trader\.

`DayTraderBot-Update.cmd` is the whole updater in one file, for a PC that
has an older version (2.2.0 has no updater built in) or for a scheduled
task. Double-click it: it reads the release list, compares with the
installed version, downloads the newer installer, checks its SHA-256 and
size, and installs it silently. Options: `-Check` (only report), `-Yes` (no
question), `-DownloadOnly`, `-Url PATH-OR-URL` (another release list). It is
a plain batch file that hands its own text to Windows PowerShell; nothing
to install.

`latest.json` next to the installer is the release list the installed app's
one-click update reads (version, installer location, SHA-256, size, notes).
Serve these two files from any https URL, folder or network share and point
`update.url` in trader\bot.json at the latest.json there; from then on new
versions are one click inside the app.

The build is reproducible from this repository with installer\build.ps1
(see the README). A push to main runs that build and replaces these files.
