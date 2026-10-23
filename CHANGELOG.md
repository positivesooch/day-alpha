# Changelog

The installed app reads the newest entry's notes from `releases/latest.json`
and shows them next to its "Update to ..." button. Keep the newest version at
the top, one `## <version> (<date>)` heading per release.

## 2.3.0 (2026-10-23)

- One-click updates. The window checks the release list when it opens and
  shows "Update to <version>" at the top right when there is a newer one; one
  click downloads the installer, checks its SHA-256 and size against the
  release list, installs it over the current version silently and reopens
  the window. `DayTraderBotCLI update` does the same from the console, and
  the menu has it as `u`. `update.url` in `trader\bot.json` says where the
  release list lives (an https URL, a folder or a network share); nothing is
  downloaded or run without your click.
- `releases\DayTraderBot-Update.cmd`: the updater as one double-clickable
  file (batch outside, Windows PowerShell inside) for PCs on 2.2.0, which has
  no updater built in, and for scheduled tasks. `-Check`, `-Yes`,
  `-DownloadOnly`, `-Url`.
- `installer\make_manifest.py` writes `latest.json` for a built installer;
  `build.ps1` and the Windows workflow call it, and the workflow attaches it
  to the release next to the installer.

## 2.2.0 (2026-10-22)

- MetaTrader 5 as a broker, for funded prop-firm accounts: the firm's own
  candles feed the tools, shares become lots sized to the plan's max loss,
  one market deal carries the stop and the target, the firm's daily loss and
  drawdown limits gate every order with a buffer, and an emergency flatten
  closes what the bot opened when equity nears a limit. `MT5_PASSWORD` in the
  environment, never in a file.
- The Windows build bundles the `MetaTrader5` package.

## 2.1.0 (2026-10-22)

- The Windows app: `DayTraderBot.exe` (the window) and `DayTraderBotCLI.exe`
  (the console menu and command line), one installer, no Python needed.

## 2.0.0 (2026-10-21)

- trader-bot: the pack's tools as one loop, bar by bar, with a built-in paper
  simulator, Alpaca's REST API, three modes (dry-run, confirm, auto on paper
  only) and offline replays of recorded sessions.
