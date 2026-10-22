---
name: trader-bot
description: >-
  Trading Bot. Runs the whole day-trading pack as one loop, bar by bar: the
  scanner finds opening-range breakouts (or you name the tickers), the entry
  timer calls the trigger, the exit planner sets the stop and the target, the
  Time Machine replays the odds, the budget sizes the order, the risk manager
  passes or blocks it, and a broker places it: a built-in paper simulator, or
  Alpaca's REST API. Three modes: dry-run stops at the READY ticket (default),
  confirm asks you to type yes for each order, auto says yes itself on PAPER
  accounts only and refuses on a live one. Replays any recorded session
  offline in a second, including the reel's TSLA day from the test fixtures.
  Watches open positions, records fills, and is flat by the close. Use when
  the user says "run the bot", "trade this for me", "automate the pipeline",
  "paper trade today", "watch these tickers and take the breakout", "backtest
  the bot on yesterday", "replay the session", or "start the trading bot".
argument-hint: "[run|replay|once|status|init] [--mode dry-run|confirm|auto] [--symbols TSLA,AAPL]"
---

# trader-bot

The Trading Bot is the pack's eleven skills in a loop. It adds nothing to
the method: every number it acts on comes out of the skill scripts
themselves, called with `--json`. What it adds is a clock that wakes on
each bar, a broker that can place the ticket, a replay that runs a recorded
day in a second, and a log of every decision. Most people who day trade lose
money, and a bot does not change that: it only makes the method repeatable.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/bot.py" init                                  # trader/config.json + trader/bot.json
python3 "${CLAUDE_SKILL_DIR}/bot.py" replay --fixtures                     # the reel's day, offline, dry-run
python3 "${CLAUDE_SKILL_DIR}/bot.py" replay --fixtures --mode auto         # same day, the sim broker takes the trade
python3 "${CLAUDE_SKILL_DIR}/bot.py" replay --fixtures --scan --mode auto  # let the scanner pick, from 231 recorded charts
python3 "${CLAUDE_SKILL_DIR}/bot.py" run --symbols TSLA,AAPL               # today, live, dry-run: nothing is placed
python3 "${CLAUDE_SKILL_DIR}/bot.py" run --mode confirm                    # live, asks you yes or no per order
python3 "${CLAUDE_SKILL_DIR}/bot.py" run --mode auto --broker alpaca       # live PAPER account, Alpaca, hands off
python3 "${CLAUDE_SKILL_DIR}/bot.py" once --now "2026-10-19 10:15"         # one tick (for a cron or /loop)
python3 "${CLAUDE_SKILL_DIR}/bot.py" status --events 20                    # candidates, positions, P&L, the log
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the pack's limits: budget, max loss, daily loss, entry window, `flat_by`, `paper` |
| `trader/bot.json` | the bot's own settings: symbols, scanner, setup, stop and target rules, gates, mode, broker |
| `trader/watchlist.json`, `entry.json`, `plan.json`, `timemachine.json`, `orders.json`, `prices.json` | written by the skills as the bot calls them, one symbol at a time |
| `trader/state/risk.json`, `trader/state/broker.json`, `trader/portfolio.json` | trader-risk's verdicts, every yes and fill, cash and shares, exactly as the skills keep them |
| `trader/state/bot.json` | the session: each candidate's stage results and why it was taken or not, positions, P&L, the event log |
| `trader/replays/<date>/` | a replay's own `trader/` folder, so it never touches a live session |

## The loop

Every bar (5 minutes by default, 1 minute with `"interval": "1m"`), for
every symbol that is not done yet:

1. **Scanner** (if `scan.enabled`): `scan.py orb` on the universe, a
   watchlist file, or a symbol list; new hits join the candidates.
2. **Entry**: `entry.py check SYMBOL`. Waiting, expired, or triggered. A
   trigger older than `setup.stale_minutes` (5) is skipped: a late entry is
   a different trade.
3. **News** (if `news.enabled`): the symbol's headlines and 8-Ks are logged;
   a tag in `news.block_tags` (for example `["earnings"]`) skips the trade.
4. **Exit**: `exit.py plan` from the entry, with `plan.stop` (`or-mid`) and
   `plan.target` (`1R`).
5. **Time Machine**: `timemachine.py run` on the plan. The trade is skipped
   when target-first minus stop-first is under `timemachine.min_edge_pct`,
   or when the setup fired fewer than `min_history_triggers` times. With no
   60-day history to replay and `required` true, it is skipped too.
6. **Budget**: a fresh price goes to `trader/prices.json`, then `size.py`
   sizes the bracket order to the max loss and the budget.
7. **Risk**: `check.py`. BLOCK ends it, with the reason in the log.
8. **Broker**: `ticket.py show`. Only a READY ticket goes further, by mode.

While a position is open the bot watches it every `poll_seconds`, records
the stop or target fill when the bracket does its job, and at `flat_by`
(15:55) closes whatever is left. `max_open_positions` (1) holds the others
back; trader-risk's trades-per-day and daily-loss limits still apply.

## The three modes

| mode | what happens at a READY ticket |
| --- | --- |
| `dry-run` (default) | logs "DRY RUN, nothing placed". The whole method runs, nobody trades. |
| `confirm` | prints the ticket and asks "Place this order? Reply yes or no." Your words go to `ticket.py approve`, which accepts only an explicit yes for that one order (not "sure", not "yes to all", not a yes that names another stock). Then the broker places it. |
| `auto` | the bot says yes itself and writes that into `trader/state/broker.json` in plain words ("trader-bot auto mode ... nobody typed this"). **Paper accounts only.** With `"paper": false` in `trader/config.json` the bot refuses to start in auto mode. |

Outside Claude there is no "Allow once / Deny" card, so in `confirm` mode
your typed yes is the last gate before the broker. The bot says so above
the prompt.

## The two brokers

- **`sim`** (default): a paper broker inside the bot. Fills a market buy at
  the latest price, then walks the bracket bar by bar: a bar whose low
  touches the stop fills the stop (at the open when it gaps through), a bar
  whose high reaches the target fills the target, and when one bar does
  both, the stop wins. Needs nothing but bars.
- **`alpaca`**: Alpaca's REST API, paper (`paper-api.alpaca.markets`) unless
  the pack's config says `"paper": false`. Keys come from the environment,
  `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (the names the MCP server uses),
  never from a file in the repo and never printed. The bot sends the bracket
  as Alpaca's `take_profit` / `stop_loss` legs, waits for the fill, watches
  the position, and at `flat_by` closes it with `cancel_orders=true`.

Fills go through `ticket.py result` and `ticket.py fill`, so the pack's own
log, portfolio and daily-loss count stay right, and `/trader-risk` sees the
bot's trades like anyone else's.

## The window and the Windows app

`gui.py` is a Tkinter window over the same bot: symbols, mode, broker and
the scanner checkbox, three buttons (Replay the reel day, Run today, Status),
Stop, a log pane, and a Reply box that lights up when the bot waits for
your yes or no in confirm mode. It never trades by itself: each button
starts `bot.py` as a subprocess with the arguments you would have typed.
`python3 bot.py gui` opens it (Debian/Ubuntu need `python3-tk`).

`installer/` builds both into `DayTraderBot-Setup-<version>.exe`
(PyInstaller plus Inno Setup; `installer/build.ps1` does both). The
installed `DayTraderBot.exe` is the window; `DayTraderBotCLI.exe` takes the
same commands as `bot.py`, opens the console menu when double-clicked, and
runs the pack's tool scripts itself (no Python on the machine). Data lives
in `%USERPROFILE%\DayTraderBot\trader\`; `DAYTRADERBOT_HOME` moves it. The
README has the details.

## Replays

`replay` runs a whole session on a simulated clock, offline, on the sim
broker, in its own folder (`trader/replays/<date>/`, or `--into FOLDER`). It takes a saved
Yahoo chart (`--bars TSLA.json`, or a folder of `SYMBOL.json`), the 60-day
history for the Time Machine (`--history`), the scanner's recorded charts
(`--wall`) and saved headlines (`--news`). `--fixtures` wires in the test
fixtures: TSLA on Monday 2026-10-19. `--reply yes` scripts a confirm-mode
answer. `--whole-day` keeps ticking to the close even when nothing is left
to do. A replay stops on its own when every candidate is settled and the
bars have run out; a position still open then is marked at the last bar.

## Example: the reel, by the bot

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

The fixtures end at 10:50 that morning, so the replay leaves the position
open, marked at $366.68 (+$20.00 unrealised). Nothing about that is money.

## Steps

1. `bot.py init`, then set the budget and limits in `trader/config.json`
   and the symbols, gates and mode in `trader/bot.json`.
2. Replay first: `bot.py replay --fixtures`, then your own recorded days.
   Read every "skipped" and "BLOCK" line; the gates are the point.
3. Run live in `dry-run` for a few sessions. `bot.py status --events 50`
   shows what it would have done.
4. Then `confirm` mode, then `auto` on a paper account with the sim broker,
   then `auto` with `--broker alpaca` on Alpaca's paper account.
5. Going live is the user's own decision, made by editing `"paper"` in
   their own config. The bot never does it, and never runs auto mode there.

## What it never does

- Never places an order in dry-run mode, in auto mode on a live account, or
  on a ticket trader-risk did not pass today.
- Never turns a doubtful reply into a yes: `ticket.py approve` decides.
- Never retries a failed order without a new check.
- Never asks for, stores or prints API keys. They live in the environment.
- Never shorts, trades options, crypto or on margin: the pack's config
  blocks them and trader-risk enforces it.
- Never promises a fill price, a win rate or a profit.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
  trader-bot is the one exception, and it is a narrow one: in `auto` mode on a PAPER account it says yes
  itself and writes that into the log in plain words; on a live account it refuses, and in `confirm` mode
  your typed yes is the gate.
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
