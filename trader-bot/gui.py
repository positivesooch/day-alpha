#!/usr/bin/env python3
"""Day Trader Bot, the window.

A Tkinter front for trader-bot/bot.py. It never trades by itself: every
button starts the bot as a subprocess (the console exe when installed,
python3 bot.py otherwise) with the same arguments you would type, streams
its output into the log pane, and sends your typed replies to it in confirm
mode. Stop ends the subprocess; open positions stay at the broker with their
bracket orders.

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
    from tkinter import messagebox, scrolledtext, ttk
except ImportError:  # a server without Tk; the console menu still works
    tk = None

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = getattr(sys, "_MEIPASS", None) or (os.path.dirname(sys.executable) if FROZEN else None)
PACK_BOT = os.path.join(BUNDLE, "pack", "trader-bot") if FROZEN else HERE
sys.path.insert(0, PACK_BOT)
import bot  # noqa: E402  (the same module the console runs)

VERSION = bot.VERSION
CLI_NAME = "DayTraderBotCLI.exe" if sys.platform == "win32" else "DayTraderBotCLI"
MODES = ("dry-run", "confirm", "auto")
BROKERS = ("sim", "alpaca")
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
        root.minsize(820, 560)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._refresh_header()
        self.append("Day Trader Bot %s. Education and tooling, not financial advice. Most people who day trade "
                    "lose money.\nData folder: %s\n\nStart with \"Replay the reel day\": the whole method on a "
                    "recorded session, nothing placed.\n\n" % (VERSION, self.home))
        root.after(100, self._drain)

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

        form = ttk.LabelFrame(r, text="What to do")
        form.pack(fill="x", **pad)
        ttk.Label(form, text="Symbols").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.symbols_var = tk.StringVar(value=self._symbols_from_config())
        ttk.Entry(form, textvariable=self.symbols_var, width=36).grid(row=0, column=1, sticky="we", padx=6)
        self.scan_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="let the scanner add opening-range breakouts",
                        variable=self.scan_var).grid(row=0, column=2, columnspan=2, sticky="w", padx=6)
        ttk.Label(form, text="Mode").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.mode_var = tk.StringVar(value="dry-run")
        mode = ttk.Combobox(form, textvariable=self.mode_var, values=MODES, state="readonly", width=12)
        mode.grid(row=1, column=1, sticky="w", padx=6)
        mode.bind("<<ComboboxSelected>>", lambda e: self._explain_mode())
        ttk.Label(form, text="Broker").grid(row=1, column=2, sticky="e", padx=6)
        self.broker_var = tk.StringVar(value="sim")
        ttk.Combobox(form, textvariable=self.broker_var, values=BROKERS, state="readonly",
                     width=10).grid(row=1, column=3, sticky="w", padx=6)
        self.mode_help = tk.StringVar()
        ttk.Label(form, textvariable=self.mode_help, foreground="#555").grid(row=2, column=0, columnspan=4,
                                                                             sticky="w", padx=6, pady=(0, 6))
        form.columnconfigure(1, weight=1)
        self._explain_mode()

        bar = ttk.Frame(r)
        bar.pack(fill="x", **pad)
        self.btn_reel = ttk.Button(bar, text="Replay the reel day", command=lambda: self.launch("reel"))
        self.btn_reel.pack(side="left", padx=(0, 6))
        self.btn_run = ttk.Button(bar, text="Run today, live", command=lambda: self.launch("run"))
        self.btn_run.pack(side="left", padx=(0, 6))
        self.btn_status = ttk.Button(bar, text="Status", command=lambda: self.launch("status"))
        self.btn_status.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Settings folder", command=self.open_settings).pack(side="right")
        ttk.Button(bar, text="Clear log", command=self.clear).pack(side="right", padx=(0, 6))

        self.log = scrolledtext.ScrolledText(r, wrap="word", font=("Consolas" if sys.platform == "win32" else
                                                                   "TkFixedFont", 10), state="disabled",
                                             height=20)
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
            self.account_var.set("LIVE account in your config: auto mode is off")
            self.account_lbl.configure(foreground="#b00020")

    def _explain_mode(self):
        self.mode_help.set({
            "dry-run": "dry-run: the whole method runs and nothing is placed.",
            "confirm": "confirm: each READY ticket is shown here and waits for your typed yes or no in the Reply box.",
            "auto": "auto: the bot says yes itself. Paper accounts only; on a live account it refuses to start.",
        }[self.mode_var.get()])

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

    # ---- actions

    def launch(self, action):
        if self.runner and self.runner.alive():
            messagebox.showinfo("Busy", "The bot is already running. Stop it first.")
            return
        mode, broker = self.mode_var.get(), self.broker_var.get()
        try:
            argv = build_argv(action, self.symbols_var.get(), mode, broker, self.scan_var.get(), self.home)
        except ValueError as e:
            messagebox.showerror("Day Trader Bot", str(e))
            return
        if action == "run" and mode == "auto" and broker == "alpaca":
            if not messagebox.askyesno("Alpaca, hands off",
                                       "Auto mode on Alpaca places bracket orders on your PAPER account without "
                                       "asking again (keys from ALPACA_API_KEY / ALPACA_SECRET_KEY). Continue?"):
                return
        self.append("\n> %s\n\n" % describe(argv), "prompt")
        self.runner = Runner(argv).start()
        self.status_var.set("running: %s" % describe(argv))
        for b in (self.btn_reel, self.btn_run, self.btn_status):
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

    def close(self):
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
        for b in (self.btn_reel, self.btn_run, self.btn_status):
            b.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.reply_entry.configure(state="disabled")
        self.btn_send.configure(state="disabled")
        self.reply_lbl.configure(text="Reply")
        self.tail = ""
        self._refresh_header()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    home = None
    if argv[:1] == ["--dir"] and len(argv) > 1:
        home = os.path.abspath(argv[1])
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
