# The Day Trader skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, Nasdaq Trader's symbol lists, SEC EDGAR
and the House Clerk.

One scans thousands of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a dollar, it replays the
rest of the session 10,000 times from the stock's own recent bars, plus every
past day the same setup fired, and tells you the honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

This fork adds a twelfth piece, **`/trader-bot`**: the eleven skills run as
one loop, bar by bar, with a built-in paper broker, an Alpaca connection, and
a replay that runs a recorded session in a second. Dry-run by default, it
asks you for a yes in confirm mode, and it trades hands-off only on a paper
account. See [The bot](#the-bot) below.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Jakeschincariol/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. The SEC tools
(news filings, the analyst) ask you to identify yourself: put your name and
email in `trader/config.json` (`"sec_user_agent": "Your Name you@example.com"`)
or in the `SEC_USER_AGENT` environment variable.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans thousands of charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks breaking news on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a dollar: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to your broker, paper trading by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads earnings reports and sums up what changed. |
| `/trader-pelosi` | Pelosi Tracker | Reads Congress's stock-trade disclosures the minute they are filed. |
| `/trader-bot` | Trading Bot | Runs the pipeline above as one loop: scan, time the entry, plan the exit, replay the odds, size, check, place. Paper by default. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                        (on a schedule)
```

An example, the trade in the reel: Tesla, Monday October 19 2026, real data.

1. **`/trader-scanner`** at 10:15 AM ET: of the 231 charts in the reel's
   scan, 42 had closed above their 15-minute opening range. TSLA is one: range 354.47 to 362.18,
   the 10:10 candle closed at 364.18, 0.55 % over the top.
2. **`/trader-news`**: the morning's TSLA headlines, in New York time
   ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", 8:04,
   tagged earnings and deal). Tesla reports this week: a day trade is out
   before the close, so it is not exposed to the report.
3. **`/trader-entry`** watched TSLA bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 364.18.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 358.32, so 5.86 a share at risk; target one times that risk,
   370.04; flat by 3:55 PM either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TSLA's own candles at that time of day over the
   previous 59 sessions (seed 19, so anyone can repeat it): target first
   29.4 %, stop first 25.5 %, still open at the close 45.1 %. It also re-ran
   the same rule on those 59 real sessions: it fired 33 times, 13 hit the
   target, 15 the stop, 5 neither. Its verdict: close to a coin flip. That is
   the point of looking before you risk a dollar.
6. **`/trader-budget`** with a $5,000 account and a $50 max loss per trade:
   50 / 5.86 = 8 shares ($2,913.44). If the stop fills at 358.32, the loss is
   $46.88. It writes one bracket order: buy 8 TSLA, stop-loss 358.32,
   take-profit 370.04.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size and cash. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. Claude calls your brokerage connector (Alpaca's MCP
   server, paper account by default), and Claude's own "Allow once / Deny"
   card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target and at 3:55 PM to
   flatten whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # ~5,900 US stocks, Nasdaq Trader
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-news/news.py --tickers TSLA --hours 12                  # headlines, 8-Ks, today's move
python3 trader-entry/entry.py check TSLA --setup orb                   # waiting / triggered / expired
python3 trader-entry/entry.py watch TSLA --every 30                    # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py TSLA                                   # quarters, growth, EPS
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: TSLA's 5-minute bars
for the 60 days to October 19 2026, the 10:15 AM opening-range scan, Yahoo's
TSLA headlines that morning, the House Clerk's 2026 index, real PTR text and
real XBRL facts. They reproduce every number in the example above.

## The bot

`/trader-bot` is the pipeline above with a clock, a broker and a log. It
calls the same scripts, with `--json`, in the same order, every bar:

```bash
python3 trader-bot/bot.py init                                 # trader/config.json + trader/bot.json
python3 trader-bot/bot.py replay --fixtures                    # the reel's day, offline, dry-run
python3 trader-bot/bot.py replay --fixtures --mode auto        # same day, the sim broker takes the trade
python3 trader-bot/bot.py replay --fixtures --scan --mode auto # the scanner picks from 231 recorded charts
python3 trader-bot/bot.py run --symbols TSLA,AAPL              # today, live, dry-run: nothing is placed
python3 trader-bot/bot.py run --mode confirm                   # live; you type yes or no per order
python3 trader-bot/bot.py run --mode auto --broker alpaca      # Alpaca PAPER account, hands off
python3 trader-bot/bot.py status --events 20                   # the session so far
```

What the replay of the reel prints, in auto mode:

```
10:15  TSLA: TRIGGERED 10:15 AM at $364.18 (10:10 bar closed above $362.18, +0.55%)
10:15  TSLA: news: 16 headlines, tagged deal, earnings, macro
10:15  TSLA: plan: entry $364.18, stop $358.32 (opening-range midpoint), target $370.04 (1R), $5.86 a share at risk, flat by 15:55
10:15  TSLA: time machine: target first 29.4%, stop first 25.5%, open at the close 45.1% (10000 replays); history: 33 of 59 fired, 13 target / 15 stop / 5 close. Close to a coin flip.
10:15  TSLA: budget: 8 shares ($2,913.44), stop-loss 358.32, take-profit 370.04, max loss $46.88
10:15  TSLA: risk: PASS. max loss $46.88 (limit $50), reward:risk 1.00 (min 1), 0 of 3 trades today
10:15  TSLA: READY: BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 [PAPER account, sim]
10:15  TSLA: auto mode on a PAPER account: the bot says yes to this one order
10:15  TSLA: FILLED buy 8 at $364.18 (sim-1), bracket at the broker: stop-loss $358.32, take-profit $370.04
```

**Three modes.** `dry-run` (the default) runs everything and places nothing.
`confirm` shows the ticket and asks you to type yes for that one order;
`ticket.py approve` decides whether it was a real yes. `auto` says yes
itself, writes "nobody typed this" into the broker log, and works only while
`trader/config.json` says `"paper": true`. On a live account it refuses to
start unless `execution.allow_auto_on_live` was armed on purpose.

**Stocks and commodities.** The window's market list is every stock type
(index ETFs, technology, communication, consumer discretionary and staples,
healthcare, financials, industrials, energy, materials, utilities, real
estate) and commodities (metals, energy, agriculture, livestock, and the
broad baskets). Alpaca trades the stocks and the commodity ETFs (`GLD`,
`USO`, `CORN`). The sim and MetaTrader 5 also take the futures (`GC=F` as
`XAUUSD`, `CL=F` as `USOIL`, and the grains, softs, and livestock). "All US
common stocks" downloads Nasdaq Trader's common-stock list and lets the
scanner look through it. The loss limits still cap how many trades a day.

**Live chart.** The Market tab polls Yahoo Finance (the same unofficial
feed the sim uses for today's bars) and draws candles for the symbol you
are watching. Pick 1-minute, 5-minute, 15-minute or 1-hour candles. Live
is on by default and refreshes every 15 seconds; turn it off or press
Refresh for one pull. The tape under the chart shows the last price and
the day's change for up to eight symbols from the Bot tab. Editing those
symbols, choosing a stock type or commodity, or typing a symbol on the
Trade tab moves the chart. After Connect, an open position's entry, stop,
target and mark are drawn across the candles. The chart does not place
orders.

**Three brokers.** `sim` is a paper broker inside the bot that fills at the
latest price and walks the bracket bar by bar (when one bar touches both the
stop and the target, the stop wins). `alpaca` talks to Alpaca's REST API with
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (or keys saved from the window's
Broker tab), paper unless your config says otherwise. `mt5` drives a
MetaTrader 5 terminal, which is how funded prop-firm accounts are traded;
see the next section.

**Gates in `trader/bot.json`.** A trigger older than `stale_minutes` is a
different trade and is skipped. News tags in `block_tags` (say,
`["earnings"]`) skip the trade. The Time Machine's edge (target first minus
stop first) must reach `min_edge_pct`, and the setup must have fired at least
`min_history_triggers` times. Then the budget, trader-risk and the ticket
decide as they always do. Every skip, block and fill is in
`trader/state/bot.json`, and `bot.py status` reads it back.

Everything in "Fine print" applies twice over to a bot. Replay first, then
dry-run, then confirm, then auto on paper. `"paper"` flips to false only
through `bot.py live --on` after you type `TRADE REAL MONEY`.

### A prop-firm account on MetaTrader 5

Funded prop firms (FTMO, FundedNext, The5ers, E8 and the like) give you a
MetaTrader 5 login, a daily loss limit and a maximum drawdown, and end the
account the moment either is breached. `--broker mt5` runs the same pipeline
on that account, through MetaQuotes' own `MetaTrader5` Python package and
the MT5 terminal installed on the same Windows machine:

```bash
python3 trader-bot/bot.py run --mode confirm --broker mt5     # you type yes per order
python3 trader-bot/bot.py run --mode auto --broker mt5        # hands off, prop rules enforced
```

What changes with `mt5` (all of it is in `trader-bot/mt5link.py`):

- **The firm's candles feed the tools.** The bot reads the symbol's M5 rates
  from the terminal, converts server time to UTC, and hands them to
  trader-entry, trader-exit and the Time Machine in the chart shape they
  already read. So the opening range, the trigger, the stop and the odds
  come from the prices you will actually be filled at, on CFD symbols Yahoo
  has never heard of. `"use_bars": false` keeps Yahoo for the analysis and
  uses the terminal only to trade.
- **Symbols are mapped.** `"symbols": {"TSLA": "TSLA.US"}` names the firm's
  symbol for each ticker; `"symbol_suffix": ".US"` covers the rest.
- **Shares become lots.** The budget's `qty` is divided by the symbol's
  contract size, rounded to the volume step, and capped so the loss at the
  stop (tick value times ticks to the stop) fits the plan's max loss. If
  even the smallest lot risks more than that, the order is refused, not
  rounded up.
- **One deal, stop and target on the position.** The fill is a market deal
  with SL and TP set on the position itself, so the terminal closes it even
  if the bot's machine goes dark. The bot reads the close from the deal
  history, including the firm's commission, and still flattens at
  `flat_by` (15:55) if neither side is hit.
- **The firm's rules are a hard gate.** `"prop"` in `bot.json` holds the
  account size, `max_daily_loss_pct`, `max_total_drawdown_pct` and a
  `buffer_pct` (default 20%: the bot treats 80% of each limit as the line).
  Before every order the live equity is measured against today's starting
  balance (at the firm's reset time, in server time) and against the account
  size, and a trade whose loss at the stop would not fit the room left is
  blocked, after trader-risk has had its say. If equity ever crosses the
  buffered line with positions open, the bot closes everything it opened
  (its own magic number only; your manual positions are left alone) and
  takes no more trades that day.
- **The account's kind is checked against your config.** Challenge and
  funded logins report themselves as demo accounts; `"paper": true` is
  only accepted for those. A login that reports a REAL account refuses to
  start under `"paper": true`, runs in confirm mode under `"paper": false`,
  and takes auto mode only with `"allow_auto_on_real": true` set on
  purpose in `bot.json`.

The password is read from `MT5_PASSWORD`, or from the Broker tab, which
stores it in `trader/state/vault.json` (Windows DPAPI for your user; never
printed, never committed). Leave `"login"` empty to use
whatever account the terminal is already logged in to. `"server_utc_offset_hours"`
pins the server clock's offset; left empty, the bot measures it from a live
tick. The scanner still runs on Yahoo's US-equity data; the symbols it finds
are then traded on the firm's matching CFDs.

A funded account is not a paper account, whatever MT5 calls it: a breached
rule ends it. Replay, then dry-run, then confirm, then auto on the
challenge account, before any funded login sees this bot. The `MetaTrader5`
package exists for Windows only; on other systems `--broker mt5` says so and
stops.

### The Windows app

The bot also ships as a Windows program, `DayTraderBot-Setup-<version>.exe`,
that needs no Python on the machine. The installer puts two programs and the
whole pack in Program Files (or your user folder if you choose "for me
only"), adds Start menu entries (the window, the console menu, "replay the
reel day", "run today (dry-run)", your data folder, the README), an optional
desktop icon and an optional PATH entry. Your settings and logs live in
`%USERPROFILE%\DayTraderBot\trader\`, and uninstalling leaves them alone.

**`Day Trader Code` is a separate window.** The Start menu item, or
`python3 trader-bot/gui.py --codegen`, shows a random 6-digit code for 12
hours. Day Trader Bot asks for that code before the desk opens. It is not a
broker password. Both windows share
`%USERPROFILE%\DayTraderBot\trader\state\access.json`.

**`DayTraderBot.exe` is the window.** Type the code from Day Trader Code, then
type the symbols or pick one from Change symbol. Pick the mode
(dry-run, confirm, auto) and the broker (sim, alpaca, mt5), tick the scanner if
you want it, then Replay the reel day, Run today, or Status. The log pane
streams what the bot does, green for PASS and fills, red for BLOCK and
stops. In confirm mode the ticket appears in the log and the Reply box
lights up: type yes or no and press Enter, and that exact reply goes to
`ticket.py approve`, which still refuses anything but a clear yes for that
one order. Stop ends the run; open positions stay at the broker with their
bracket orders.

**`DayTraderBotCLI.exe` is the console program.** Double-clicked, it opens
a numbered menu with the same choices. From a Command Prompt it takes the
same commands as `bot.py`:

```
DayTraderBotCLI replay --fixtures --mode auto
DayTraderBotCLI run --symbols TSLA,AAPL --mode confirm
DayTraderBotCLI status --events 20
```

The window never trades by itself: every button starts `DayTraderBotCLI`
with the arguments you would have typed. Without the installer, the same
window is `python3 trader-bot/bot.py gui` (Debian/Ubuntu need `python3-tk`).

**Updates are one click.** When the window opens it reads a small release
list (`latest.json`) in the background; if a newer version is out, the
top-right button says "Update to 2.4.0" and the log shows the notes from
`CHANGELOG.md`. One click updates the copy already on this PC: it downloads
the new version, checks its SHA-256 and size, replaces the files in the same
folder, and reopens the window. Settings stay. A setup file is for a PC that
has never had the app.
`DayTraderBotCLI update` does the same from the console (`--check` only
asks, `--yes` skips the question, `--download-only` just saves the verified
installer), and the console menu has it as `u`. Nothing is downloaded or
run without that click; the check itself sends nothing but the request.

Where the release list lives is `update.url` in `trader\bot.json`: an
https URL (the default is
`https://github.com/positivesooch/day-alpha/releases/latest/download/latest.json`,
where each release publishes `latest.json` next to the installer), or a
folder or network share for a team that distributes the installer itself.
A blank address, a value that is only slashes (`//`), or a Windows path
that is not on this PC uses that GitHub release list.
A push to `main` on that GitHub repository builds the installer again and
replaces the release files, so the download is the newest build. The in-app
button offers it when the version number goes up (the workflow raises the
patch number when a push leaves it unchanged).
`DAYTRADERBOT_UPDATE_URL` overrides it; `"check_on_start": false` turns the
automatic check off. `releases\DayTraderBot-Update.cmd` is the same updater as one
double-clickable file (batch outside, Windows PowerShell inside), for a PC
whose installed version predates the button or for a scheduled task:
`-Check`, `-Yes`, `-DownloadOnly`, `-Url`. Every build writes `latest.json` with
`installer\make_manifest.py` (its notes come from the version's section in
`CHANGELOG.md`). A push to `main` publishes that build: the workflow raises
the patch number when `VERSION` was left unchanged, then replaces the
installer, `latest.json`, and the updater on the GitHub release.

Build it yourself on Windows with Python 3.8+ and
[Inno Setup 6](https://jrsoftware.org/isinfo.php) installed:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

That runs PyInstaller on `installer\DayTraderBot.spec` (one folder,
`dist\DayTraderBot\`, with the window and the console exe sharing it, and
the console exe doubling as the Python that runs the tool scripts),
smoke-tests the frozen app on the reel day, then compiles
`installer\DayTraderBot.iss` into `installer\Output\DayTraderBot-Setup-<version>.exe`.
`-SkipInstaller` stops after the exe. The GitHub Actions workflow in
`.github/workflows/windows-installer.yml` does the same on a Windows runner
for every `v*` tag and attaches the installer to the release.

For Alpaca, set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in Windows
(Settings > System > About > Advanced system settings > Environment
Variables), or type them once on the Broker tab. For a prop firm on
MetaTrader 5, install the firm's MT5 terminal, log it in, and either set
`MT5_PASSWORD` or save the login on the Broker tab (leave `"login"` empty
in `bot.json` to use the terminal's current login). Pick the `mt5` broker;
the build script bundles the `MetaTrader5` package so the installed app
needs nothing else. The Trade tab connects, shows the account and places or
closes a position. FLATTEN ALL is the kill switch. Settings switches paper
to a live account only after you type `TRADE REAL MONEY`. **Run live, auto**
does both steps and starts today's session in auto mode on Alpaca's live API
or MetaTrader 5: the bot places orders without a yes for each one. The sim
broker stays paper only. Picking auto mode with `mt5` asks you once more
before it starts.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`, and
Alpaca's MCP server defaults to its paper account. Keys live in environment
variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: an earnings report, a halt, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Day-trading rules.** FINRA removed the $25,000 pattern-day-trader minimum
on June 4, 2026 (Regulatory Notice 26-10), and brokers may phase the change
in until October 20, 2027, so yours may still apply the old rule. If it does,
set `"pdt_guard": true` and `/trader-risk` counts your day trades.

**Congress's trades are public, not inside information.** Members must disclose
trades over $1,000 within 45 days (the STOCK Act). The Pelosi skill reads those
public filings the minute they post.

**Taxes.** Day-trading gains are short-term gains, and frequent trading makes
wash sales likely. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
trader-bot/         SKILL.md, bot.py, bot.example.json
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai).
