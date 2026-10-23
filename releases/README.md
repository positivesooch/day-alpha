# Releases

Built Windows installers, for people who cannot build one themselves.

| file | version | SHA-256 |
| --- | --- | --- |
| DayTraderBot-Setup-2.2.0.exe | 2.2.0 | `2f03f9d61643eb8e6d75839582923a34b8f5021bc3886d14bbba96bf1e2978a3` |

Download the .exe and double-click it. It is not code-signed, so SmartScreen
will say "Windows protected your PC": click "More info", then "Run anyway".
It installs for your user only (no admin rights) and needs no Python. Your
settings live in %USERPROFILE%\DayTraderBot\trader\.

The build is reproducible from this repository with installer\build.ps1
(see the README), or by the Windows installer workflow on a tag.
