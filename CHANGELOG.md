# Changelog

The installed app reads the newest entry's notes from `releases/latest.json`
and shows them next to its "Update to ..." button. Keep the newest version at
the top, one `## <version> (<date>)` heading per release.

## 2.5.1 (2026-10-24)

- The login code opens the bot. Day Trader Bot and Day Trader Code now share
  one file, `%USERPROFILE%\DayTraderBot\trader\state\access.json`, even when
  the two windows were started from different folders. The code is 6 digits
  and lasts 12 hours. The Open screen has a button that opens Day Trader Code.
  FLATTEN ALL still closes everything.

## 2.5.0 (2026-10-24)

- Login with a code. Day Trader Code is a separate window (Start menu, or
  gui.py --codegen) that shows a random code for 15 minutes. Day Trader Bot
  asks for that code before it opens. The code is not a broker password.
- Currencies. The market list adds forex: majors (EURUSD, GBPUSD, USDJPY and
  the rest), crosses, and a few other pairs. The sim and MetaTrader 5 watch
  the pairs. Alpaca watches the currency ETFs (FXE, FXY, UUP).
- Change symbol is a dropdown on the Bot, Market and Trade tabs. The market
  list fills the empty space under the buttons. FLATTEN ALL still closes
  everything.

## 2.4.9 (2026-10-23)

- The window stays open when trader\state\bot.json is damaged. The installed
  app has no console, so the repair note no longer crashes the window.
  Readable trades are kept. FLATTEN ALL still closes everything.

## 2.4.8 (2026-10-23)

- A damaged session file (trader\state\bot.json) no longer stops the bot.
  The file is copied aside and the trades that can still be read are kept.
  When nothing can be read, the bot starts a fresh session. Check open
  positions at the broker before the next run. FLATTEN ALL still closes
  everything.

## 2.4.7 (2026-10-23)

- Check for updates downloads the installer from the GitHub release at
  https://github.com/positivesooch/day-alpha. A blank update address, a
  value that is only slashes, or a Windows path that is not on this PC
  uses that release list. The downloaded setup runs as that file. FLATTEN ALL
  still closes everything.

## 2.4.6 (2026-10-23)

- Trade history. The Trade tab lists live positions and every closed trade,
  with profit or loss on each row and a realised total. It keeps reading the
  session while the bot runs. FLATTEN ALL still closes everything.

## 2.4.5 (2026-10-23)

- Update stays on this copy. Check for updates, then "Update to ...", replaces
  the files already installed and opens the window again on the new version.
  Settings in the DayTraderBot folder stay. A new setup file is only for a PC
  that has never had the app. FLATTEN ALL still closes everything.

## 2.4.4 (2026-10-23)

- This build replaces the installer on the GitHub release. Check for updates
  downloads it from the positivesooch/day-alpha release. FLATTEN ALL still
  closes everything.

## 2.4.3 (2026-10-23)

- Online updates from GitHub. Check for updates reads latest.json on
  https://github.com/positivesooch/day-alpha and downloads
  DayTraderBot-Setup from that same release. An older install that still
  has the unused yoyossm address in trader\bot.json follows this release
  list instead. FLATTEN ALL still closes everything.

## 2.4.2 (2026-10-23)

- Live market chart. The Market tab draws today's candles (1 minute, 5
  minute, 15 minute, or 1 hour) and a last-price tape from Yahoo Finance,
  the same feed the sim trades on. Live refreshes every 5, 15, 30 or 60
  seconds. Changing the symbol box, a market-list button, or the Trade
  tab's symbol moves the chart onto that name. Connect draws the open
  position's entry, stop, target and mark on the candles. FLATTEN ALL
  still closes everything.

## 2.4.1 (2026-10-23)

- Stocks and commodities. The Bot tab lists every stock type (index ETFs,
  technology, communication, consumer discretionary, consumer staples,
  healthcare, financials, industrials, energy, materials, utilities, real
  estate) and commodities (metals, energy, agriculture, livestock, baskets).
  Alpaca watches the stock and the commodity ETF (GLD, USO, CORN). The sim
  and MetaTrader 5 also watch the futures (GC=F gold as XAUUSD, CL=F oil as
  USOIL, and the grains, softs, and livestock). All US common stocks turns
  the scanner onto Nasdaq Trader's list.
- Run live, auto. One button on the Bot tab arms the live account and starts
  today's session in auto mode, so the bot places its own orders on Alpaca's
  live API or the MetaTrader 5 account without a yes for each ticket. It asks
  once, and it asks you to type TRADE REAL MONEY. The sim broker stays paper
  only. FLATTEN ALL still closes everything.

## 2.4.0 (2026-10-23)

- A trading desk in the window: Connect shows balance, equity, today's P&L
  and the open positions. Place order sends one bracket (stop and target)
  through the same size, risk and ticket gates as the bot. Close selected
  sells that position. FLATTEN ALL closes every position, cancels open
  orders and halts the day.
- Broker tab: Alpaca key and secret, and a MetaTrader 5 login, server and
  password. Saved values go to trader\state\vault.json, encrypted with
  Windows DPAPI for your user (on other systems the file is readable by you
  only). An environment variable still wins, and the values are never printed.
- Settings tab: edit the budget, the loss limits, the flat-by time and the
  prop-firm percentages. Switch to a LIVE account only by typing TRADE REAL
  MONEY. Auto mode on that live account stays off until you tick it and type
  the same phrase.
- Console: `account`, `positions`, `order`, `close`, `flatten`, `resume`,
  `settings`, `live`, `creds`. The menu keys are `a` and `k`.

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
