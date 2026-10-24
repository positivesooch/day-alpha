#!/usr/bin/env python3
"""Day Trader Bot, the window.

A Tkinter front for trader-bot/bot.py. The Bot tab starts the loop as a
subprocess (the console exe when installed, python3 bot.py otherwise), streams
its output into the log, and sends typed replies in confirm mode. The Trade,
Broker and Settings tabs talk to the same program: connect and read the
account, place a bracket order, close one position, flatten everything, save
broker keys, and switch between the paper account and a live one. A live
switch and auto mode on a live account both demand the phrase TRADE REAL MONEY.

    python3 trader-bot/gui.py            # or: python3 trader-bot/bot.py gui
    DayTraderBot.exe                     # the installed app

Standard library only (tkinter ships with Python on Windows and macOS; on
Debian/Ubuntu it is the python3-tk package).
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, simpledialog, ttk
except ImportError:  # a server without Tk; the console menu still works
    tk = None

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = getattr(sys, "_MEIPASS", None) or (os.path.dirname(sys.executable) if FROZEN else None)
PACK_BOT = os.path.join(BUNDLE, "pack", "trader-bot") if FROZEN else HERE
sys.path.insert(0, PACK_BOT)
import access  # noqa: E402  (the code Day Trader Code writes, which this window asks for)
import bot  # noqa: E402  (the same module the console runs)
import markets  # noqa: E402  (stock sectors, commodities and forex)
import tape  # noqa: E402  (live quotes and the candle layout)
import update as updater  # noqa: E402

VERSION = bot.VERSION
CLI_NAME = "DayTraderBotCLI.exe" if sys.platform == "win32" else "DayTraderBotCLI"
MODES = ("dry-run", "confirm", "auto")
BROKERS = ("sim", "alpaca", "mt5")
PROMPTS = ("Reply yes or no: ", "Enter keeps trader\\bot.json): ", "Choose: ")
PROMPT_STARTS = ("Press Enter",)
REEL = ["replay", "--fixtures", "--fresh"]


def cli_command():
    """The command that runs the bot: the console exe next to this one when
    installed (the windowed exe has no console to hand the tools), else python3 bot.py."""
    if FROZEN:
        sibling = os.path.join(os.path.dirname(sys.executable), CLI_NAME)
        if os.path.exists(sibling):
            return [sibling]
        return [sys.executable]
    return [sys.executable, os.path.join(PACK_BOT, "bot.py")]


def build_argv(action, symbols="", mode="dry-run", broker="sim", scan=False, home=None):
    """The bot's argv for one button. `action` is run, reel, or status."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    if broker not in BROKERS:
        raise ValueError("broker must be one of %s" % ", ".join(BROKERS))
    syms = ",".join(s.strip().upper() for s in symbols.replace(";", ",").split(",") if s.strip())
    if action == "status":
        argv = ["status", "--events", "30"]
    elif action == "reel":
        argv = list(REEL) + ["--mode", mode]
        if scan:
            argv.append("--scan")
        elif syms:
            argv += ["--symbols", syms]
    elif action == "run":
        argv = ["run", "--mode", mode, "--broker", broker]
        if scan:
            argv.append("--scan")
        if syms:
            argv += ["--symbols", syms]
    else:
        raise ValueError("unknown action %r" % action)
    if home:
        argv += ["--dir", home]
    return argv


def looks_like_prompt(tail):
    """True when the bot's last unfinished line is waiting for the user."""
    t = tail.rstrip("\r")
    if any(t.endswith(p.rstrip()) or t.endswith(p) for p in PROMPTS):
        return True
    return any(t.lstrip().startswith(p) for p in PROMPT_STARTS)


def _money(value):
    if value is None or value == "":
        return "-"
    try:
        return "$%s" % format(float(value), ",.2f")
    except (TypeError, ValueError):
        return str(value)


def _signed_money(value):
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 0:
        return "+" + _money(number)
    if number < 0:
        return "-" + _money(abs(number))
    return _money(0)


def _pnl_result(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "flat"
    if number > 0:
        return "profit"
    if number < 0:
        return "loss"
    return "flat"


def _position_pnl(pos):
    if pos.get("pnl") is not None:
        try:
            return round(float(pos["pnl"]), 2)
        except (TypeError, ValueError):
            return None
    try:
        return round((float(pos["mark"]) - float(pos["entry"])) * float(pos["qty"]), 2)
    except (TypeError, ValueError, KeyError):
        return None


def _trade_row(symbol, pos, when=""):
    pnl = _position_pnl(pos)
    return {"when": when or pos.get("exit_time") or "", "symbol": symbol,
            "side": pos.get("side") or "long", "qty": pos.get("qty"),
            "entry": pos.get("entry"), "mark": pos.get("mark"), "exit": pos.get("exit"),
            "stop": pos.get("stop"), "target": pos.get("target"), "pnl": pnl,
            "result": _pnl_result(pnl), "why": pos.get("exit_reason") or ""}


def read_trade_book(home):
    """Open positions from the live session, and every closed trade the bot has recorded.

    Closed trades include the live session and each replay under trader/replays.
    A missing or unreadable file is skipped. Paper and live fills both land here;
    the row does not place an order.
    """
    live_path = os.path.join(home, "trader", "state", "bot.json")
    paths = []
    if os.path.isfile(live_path):
        paths.append(live_path)
    replays = os.path.join(home, "trader", "replays")
    if os.path.isdir(replays):
        for day in sorted(os.listdir(replays)):
            path = os.path.join(replays, day, "trader", "state", "bot.json")
            if os.path.isfile(path):
                paths.append(path)
    opens, closed = [], []
    for path in paths:
        state = bot.read_state(path)
        if not state:
            continue
        date = state.get("date") or ""
        live = os.path.abspath(path) == os.path.abspath(live_path)
        for symbol, pos in (state.get("positions") or {}).items():
            if not isinstance(pos, dict):
                continue
            when = pos.get("exit_time") or date
            row = _trade_row(symbol, pos, when)
            if pos.get("status") == "closed":
                closed.append(row)
            elif live and pos.get("status") == "open":
                opens.append(row)
    closed.sort(key=lambda row: row.get("when") or "", reverse=True)
    wins = len([row for row in closed if row["result"] == "profit"])
    losses = len([row for row in closed if row["result"] == "loss"])
    return {"open": opens, "closed": closed,
            "open_pnl": round(sum(row["pnl"] or 0 for row in opens), 2),
            "closed_pnl": round(sum(row["pnl"] or 0 for row in closed), 2),
            "wins": wins, "losses": losses}


def describe(argv):
    """The command as shown in the log: without the --dir pair, which is always the data folder."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--dir":
            skip = True
            continue
        out.append(a)
    return " ".join(out)


class Runner(object):
    """One bot subprocess: output lands in a queue, replies go to its stdin."""

    def __init__(self, argv, on_exit=None):
        self.argv = argv
        self.queue = queue.Queue()
        self.on_exit = on_exit
        self.proc = None
        self.returncode = None

    def start(self):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(cli_command() + self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, **kw)
        threading.Thread(target=self._pump, daemon=True).start()
        return self

    def _pump(self):
        buf = b""
        while True:
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk
            try:
                text = buf.decode("utf-8")
                buf = b""
            except UnicodeDecodeError:
                text = buf[:-3].decode("utf-8", "replace")
                buf = buf[-3:]
            self.queue.put(text)
        if buf:
            self.queue.put(buf.decode("utf-8", "replace"))
        self.returncode = self.proc.wait()
        self.queue.put(None)

    def send(self, text):
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            try:
                self.proc.stdin.write((text + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class App(object):
    def __init__(self, root, home=None):
        self.root = root
        self.home = home or bot.default_home()
        os.makedirs(os.path.join(self.home, "trader"), exist_ok=True)
        self.runner = None
        self.tail = ""
        root.title("Day Trader Bot %s" % VERSION)
        root.minsize(980, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.desk_busy = False
        self.account_doc = None
        self._tape_job = None
        self._tape_gen = 0
        self._tape_snap = None
        self._closing = False
        self._build()
        self._refresh_header()
        self.append("Day Trader Bot %s. Education and tooling, not financial advice. Most people who day trade "
                    "lose money.\nData folder: %s\n\nOpen this window with the code from Day Trader Code "
                    "(Start menu, or gui.py --codegen). Market: a live candle chart. Change symbol switches "
                    "the chart and the order ticket. The list covers stocks, commodities and currencies "
                    "(EURUSD and the other pairs). Trade: live positions and closed-trade profit or loss. "
                    "FLATTEN ALL closes everything and stops new entries for the day. Broker: Alpaca keys or "
                    "a MetaTrader 5 login. Settings: your limits, and the switch from paper to a live account "
                    "(that switch asks you to type %s).\n\n" % (VERSION, self.home, bot.LIVE_PHRASE))
        root.after(100, self._drain)
        self.update_info = None
        self.update_busy = False
        self._trade_job = None
        self._refresh_trades()
        if updater.settings(self.home).get("check_on_start", True):
            root.after(1500, lambda: self.check_updates(quiet=True))

    # ---- layout

    def _build(self):
        r = self.root
        pad = {"padx": 8, "pady": 4}
        head = ttk.Frame(r)
        head.pack(fill="x", **pad)
        self.title_var = tk.StringVar()
        ttk.Label(head, textvariable=self.title_var, font=("TkDefaultFont", 13, "bold")).pack(side="left")
        self.account_var = tk.StringVar()
        self.account_lbl = ttk.Label(head, textvariable=self.account_var)
        self.account_lbl.pack(side="right")
        self.btn_update = ttk.Button(head, text="Check for updates", command=self.check_updates)
        self.btn_update.pack(side="right", padx=(0, 12))

        acct = ttk.Frame(r)
        acct.pack(fill="x", **pad)
        self.btn_connect = ttk.Button(acct, text="Connect", command=self.connect)
        self.btn_connect.pack(side="left")
        self.conn_var = tk.StringVar(value="not connected")
        ttk.Label(acct, textvariable=self.conn_var).pack(side="left", padx=8)
        self.balance_var = tk.StringVar(value="Balance -")
        self.equity_var = tk.StringVar(value="Equity -")
        self.day_var = tk.StringVar(value="Today -")
        ttk.Label(acct, textvariable=self.balance_var).pack(side="left", padx=8)
        ttk.Label(acct, textvariable=self.equity_var).pack(side="left", padx=8)
        ttk.Label(acct, textvariable=self.day_var).pack(side="left", padx=8)
        self.btn_flatten = ttk.Button(acct, text="FLATTEN ALL", command=self.flatten_all)
        self.btn_flatten.pack(side="right")

        self.book = ttk.Notebook(r)
        self.book.pack(fill="both", expand=True, padx=8, pady=4)
        bot_tab = ttk.Frame(self.book)
        market_tab = ttk.Frame(self.book)
        trade_tab = ttk.Frame(self.book)
        broker_tab = ttk.Frame(self.book)
        settings_tab = ttk.Frame(self.book)
        self.book.add(bot_tab, text="Bot")
        self.book.add(market_tab, text="Market")
        self.book.add(trade_tab, text="Trade")
        self.book.add(broker_tab, text="Broker")
        self.book.add(settings_tab, text="Settings")

        form = ttk.LabelFrame(bot_tab, text="What to do")
        form.pack(fill="x", padx=8, pady=6)
        ttk.Label(form, text="Symbols").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.symbols_var = tk.StringVar(value=self._symbols_from_config())
        self.chart_symbol = tk.StringVar(value=self._first_symbol())
        self.trade_symbol = tk.StringVar(value=self.chart_symbol.get())
        ttk.Entry(form, textvariable=self.symbols_var, width=28).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(form, text="Change symbol").grid(row=0, column=2, sticky="e", padx=6)
        self.symbol_pick = ttk.Combobox(form, textvariable=self.chart_symbol, state="readonly", width=14)
        self.symbol_pick.grid(row=0, column=3, sticky="w", padx=6)
        self.symbol_pick.bind("<<ComboboxSelected>>", self._symbol_picked)
        self.scan_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="let the scanner add opening-range breakouts",
                        variable=self.scan_var).grid(row=1, column=0, columnspan=4, sticky="w", padx=6)
        ttk.Label(form, text="Mode").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.mode_var = tk.StringVar(value="dry-run")
        mode = ttk.Combobox(form, textvariable=self.mode_var, values=MODES, state="readonly", width=12)
        mode.grid(row=2, column=1, sticky="w", padx=6)
        mode.bind("<<ComboboxSelected>>", lambda e: self._explain_mode())
        ttk.Label(form, text="Broker").grid(row=2, column=2, sticky="e", padx=6)
        self.broker_var = tk.StringVar(value="sim")
        broker = ttk.Combobox(form, textvariable=self.broker_var, values=BROKERS, state="readonly", width=10)
        broker.grid(row=2, column=3, sticky="w", padx=6)
        broker.bind("<<ComboboxSelected>>", lambda e: self._explain_mode())
        self.mode_help = tk.StringVar()
        ttk.Label(form, textvariable=self.mode_help, foreground="#555").grid(row=3, column=0, columnspan=4,
                                                                             sticky="w", padx=6, pady=(0, 6))
        form.columnconfigure(1, weight=1)
        self._explain_mode()

        bar = ttk.Frame(bot_tab)
        bar.pack(fill="x", padx=8, pady=4)
        self.btn_reel = ttk.Button(bar, text="Replay the reel day", command=lambda: self.launch("reel"))
        self.btn_reel.pack(side="left", padx=(0, 6))
        self.btn_run = ttk.Button(bar, text="Run today", command=lambda: self.launch("run"))
        self.btn_run.pack(side="left", padx=(0, 6))
        self.btn_live_auto = ttk.Button(bar, text="Run live, auto", command=self.run_live_auto)
        self.btn_live_auto.pack(side="left", padx=(0, 6))
        self.btn_status = ttk.Button(bar, text="Status", command=lambda: self.launch("status"))
        self.btn_status.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Settings folder", command=self.open_settings).pack(side="right")
        ttk.Button(bar, text="Clear log", command=self.clear).pack(side="right", padx=(0, 6))
        self._build_markets(bot_tab)
        self._build_market(market_tab)
        self._build_trade(trade_tab)
        self._build_broker(broker_tab)
        self._build_settings(settings_tab)

        self.log = scrolledtext.ScrolledText(r, wrap="word", font=("Consolas" if sys.platform == "win32" else
                                                                   "TkFixedFont", 10), state="disabled",
                                             height=12)
        self.log.pack(fill="both", expand=True, padx=8, pady=4)
        self.log.tag_configure("prompt", foreground="#0a58ca", font=("TkDefaultFont", 10, "bold"))
        self.log.tag_configure("bad", foreground="#b00020")
        self.log.tag_configure("good", foreground="#1b7f3b")

        reply = ttk.Frame(r)
        reply.pack(fill="x", **pad)
        self.reply_lbl = ttk.Label(reply, text="Reply")
        self.reply_lbl.pack(side="left")
        self.reply_var = tk.StringVar()
        self.reply_entry = ttk.Entry(reply, textvariable=self.reply_var, state="disabled")
        self.reply_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.reply_entry.bind("<Return>", lambda e: self.send())
        self.btn_send = ttk.Button(reply, text="Send", command=self.send, state="disabled")
        self.btn_send.pack(side="left")

        self.status_var = tk.StringVar(value="idle")
        ttk.Label(r, textvariable=self.status_var, anchor="w", relief="sunken").pack(fill="x", side="bottom")
        self._build_lock()

    def _build_lock(self):
        """Cover the desk until the code from Day Trader Code is typed."""
        self.locked = True
        self.lock = tk.Frame(self.root, background="#e8edf2")
        self.lock.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lock.lift()
        card = tk.Frame(self.lock, background="#ffffff", padx=28, pady=22,
                        highlightthickness=1, highlightbackground="#c5ced6")
        card.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(card, text="Open Day Trader Bot", background="#ffffff",
                 font=("TkDefaultFont", 16, "bold")).pack()
        tk.Label(card, text="Open Day Trader Code, press Generate, and type the 6 digits.\n"
                            "That code is not your broker password.",
                 background="#ffffff", justify="center", wraplength=380).pack(pady=(8, 12))
        self.code_var = tk.StringVar()
        self.code_entry = tk.Entry(card, textvariable=self.code_var, width=12, justify="center",
                                   font=("TkFixedFont", 18))
        self.code_entry.pack()
        self.code_entry.bind("<Return>", lambda e: self._try_unlock())
        self.code_msg = tk.StringVar()
        tk.Label(card, textvariable=self.code_msg, background="#ffffff", fg="#b00020",
                 wraplength=380, justify="center").pack(pady=(8, 0))
        row = ttk.Frame(card)
        row.pack(pady=(10, 0))
        ttk.Button(row, text="Open", command=self._try_unlock).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Open Day Trader Code", command=self._launch_codegen).pack(side="left")
        tk.Label(card, text=access.code_path(access.user_data_home()), background="#ffffff",
                 fg="#666", wraplength=380, justify="center").pack(pady=(8, 0))
        self.code_entry.focus_set()

    def _launch_codegen(self):
        """Open Day Trader Code against the same data folder this window reads."""
        target = access.user_data_home()
        if FROZEN:
            argv = [sys.executable, "--codegen", "--dir", target]
        else:
            argv = [sys.executable, os.path.join(HERE, "gui.py"), "--codegen", "--dir", target]
        try:
            subprocess.Popen(argv)
        except OSError as exc:
            self.code_msg.set("Could not open Day Trader Code: %s" % exc)

    def _try_unlock(self):
        typed = self.code_entry.get() if self.code_entry else self.code_var.get()
        ok, msg = access.check(self.home, typed)
        self.code_msg.set("" if ok else msg)
        if not ok:
            return
        self.locked = False
        self.lock.place_forget()
        self.append("Opened with the code from Day Trader Code.\n", "good")

    def _build_markets(self, tab):
        box = ttk.LabelFrame(tab, text="Stocks, commodities and currencies. Pick types, then watch them. "
                                        "Futures and forex pairs are added for the sim and MetaTrader 5. "
                                        "Alpaca watches the ETFs (GLD, USO, FXE).")
        box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.market_list = tk.Listbox(box, selectmode="extended", height=8, exportselection=False)
        for g in markets.GROUPS:
            self.market_list.insert("end", "%s (%s)" % (g["name"], markets.kind_label(g["kind"])))
        self.market_list.pack(fill="both", expand=True, padx=6, pady=4)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(row, text="Watch selected", command=self.apply_markets).pack(side="left")
        ttk.Button(row, text="All stock types", command=lambda: self.apply_markets(markets.ids_of("stock"))).pack(
            side="left", padx=6)
        ttk.Button(row, text="All commodities", command=lambda: self.apply_markets(markets.ids_of("commodity"))).pack(
            side="left")
        ttk.Button(row, text="All forex", command=lambda: self.apply_markets(markets.ids_of("forex"))).pack(
            side="left", padx=6)
        ttk.Button(row, text="All US common stocks", command=self.watch_all_us_stocks).pack(side="left")

    def apply_markets(self, group_ids=None):
        if group_ids is None:
            group_ids = [markets.GROUPS[i]["id"] for i in self.market_list.curselection()]
        if not group_ids:
            messagebox.showinfo("Markets", "Select one or more types in the list.")
            return
        futures = self.broker_var.get() != "alpaca"
        syms = markets.pick(group_ids, futures=futures)
        self.symbols_var.set(",".join(syms))
        self.trade_symbol.set(syms[0])
        self.scan_var.set(False)
        mapping = markets.mt5_map(group_ids) if futures else {}
        if mapping:
            current = {}
            cfg = bot.read_json(os.path.join(self.home, "trader", "bot.json"), required=False) or {}
            current.update((cfg.get("mt5") or {}).get("symbols") or {})
            current.update(mapping)
            self.desk(["settings", "--dir", self.home, "--set", "bot.mt5.symbols=%s" % json.dumps(current)])
        self.append("Watching %d symbols%s.\n" % (
            len(syms), ", including forex pairs" if any("=" in s and s.endswith("X") for s in syms) else ""), "prompt")

    def watch_all_us_stocks(self):
        """Turn the scanner onto Nasdaq Trader's common-stock list, and download that list if it is missing."""
        self.scan_var.set(True)
        out = os.path.join(self.home, "trader", "universe.txt")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            self.append("Scanner will use the US common-stock list in %s. Leave Symbols as they are, or clear "
                        "them, and press Run. The bot still only takes a few trades a day.\n" % out, "prompt")
            return
        if FROZEN:
            script = os.path.join(BUNDLE, "pack", "trader-scanner", "scan.py")
            argv = cli_command() + ["--tool", script, "universe", "--out", out]
        else:
            script = os.path.join(os.path.dirname(HERE), "trader-scanner", "scan.py")
            argv = [sys.executable, script, "universe", "--out", out]
        self.append("Downloading the US common-stock list from Nasdaq Trader...\n", "prompt")
        self.desk_busy = True

        def work():
            try:
                p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=180)
                text, code = p.stdout.decode("utf-8", "replace"), p.returncode
            except Exception as e:
                text, code = str(e), 1

            def finish():
                self.desk_busy = False
                self._show(text if text.endswith("\n") else text + "\n")
                if code == 0:
                    self.append("US common stocks are ready. Press Run with the scanner ticked.\n", "good")
                self.status_var.set("idle" if code == 0 else "idle (exit code %s)" % code)
            self.root.after(0, finish)
        threading.Thread(target=work, daemon=True).start()

    def _build_market(self, tab):
        """Live candles and a last-price tape. Yahoo is polled; the chart follows symbol edits."""
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Symbol").pack(side="left")
        self.chart_symbol_box = ttk.Combobox(top, textvariable=self.chart_symbol, state="readonly", width=14)
        self.chart_symbol_box.pack(side="left", padx=6)
        self.chart_symbol_box.bind("<<ComboboxSelected>>", self._symbol_picked)
        ttk.Label(top, text="Candles").pack(side="left")
        self.chart_interval = tk.StringVar(value="5m")
        interval = ttk.Combobox(top, textvariable=self.chart_interval, values=tape.INTERVALS,
                                state="readonly", width=6)
        interval.pack(side="left", padx=6)
        interval.bind("<<ComboboxSelected>>", lambda e: self.refresh_tape(now=True))
        self.tape_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Live", variable=self.tape_on, command=self._tape_arm).pack(side="left", padx=6)
        ttk.Label(top, text="every").pack(side="left")
        self.tape_every = tk.StringVar(value="15")
        every = ttk.Combobox(top, textvariable=self.tape_every, values=("5", "15", "30", "60"),
                             state="readonly", width=4)
        every.pack(side="left", padx=4)
        ttk.Label(top, text="s").pack(side="left")
        every.bind("<<ComboboxSelected>>", lambda e: self._tape_arm())
        ttk.Button(top, text="Refresh", command=lambda: self.refresh_tape(now=True)).pack(side="left", padx=8)
        self.quote_var = tk.StringVar(value="Live prices load from Yahoo Finance, the same feed the sim trades on.")
        ttk.Label(tab, textvariable=self.quote_var, wraplength=920).pack(anchor="w", padx=8)
        self.chart = tk.Canvas(tab, height=280, background="#f4f6f8", highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=8, pady=4)
        self.chart.bind("<Configure>", lambda e: self._draw_chart(self._tape_snap) if self._tape_snap else None)
        cols = ("symbol", "last", "change", "state")
        self.tape_list = ttk.Treeview(tab, columns=cols, show="headings", height=5)
        for name, heading, width in (("symbol", "Symbol", 110), ("last", "Last", 110),
                                     ("change", "Change", 180), ("state", "Session", 120)):
            self.tape_list.heading(name, text=heading)
            self.tape_list.column(name, width=width, anchor="w")
        self.tape_list.pack(fill="x", padx=8, pady=(0, 6))
        self.tape_list.bind("<<TreeviewSelect>>", self._tape_picked)
        self._sync_chart_symbols()
        self.symbols_var.trace_add("write", lambda *a: self._symbols_edited())
        self.root.after(700, self._tape_arm)

    def _first_symbol(self):
        syms = tape.split_symbols(self.symbols_var.get(), limit=1)
        return syms[0] if syms else "TSLA"

    def _catalog(self):
        syms = list(markets.catalog())
        seen = set(syms)
        for name in tape.split_symbols(self.symbols_var.get(), limit=40):
            if name not in seen:
                syms.append(name)
                seen.add(name)
        current = self.chart_symbol.get().strip().upper()
        if current and current not in seen:
            syms.insert(0, current)
        return syms or ["TSLA"]

    def _sync_chart_symbols(self):
        syms = self._catalog()
        values = tuple(syms)
        self.chart_symbol_box.configure(values=values)
        self.symbol_pick.configure(values=values)
        if getattr(self, "trade_symbol_box", None) is not None:
            self.trade_symbol_box.configure(values=values)

    def _symbol_picked(self, event=None):
        """A choice in the symbol dropdown moves the chart and the order ticket onto that name."""
        sym = self.chart_symbol.get().strip().upper()
        if not sym:
            return
        if self.chart_symbol.get() != sym:
            self.chart_symbol.set(sym)
        self.trade_symbol.set(sym)
        watched = tape.split_symbols(self.symbols_var.get(), limit=40)
        if sym not in watched:
            self.symbols_var.set(",".join([sym] + watched))
        self._sync_chart_symbols()
        self._tape_soon()

    def _symbols_edited(self):
        """A change in the symbol box (typed, or a market-list button) moves the chart onto that list."""
        syms = tape.split_symbols(self.symbols_var.get(), limit=40)
        if syms and self.chart_symbol.get().strip().upper() not in syms:
            self.chart_symbol.set(syms[0])
            self.trade_symbol.set(syms[0])
        self._sync_chart_symbols()
        self._tape_soon()

    def _trade_symbol_picked(self, event=None):
        sym = self.trade_symbol.get().strip().upper()
        if not sym:
            return
        self.chart_symbol.set(sym)
        self._symbol_picked()

    def _trade_symbol_edited(self):
        sym = self.trade_symbol.get().strip().upper()
        if not sym or sym == self.chart_symbol.get().strip().upper():
            return
        self.chart_symbol.set(sym)
        self._sync_chart_symbols()
        self._tape_soon()

    def _tape_soon(self):
        """Wait a beat so typing a symbol does not fire a request per keystroke."""
        if self._closing:
            return
        if self._tape_job is not None:
            try:
                self.root.after_cancel(self._tape_job)
            except tk.TclError:
                pass
            self._tape_job = None
        self._tape_job = self.root.after(400, lambda: self.refresh_tape(now=True))

    def _tape_arm(self):
        if self.tape_on.get():
            self.refresh_tape(now=True)
        elif self._tape_job is not None:
            try:
                self.root.after_cancel(self._tape_job)
            except tk.TclError:
                pass
            self._tape_job = None

    def _tape_picked(self, event=None):
        sel = self.tape_list.selection()
        if not sel:
            return
        values = self.tape_list.item(sel[0], "values")
        if not values:
            return
        sym = str(values[0]).strip().upper()
        if sym and sym != self.chart_symbol.get().strip().upper():
            self.chart_symbol.set(sym)
            self.refresh_tape(now=True)

    def refresh_tape(self, now=False):
        """Pull the chart symbol and up to seven watched names. `now` ignores the Live checkbox."""
        if self._closing:
            return
        if self._tape_job is not None:
            try:
                self.root.after_cancel(self._tape_job)
            except tk.TclError:
                pass
            self._tape_job = None
        if not now and not self.tape_on.get():
            return
        symbol = self.chart_symbol.get().strip().upper()
        if not symbol:
            self.quote_var.set("Type a symbol to load its chart.")
            return
        interval = self.chart_interval.get() or "5m"
        others = [s for s in tape.split_symbols(self.symbols_var.get(), limit=8) if s != symbol]
        self._tape_gen += 1
        gen = self._tape_gen
        self.quote_var.set("Loading %s (%s) from Yahoo Finance..." % (symbol, interval))

        def work():
            try:
                main = tape.load(symbol, interval)
                rows = [main]
                for name in others:
                    try:
                        rows.append(tape.load(name, "5m"))
                    except Exception as err:
                        rows.append({"symbol": name, "error": str(err)})
                payload = (main, rows, None)
            except Exception as err:
                payload = (None, [], err)
            try:
                self.root.after(0, lambda: self._tape_show(gen, payload))
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _tape_show(self, gen, payload):
        if self._closing or gen != self._tape_gen:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        main, rows, err = payload
        if err is not None:
            self.quote_var.set("%s: %s" % (self.chart_symbol.get().strip().upper(), err))
            self._draw_chart(None, error=str(err))
        else:
            self._tape_snap = main
            self.quote_var.set(tape.headline(main) + "    Yahoo Finance")
            self._draw_chart(main)
            self._fill_tape(rows)
        self._tape_later()

    def _tape_later(self):
        if self._closing or not self.tape_on.get():
            return
        try:
            seconds = int(self.tape_every.get())
        except (TypeError, ValueError):
            seconds = 15
        seconds = min(120, max(5, seconds))
        self._tape_job = self.root.after(seconds * 1000, lambda: self.refresh_tape(now=True))

    def _fill_tape(self, rows):
        self.tape_list.delete(*self.tape_list.get_children())
        for row in rows:
            if row.get("error"):
                self.tape_list.insert("", "end", values=(row.get("symbol") or "?", "-", str(row["error"])[:48], ""))
                continue
            self.tape_list.insert("", "end", values=(
                row.get("symbol") or "?", tape.price_text(row.get("price")), tape.change_text(row),
                tape.state_text(row.get("state"))))

    def _chart_levels(self, symbol):
        doc = self.account_doc or {}
        return tape.levels_for(symbol, doc.get("positions"))

    def _draw_chart(self, snap, error=None):
        canvas = self.chart
        canvas.delete("all")
        width = max(int(canvas.winfo_width() or 0), 640)
        height = max(int(canvas.winfo_height() or 0), 280)
        if error:
            canvas.create_text(width / 2, height / 2, text=error, fill="#b00020", width=width - 48)
            return
        bars = (snap or {}).get("bars") if snap else None
        if not bars:
            canvas.create_text(width / 2, height / 2, text="No candles yet. Pick a symbol and press Refresh.",
                               fill="#555")
            return
        laid = tape.layout(bars, width, height, levels=self._chart_levels(snap.get("symbol")))
        for lab in laid["labels"]:
            canvas.create_line(laid["left"], lab["y"], width - laid["right"], lab["y"], fill="#e1e5ea")
            canvas.create_text(6, lab["y"], anchor="w", text=tape.price_text(lab["price"]), fill="#667",
                               font=("TkDefaultFont", 8))
        for candle in laid["candles"]:
            color = "#1b7f3b" if candle["up"] else "#b00020"
            canvas.create_line(candle["x"], candle["high"], candle["x"], candle["low"], fill=color)
            top = min(candle["open"], candle["close"])
            bottom = max(candle["open"], candle["close"])
            if bottom - top < 1:
                bottom = top + 1
            half = candle["w"] / 2.0
            canvas.create_rectangle(candle["x"] - half, top, candle["x"] + half, bottom, fill=color, outline=color)
        colors = {"entry": "#0a58ca", "stop": "#b00020", "target": "#1b7f3b", "mark": "#6b4c9a"}
        for mark in laid["marks"]:
            color = colors.get(mark["name"], "#333")
            canvas.create_line(laid["left"], mark["y"], width - laid["right"], mark["y"], fill=color, dash=(4, 3))
            canvas.create_text(width - 8, mark["y"], anchor="e",
                               text="%s %s" % (mark["name"], tape.price_text(mark["price"])), fill=color,
                               font=("TkDefaultFont", 8))
        if laid["candles"]:
            canvas.create_line(laid["left"], laid["last_y"], width - laid["right"], laid["last_y"], fill="#0a58ca")
            first, last = laid["candles"][0], laid["candles"][-1]
            if first.get("t"):
                canvas.create_text(first["x"], height - 8, anchor="w",
                                   text=bot.to_eastern(first["t"]).strftime("%H:%M"), fill="#555",
                                   font=("TkDefaultFont", 8))
            if last.get("t") and last is not first:
                canvas.create_text(last["x"], height - 8, anchor="e",
                                   text=bot.to_eastern(last["t"]).strftime("%H:%M"), fill="#555",
                                   font=("TkDefaultFont", 8))

    def _build_trade(self, tab):
        form = ttk.LabelFrame(tab, text="One bracket order (long only). Your limits still size it and can refuse it.")
        form.pack(fill="x", padx=8, pady=6)
        self.trade_qty = tk.StringVar()
        self.trade_entry = tk.StringVar()
        self.trade_stop = tk.StringVar()
        self.trade_target = tk.StringVar()
        self.trade_dry = tk.BooleanVar(value=False)
        fields = (("Symbol", self.trade_symbol), ("Shares", self.trade_qty), ("Entry", self.trade_entry),
                  ("Stop", self.trade_stop), ("Target", self.trade_target))
        for col, (label, var) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=0, column=col, sticky="w", padx=6, pady=(4, 0))
            if label == "Symbol":
                self.trade_symbol_box = ttk.Combobox(form, textvariable=var, state="readonly", width=12)
                self.trade_symbol_box.grid(row=1, column=col, sticky="we", padx=6, pady=(0, 6))
                self.trade_symbol_box.bind("<<ComboboxSelected>>", self._trade_symbol_picked)
            else:
                ttk.Entry(form, textvariable=var, width=12).grid(row=1, column=col, sticky="we", padx=6, pady=(0, 6))
        self._sync_chart_symbols()
        form.columnconfigure(0, weight=1)
        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8)
        ttk.Button(row, text="Place order", command=self.place_order).pack(side="left")
        ttk.Checkbutton(row, text="dry-run (build the ticket, place nothing)", variable=self.trade_dry).pack(
            side="left", padx=12)
        ttk.Label(row, text="Entry blank: use the broker's price. Target blank: 2R.").pack(side="left")

        live = ttk.LabelFrame(tab, text="Live positions")
        live.pack(fill="both", expand=True, padx=8, pady=6)
        self.trade_pnl_var = tk.StringVar(value="No live position. No closed trades yet.")
        ttk.Label(live, textvariable=self.trade_pnl_var).pack(anchor="w", padx=6, pady=(4, 0))
        cols = ("symbol", "side", "qty", "entry", "mark", "pnl", "result", "stop", "target")
        heads = ("Symbol", "Side", "Qty", "Entry", "Mark", "P&L", "Result", "Stop", "Target")
        self.tree = ttk.Treeview(live, columns=cols, show="headings", height=4)
        for c, h in zip(cols, heads):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=80, anchor="e" if c != "symbol" else "w")
        self.tree.tag_configure("profit", foreground="#1b7f3b")
        self.tree.tag_configure("loss", foreground="#b00020")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)
        self.trade_symbol.trace_add("write", lambda *a: self._trade_symbol_edited())
        bar = ttk.Frame(live)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(bar, text="Close selected", command=self.close_selected).pack(side="left")
        ttk.Button(bar, text="Refresh positions", command=self.connect).pack(side="left", padx=6)

        past = ttk.LabelFrame(tab, text="Trade history")
        past.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        hcols = ("when", "symbol", "qty", "entry", "exit", "why", "result", "pnl")
        hheads = ("When", "Symbol", "Qty", "Entry", "Exit", "Why", "Result", "P&L")
        self.history = ttk.Treeview(past, columns=hcols, show="headings", height=6)
        for c, h in zip(hcols, hheads):
            self.history.heading(c, text=h)
            self.history.column(c, width=90, anchor="e" if c not in ("symbol", "why", "result") else "w")
        self.history.tag_configure("profit", foreground="#1b7f3b")
        self.history.tag_configure("loss", foreground="#b00020")
        self.history.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_broker(self, tab):
        form = ttk.LabelFrame(tab, text="Alpaca. Paper keys until you switch to live on Settings. Values are saved "
                                         "encrypted for your Windows user and are not shown again.")
        form.pack(fill="x", padx=8, pady=6)
        self.key_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        ttk.Label(form, text="API key").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(form, textvariable=self.key_var, show="*", width=42).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(form, text="Secret").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(form, textvariable=self.secret_var, show="*", width=42).grid(row=1, column=1, sticky="we", padx=6)
        form.columnconfigure(1, weight=1)

        mt5 = ttk.LabelFrame(tab, text="MetaTrader 5. Leave the login empty to use the account already logged in "
                                        "to the open terminal.")
        mt5.pack(fill="x", padx=8, pady=6)
        self.mt5_login = tk.StringVar()
        self.mt5_server = tk.StringVar()
        self.mt5_pw = tk.StringVar()
        self.mt5_path = tk.StringVar()
        self.mt5_suffix = tk.StringVar()
        for row, (label, var, secret) in enumerate((
                ("Login", self.mt5_login, False), ("Server", self.mt5_server, False),
                ("Password", self.mt5_pw, True), ("Terminal path", self.mt5_path, False),
                ("Symbol suffix", self.mt5_suffix, False))):
            ttk.Label(mt5, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(mt5, textvariable=var, show="*" if secret else "", width=42).grid(
                row=row, column=1, sticky="we", padx=6)
        mt5.columnconfigure(1, weight=1)
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text="Save broker", command=self.save_broker).pack(side="left")
        ttk.Button(bar, text="Forget saved keys", command=self.forget_keys).pack(side="left", padx=6)
        self.cred_var = tk.StringVar(value="Keys: not loaded")
        ttk.Label(bar, textvariable=self.cred_var).pack(side="left", padx=12)

    def _build_settings(self, tab):
        form = ttk.LabelFrame(tab, text="Limits (trader\\config.json and trader\\bot.json)")
        form.pack(fill="x", padx=8, pady=6)
        self.setting_vars = {}
        fields = (
            ("budget", "Budget ($)"), ("max_loss_per_trade_usd", "Max loss per trade ($)"),
            ("max_daily_loss_usd", "Max daily loss ($)"), ("max_trades_per_day", "Max trades a day"),
            ("max_position_pct", "Max position (%)"), ("flat_by", "Flat by (New York)"),
            ("no_entries_before", "No entries before"), ("no_entries_after", "No entries after"),
            ("bot.execution.max_open_positions", "Max open positions"),
            ("bot.plan.stop", "Stop rule"), ("bot.plan.target", "Target rule"),
            ("bot.mt5.prop.initial_balance", "Prop account size ($)"),
            ("bot.mt5.prop.max_daily_loss_pct", "Prop daily loss (%)"),
            ("bot.mt5.prop.max_total_drawdown_pct", "Prop max drawdown (%)"),
            ("bot.mt5.prop.buffer_pct", "Prop buffer (%)"),
        )
        for i, (key, label) in enumerate(fields):
            var = tk.StringVar()
            self.setting_vars[key] = var
            ttk.Label(form, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=6, pady=3)
            ttk.Entry(form, textvariable=var, width=18).grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=6)
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text="Load", command=self.load_settings).pack(side="left")
        ttk.Button(bar, text="Save limits", command=self.save_settings).pack(side="left", padx=6)

        live = ttk.LabelFrame(tab, text="Real money. Paper is the default. Switching to live sends orders to the "
                                         "broker's live account.")
        live.pack(fill="x", padx=8, pady=6)
        self.live_var = tk.StringVar(value="Account mode: paper, until you load or connect")
        ttk.Label(live, textvariable=self.live_var).pack(anchor="w", padx=6, pady=4)
        row = ttk.Frame(live)
        row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(row, text="Switch to LIVE", command=self.switch_live).pack(side="left")
        ttk.Button(row, text="Back to paper", command=self.switch_paper).pack(side="left", padx=6)
        self.auto_live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Let auto mode trade the live account", variable=self.auto_live_var,
                        command=self.arm_auto).pack(side="left", padx=12)

    def _symbols_from_config(self):
        cfg = bot.read_json(os.path.join(self.home, "trader", "bot.json"), required=False) or {}
        return ",".join(cfg.get("symbols") or ["TSLA"])

    def _refresh_header(self):
        cfg = bot.read_json(os.path.join(self.home, "trader", "config.json"), required=False) or {}
        paper = cfg.get("paper", True) if cfg else True
        self.title_var.set("Day Trader Bot %s" % VERSION)
        if not cfg:
            self.account_var.set("no config yet: the first run writes the example limits (paper)")
            self.account_lbl.configure(foreground="#555")
        elif paper:
            self.account_var.set("PAPER account, budget $%s, max loss $%s a trade" % (
                format(cfg.get("budget", 0), ",.0f"), cfg.get("max_loss_per_trade_usd", "?")))
            self.account_lbl.configure(foreground="#1b7f3b")
        else:
            bcfg = bot.read_json(os.path.join(self.home, "trader", "bot.json"), required=False) or {}
            armed = bool((bcfg.get("execution") or {}).get("allow_auto_on_live")
                         or (bcfg.get("mt5") or {}).get("allow_auto_on_real"))
            self.account_var.set("LIVE account, real money: auto mode is %s" % ("armed" if armed else "off"))
            self.account_lbl.configure(foreground="#b00020")

    def _explain_mode(self):
        text = {
            "dry-run": "dry-run: the whole method runs and nothing is placed.",
            "confirm": "confirm: each READY ticket is shown here and waits for your typed yes or no in the Reply box.",
            "auto": "auto: the bot says yes itself. Paper accounts only, until Run live, auto arms the live "
                    "account. That button then places orders on the live market with real money.",
        }[self.mode_var.get()]
        if self.broker_var.get() == "mt5":
            text += ("  MT5: the open terminal is the broker and the candle feed; the prop firm's daily loss and "
                     "drawdown limits are enforced on top. Save the login on the Broker tab, or set MT5_PASSWORD.")
        elif self.broker_var.get() == "alpaca":
            text += "  Alpaca: paper keys until Settings switches this to the live account. Save them on the Broker tab."
        self.mode_help.set(text)

    # ---- log

    def append(self, text, tag=None):
        self.log.configure(state="normal")
        if tag:
            self.log.insert("end", text, tag)
        else:
            self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _paper(self):
        cfg = bot.read_json(os.path.join(self.home, "trader", "config.json"), required=False) or {}
        if not cfg:
            return True
        return bool(cfg.get("paper", True))

    def _json_from(self, text):
        start = text.find("{")
        if start < 0:
            return None
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            return None

    def _ask_phrase(self):
        got = simpledialog.askstring(
            "Real money",
            "This uses a LIVE brokerage account. Real money, real losses. Most people who day trade lose money.\n\n"
            "Type %s to continue." % bot.LIVE_PHRASE, parent=self.root)
        if got is None:
            return False
        if got.strip().upper() != bot.LIVE_PHRASE:
            messagebox.showerror("Real money", "The phrase did not match. Nothing was changed.")
            return False
        return True

    def desk(self, argv, stdin_text=None, done=None):
        """One short bot command (account, order, close, settings). The long run stays on Runner."""
        if self.runner and self.runner.alive() or self.desk_busy:
            messagebox.showinfo("Busy", "The bot is already running. Stop it first.")
            return
        self.desk_busy = True
        shown = describe(argv)
        self.status_var.set("running: %s" % shown)
        self.append("\n> %s\n\n" % shown, "prompt")

        def work():
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            kw = {}
            if sys.platform == "win32":
                kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                data = None if stdin_text is None else (stdin_text + "\n").encode("utf-8")
                p = subprocess.run(cli_command() + argv, input=data, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=env, timeout=180, **kw)
                text, code = p.stdout.decode("utf-8", "replace"), p.returncode
            except Exception as e:
                text, code = "trader-bot: %s" % e, 1

            def finish():
                self.desk_busy = False
                self._show(text if text.endswith("\n") else text + "\n")
                self.status_var.set("idle" if code == 0 else "idle (exit code %s)" % code)
                if done:
                    done(code, text)
            self.root.after(0, finish)
        threading.Thread(target=work, daemon=True).start()

    def _base(self, argv):
        return argv + ["--dir", self.home, "--broker", self.broker_var.get(), "--quiet"]

    def connect(self):
        self.desk(self._base(["account", "--json"]), done=self._connected)

    def _connected(self, code, text):
        doc = self._json_from(text)
        if not doc or "account" not in doc:
            self.conn_var.set("not connected")
            return
        self.account_doc = doc
        a = doc["account"]
        kind = "PAPER" if doc.get("paper", True) else "LIVE"
        where = a.get("server") or a.get("id") or doc["broker"]
        self.conn_var.set("%s  %s  %s" % (doc["broker"], kind, where))
        self.balance_var.set("Balance %s" % _money(a.get("balance")))
        self.equity_var.set("Equity %s" % _money(a.get("equity")))
        day = a.get("day_pnl")
        self.day_var.set("Today -" if day is None else "Today %s%s" % ("+" if day >= 0 else "-", _money(abs(day))))
        creds = doc.get("credentials") or {}
        self.cred_var.set("Keys: " + ", ".join("%s %s" % (k.split(".")[-1], v or "missing") for k, v in sorted(creds.items())))
        self.live_var.set("Account mode: %s. Auto mode on a live account is armed on the bot only after you tick it "
                          "and type the phrase." % ("PAPER" if doc.get("paper", True) else "LIVE, real money"))
        self._refresh_header()
        if not doc.get("paper", True):
            self.account_var.set("LIVE account, real money, %s equity %s" % (doc["broker"], _money(a.get("equity"))))
            self.account_lbl.configure(foreground="#b00020")
        self._show_open_rows([_trade_row(p.get("symbol"), p) for p in doc.get("positions") or []])
        self._refresh_trades()
        if self._tape_snap:
            self._draw_chart(self._tape_snap)

    def _show_open_rows(self, rows):
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", tags=(row["result"],), values=(
                row.get("symbol"), row.get("side"), row.get("qty"), _money(row.get("entry")),
                _money(row.get("mark")), _signed_money(row.get("pnl")), row.get("result"),
                _money(row.get("stop")) if row.get("stop") else "-",
                _money(row.get("target")) if row.get("target") else "-"))

    def _show_history_rows(self, rows):
        self.history.delete(*self.history.get_children())
        for row in rows:
            why = {"stop": "stop", "target": "target", "flat_by": "flat at the close"}.get(
                row.get("why") or "", row.get("why") or "-")
            self.history.insert("", "end", tags=(row["result"],), values=(
                row.get("when") or "-", row.get("symbol"), row.get("qty"), _money(row.get("entry")),
                _money(row.get("exit")), why, row.get("result"), _signed_money(row.get("pnl"))))

    def _refresh_trades(self):
        """Read the session files and paint live positions plus closed profit and loss.

        While the bot is running this uses the file it keeps writing, so the Trade
        tab moves with the session. Connect still replaces the live rows with the
        broker's own positions when the bot is idle.
        """
        if self._trade_job is not None:
            try:
                self.root.after_cancel(self._trade_job)
            except tk.TclError:
                pass
            self._trade_job = None
        if self._closing:
            return
        try:
            book = read_trade_book(self.home)
        except OSError:
            book = {"open": [], "closed": [], "open_pnl": 0, "closed_pnl": 0, "wins": 0, "losses": 0}
        if bot.STATE_NOTE and bot.STATE_NOTE != getattr(self, "_state_note", None):
            self._state_note = bot.STATE_NOTE
            self.append(bot.STATE_NOTE + "\n", "bad")
        running = self.runner is not None and self.runner.alive()
        if running or not (self.account_doc and self.account_doc.get("positions") is not None):
            self._show_open_rows(book["open"])
            open_pnl = book["open_pnl"]
            open_n = len(book["open"])
        else:
            open_n = len(self.tree.get_children())
            open_pnl = round(sum(_position_pnl(p) or 0 for p in self.account_doc.get("positions") or []), 2)
        self._show_history_rows(book["closed"])
        if open_n:
            live = "Live: %d open, unrealised %s." % (open_n, _signed_money(open_pnl))
        else:
            live = "No live position."
        if book["closed"]:
            past = " History: %d closed, %d profit, %d loss, realised %s." % (
                len(book["closed"]), book["wins"], book["losses"], _signed_money(book["closed_pnl"]))
        else:
            past = " No closed trades yet."
        self.trade_pnl_var.set(live + past)
        if not self._closing:
            self._trade_job = self.root.after(3000, self._refresh_trades)

    def flatten_all(self):
        account = "PAPER" if self._paper() else "LIVE, real money"
        if not messagebox.askyesno("Flatten all",
                                   "Close every open position at market on the %s %s account, cancel every open "
                                   "order, and take no more entries today?" % (self.broker_var.get(), account)):
            return
        self.desk(self._base(["flatten", "--yes", "--json"]), done=lambda c, t: self.connect())

    def place_order(self):
        sym = self.trade_symbol.get().strip().upper()
        if not sym:
            messagebox.showerror("Order", "A symbol is required.")
            return
        try:
            stop = float(self.trade_stop.get())
        except ValueError:
            messagebox.showerror("Order", "Stop must be a price under the entry.")
            return
        argv = ["order", "--symbol", sym, "--stop", repr(stop), "--json"]
        entry = self.trade_entry.get().strip()
        target = self.trade_target.get().strip()
        qty = self.trade_qty.get().strip()
        if entry:
            argv += ["--entry", entry]
        if target:
            argv += ["--target", target]
        if qty:
            argv += ["--qty", qty]
        if self.trade_dry.get():
            argv.append("--dry-run")
        else:
            if not self._paper():
                if not messagebox.askyesno("Live order",
                                           "Place this bracket order on the LIVE account with real money?"):
                    return
            argv.append("--yes")
        self.desk(self._base(argv), done=lambda c, t: self.connect())

    def close_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Close", "Select a position first.")
            return
        sym = str(self.tree.item(sel[0], "values")[0])
        account = "PAPER" if self._paper() else "LIVE, real money"
        if not messagebox.askyesno("Close %s" % sym, "Close %s at market on the %s account?" % (sym, account)):
            return
        self.desk(self._base(["close", "--symbol", sym, "--yes", "--json"]), done=lambda c, t: self.connect())

    def save_broker(self):
        pairs = []
        for key, var in (("bot.mt5.login", self.mt5_login), ("bot.mt5.server", self.mt5_server),
                         ("bot.mt5.terminal_path", self.mt5_path), ("bot.mt5.symbol_suffix", self.mt5_suffix)):
            val = var.get().strip()
            if val:
                pairs.append("%s=%s" % (key, val))
        secrets = [(name, var.get()) for name, var in (
            ("alpaca.key", self.key_var), ("alpaca.secret", self.secret_var), ("mt5.password", self.mt5_pw))
            if var.get().strip()]
        if not pairs and not secrets:
            messagebox.showinfo("Broker", "Nothing to save. Leave a box blank to keep what is already saved.")
            return

        def secrets_from(i):
            if i >= len(secrets):
                self.key_var.set("")
                self.secret_var.set("")
                self.mt5_pw.set("")
                self.append("Saved. The key boxes are cleared so the values are not left on screen.\n", "good")
                self.connect()
                return
            name, value = secrets[i]
            self.desk(["creds", "--set", name, "--dir", self.home, "--json"], stdin_text=value,
                      done=lambda code, text, i=i: secrets_from(i + 1) if code == 0 else None)

        if pairs:
            self.desk(["settings", "--dir", self.home] + sum([["--set", p] for p in pairs], []),
                      done=lambda code, text: secrets_from(0) if code == 0 else None)
        else:
            secrets_from(0)

    def forget_keys(self):
        if not messagebox.askyesno("Forget keys", "Delete the saved Alpaca keys and the MetaTrader 5 password?"):
            return
        self.desk(["creds", "--clear", "all", "--dir", self.home], done=lambda c, t: self.connect())

    def load_settings(self):
        self.desk(["settings", "--json", "--dir", self.home], done=self._settings_loaded)

    def _settings_loaded(self, code, text):
        doc = self._json_from(text)
        if not doc:
            return
        cfg, bcfg = doc.get("config") or {}, doc.get("bot") or {}
        for key, var in self.setting_vars.items():
            if key.startswith("bot."):
                node = bcfg
                for part in key[4:].split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                var.set("" if node is None else str(node))
            else:
                val = cfg.get(key)
                var.set("" if val is None else str(val))
        mt5 = bcfg.get("mt5") or {}
        self.mt5_login.set("" if not mt5.get("login") else str(mt5.get("login")))
        self.mt5_server.set(mt5.get("server") or "")
        self.mt5_path.set(mt5.get("terminal_path") or "")
        self.mt5_suffix.set(mt5.get("symbol_suffix") or "")
        paper = bool(cfg.get("paper", True))
        self.live_var.set("Account mode: %s." % ("PAPER" if paper else "LIVE, real money"))
        armed = bool((bcfg.get("execution") or {}).get("allow_auto_on_live") or mt5.get("allow_auto_on_real"))
        self.auto_live_var.set(armed)
        self._refresh_header()

    def save_settings(self):
        pairs = []
        for key, var in self.setting_vars.items():
            val = var.get().strip()
            if val:
                pairs.append("%s=%s" % (key, val))
        if not pairs:
            messagebox.showinfo("Settings", "Load first, then change a value.")
            return
        argv = ["settings", "--dir", self.home] + sum([["--set", p] for p in pairs], [])
        self.desk(argv, done=lambda c, t: self.load_settings())

    def run_live_auto(self):
        """Arm the live account and start today's session in auto mode. One confirmation, then no per-order yes."""
        broker = self.broker_var.get()
        if broker == "sim":
            messagebox.showerror("Live market",
                                 "The sim broker is paper only. Pick Alpaca or MT5, save that login on the Broker tab, "
                                 "then press Run live, auto.")
            return
        if (self.runner and self.runner.alive()) or self.desk_busy:
            messagebox.showinfo("Busy", "The bot is already running. Stop it first.")
            return
        where = "Alpaca's live API (api.alpaca.markets, your live keys)" if broker == "alpaca" else (
            "the MetaTrader 5 account the terminal is logged in to, including a REAL account")
        if not messagebox.askyesno("Live market, auto",
                                   "The bot will trade %s by itself today.\n\nBroker: %s.\nSymbols: %s.\n\n"
                                   "It will not ask yes or no for each order. Your loss limits still apply. "
                                   "FLATTEN ALL closes everything. Continue?"
                                   % (where, broker, self.symbols_var.get().strip() or "(from settings)")):
            return
        if not self._ask_phrase():
            return
        self.mode_var.set("auto")
        self._explain_mode()
        self.desk(["live", "--on", "--auto", "on", "--confirm", bot.LIVE_PHRASE, "--json", "--dir", self.home],
                  done=self._live_auto_armed)

    def _live_auto_armed(self, code, text):
        if code != 0:
            self.append("Live auto was not armed. Nothing was started.\n", "bad")
            return
        self.auto_live_var.set(True)
        self._refresh_header()
        self.launch("run", skip_confirm=True)

    def switch_live(self):
        if not messagebox.askyesno("Live account",
                                   "Alpaca orders will go to the live API, and a MetaTrader 5 login may be a REAL "
                                   "account. Confirm mode still asks you for every order. Continue?"):
            return
        if not self._ask_phrase():
            return
        self.desk(["live", "--on", "--confirm", bot.LIVE_PHRASE, "--json", "--dir", self.home],
                  done=lambda c, t: self._refresh_header() or self.load_settings())

    def switch_paper(self):
        self.desk(["live", "--off", "--json", "--dir", self.home],
                  done=lambda c, t: self._refresh_header() or self.load_settings())

    def arm_auto(self):
        on = self.auto_live_var.get()
        if on and not self._ask_phrase():
            self.auto_live_var.set(False)
            return
        argv = ["live", "--auto", "on" if on else "off", "--json", "--dir", self.home]
        if on:
            argv += ["--confirm", bot.LIVE_PHRASE]
        self.desk(argv, done=lambda code, text: self.auto_live_var.set(False) if code != 0 and on else None)

    # ---- actions

    def launch(self, action, skip_confirm=False):
        if (self.runner and self.runner.alive()) or self.desk_busy:
            messagebox.showinfo("Busy", "The bot is already running. Stop it first.")
            return
        mode, broker = self.mode_var.get(), self.broker_var.get()
        try:
            argv = build_argv(action, self.symbols_var.get(), mode, broker, self.scan_var.get(), self.home)
        except ValueError as e:
            messagebox.showerror("Day Trader Bot", str(e))
            return
        if not skip_confirm and action == "run" and mode == "auto" and not self._paper():
            if not messagebox.askyesno("Live account, hands off",
                                       "Auto mode on a LIVE account places orders with real money and does not ask "
                                       "again. It starts only if \"Let auto mode trade the live account\" is ticked "
                                       "on Settings. Continue?"):
                return
        elif action == "run" and mode == "auto" and broker == "alpaca":
            if not messagebox.askyesno("Alpaca, hands off",
                                       "Auto mode on Alpaca places bracket orders on your PAPER account without "
                                       "asking again (keys from ALPACA_API_KEY / ALPACA_SECRET_KEY). Continue?"):
                return
        elif action == "run" and mode == "auto" and broker == "mt5":
            if not messagebox.askyesno("MetaTrader 5, hands off",
                                       "Auto mode on MetaTrader 5 sends market deals with stop-loss and take-profit "
                                       "to the account the open terminal is logged in to, without asking again. "
                                       "The bot stops for the day when the prop firm's daily loss or drawdown limit "
                                       "comes within the buffer, and flattens what it opened. A funded account is "
                                       "not a toy: a breached rule ends it. Continue?"):
                return
        self.append("\n> %s\n\n" % describe(argv), "prompt")
        self.runner = Runner(argv).start()
        self.status_var.set("running: %s" % describe(argv))
        for b in (self.btn_reel, self.btn_run, self.btn_live_auto, self.btn_status):
            b.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.reply_entry.configure(state="normal")
        self.btn_send.configure(state="normal")

    def stop(self):
        if self.runner:
            self.runner.stop()
            self.append("\nStopped. Open positions stay at the broker with their bracket orders; check them.\n",
                        "bad")

    def send(self):
        text = self.reply_var.get()
        if self.runner and self.runner.send(text):
            self.append(text + "\n", "prompt")
            self.reply_var.set("")
            self.reply_lbl.configure(text="Reply")

    def open_settings(self):
        paths = [os.path.join(self.home, "trader", f) for f in ("config.json", "bot.json")]
        if not all(os.path.exists(p) for p in paths):
            bot.main(["init", "--dir", self.home])
            self._refresh_header()
            self.symbols_var.set(self._symbols_from_config())
        if not bot.open_folder(os.path.join(self.home, "trader")):
            messagebox.showinfo("Settings", "Edit these in any text editor:\n\n" + "\n".join(paths))

    # ---- updates

    def check_updates(self, quiet=False):
        """Ask the release list in the background; the result lands in _update_checked."""
        if self.update_busy:
            return
        if self.update_info and not quiet:
            self.install_update()
            return
        url = updater.settings(self.home)["url"]
        self.update_busy = True
        self.btn_update.configure(state="disabled", text="Checking...")

        def work():
            try:
                result = updater.check(url, VERSION)
            except updater.UpdateError as e:
                result = e
            self.root.after(0, lambda: self._update_checked(result, quiet))
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, result, quiet):
        self.update_busy = False
        if isinstance(result, Exception):
            self.btn_update.configure(state="normal", text="Check for updates")
            if not quiet:
                messagebox.showwarning("Updates", "Could not check for updates.\n\n%s" % result)
            return
        if not result["available"]:
            self.btn_update.configure(state="normal", text="Check for updates")
            if not quiet:
                messagebox.showinfo("Updates", "Day Trader Bot %s is the newest version." % VERSION)
            return
        self.update_info = result
        self.btn_update.configure(state="normal", text="Update to %s" % result["latest"])
        self.append("Day Trader Bot %s is available (you have %s). Click \"Update to %s\" at the top right. "
                    "It updates this copy and opens the window again. Your settings stay.\n%s\n"
                    % (result["latest"], VERSION, result["latest"],
                       ("\n" + result["notes"] + "\n") if result["notes"] else ""), "prompt")
        if not quiet:
            self.install_update()

    def install_update(self):
        info = self.update_info
        if not info or self.update_busy:
            return
        if self.runner and self.runner.alive():
            messagebox.showinfo("Updates", "The bot is running. Stop it first, then update.")
            return
        if not messagebox.askyesno("Update to %s" % info["latest"],
                                   "Update this copy from %s to %s (%s MB)?\n\n"
                                   "The window closes for a moment and opens again on %s. "
                                   "Your settings and sessions in %s stay."
                                   % (VERSION, info["latest"], round(int(info["manifest"]["installer"].get("size") or 0)
                                                                     / 1048576.0, 1), info["latest"], self.home)):
            return
        self.update_busy = True
        self.btn_update.configure(state="disabled", text="Downloading...")
        for b in (self.btn_reel, self.btn_run, self.btn_live_auto, self.btn_status):
            b.configure(state="disabled")

        def progress(got, total):
            pct = int(100 * got / total) if total else 0
            self.root.after(0, lambda: self.status_var.set("downloading the update: %d%%" % pct))

        def work():
            try:
                path = updater.download(info["manifest"], progress=progress)
                self.root.after(0, lambda: self._update_ready(path))
            except updater.UpdateError as e:
                self.root.after(0, lambda: self._update_failed(e))
        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, err):
        self.update_busy = False
        self.status_var.set("idle")
        self.btn_update.configure(state="normal", text="Update to %s" % self.update_info["latest"])
        for b in (self.btn_reel, self.btn_run, self.btn_live_auto, self.btn_status):
            b.configure(state="normal")
        messagebox.showerror("Update failed", "Nothing was installed.\n\n%s" % err)

    def _update_ready(self, path):
        self.status_var.set("installing %s" % self.update_info["latest"])
        try:
            updater.install(path, relaunch=updater.app_exe())
        except updater.UpdateError as e:
            self.update_busy = False
            messagebox.showinfo("Installer saved", "%s\n\nRun it yourself: %s" % (e, path))
            self.btn_update.configure(state="normal", text="Update to %s" % self.update_info["latest"])
            for b in (self.btn_reel, self.btn_run, self.btn_live_auto, self.btn_status):
                b.configure(state="normal")
            return
        self.root.after(300, self.root.destroy)

    def close(self):
        self._closing = True
        if self._tape_job is not None:
            try:
                self.root.after_cancel(self._tape_job)
            except tk.TclError:
                pass
            self._tape_job = None
        if self._trade_job is not None:
            try:
                self.root.after_cancel(self._trade_job)
            except tk.TclError:
                pass
            self._trade_job = None
        if self.runner and self.runner.alive():
            if not messagebox.askyesno("Quit", "The bot is running. Stop it and quit?"):
                return
            self.runner.stop()
        self.root.destroy()

    # ---- the pump

    def _drain(self):
        if self.runner:
            finished = False
            while True:
                try:
                    item = self.runner.queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    finished = True
                    break
                self._show(item)
            if finished:
                self._finished()
        self.root.after(100, self._drain)

    def _show(self, text):
        for line in text.splitlines(True):
            tag = None
            if "FILLED" in line or "PASS" in line or "TARGET filled" in line:
                tag = "good"
            elif "BLOCK" in line or "FAILED" in line or "STOP filled" in line or "trader-bot:" in line:
                tag = "bad"
            self.append(line, tag)
            self.tail = line if line.endswith("\n") else self.tail + line
            if line.endswith("\n"):
                self.tail = ""
        if self.tail and looks_like_prompt(self.tail):
            self.reply_lbl.configure(text="Reply (the bot is waiting)")
            self.reply_entry.focus_set()
            self.root.bell()
            if "yes or no" in self.tail:
                self.status_var.set("waiting for your yes or no")

    def _finished(self):
        code = self.runner.returncode
        self.status_var.set("idle" if code == 0 else "idle (exit code %s)" % code)
        self.append("\n[done%s]\n" % ("" if code == 0 else ", exit code %s" % code), "prompt" if code == 0 else "bad")
        for b in (self.btn_reel, self.btn_run, self.btn_live_auto, self.btn_status):
            b.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.reply_entry.configure(state="disabled")
        self.btn_send.configure(state="disabled")
        self.reply_lbl.configure(text="Reply")
        self.tail = ""
        self._refresh_header()


def _ensure_stdio():
    """A windowed exe has no console: stdout and stderr are None, and writing them crashes."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    sink = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def main(argv=None):
    _ensure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    home = None
    if "--dir" in argv:
        i = argv.index("--dir")
        if i + 1 < len(argv):
            home = os.path.abspath(argv[i + 1])
    if "--codegen" in argv:
        import codegen
        return codegen.main(home)
    if tk is None:
        print("Day Trader Bot: tkinter is not installed (python3-tk on Debian/Ubuntu). "
              "The console menu works: bot.py menu", file=sys.stderr)
        return 1
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root, home=home)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
