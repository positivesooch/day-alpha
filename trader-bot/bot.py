#!/usr/bin/env python3
"""trader-bot: the Day Trader skill's tools, run as one loop.

For every symbol on the watch (yours, or the scanner's hits) it runs the
pack's own tools in the pack's own order, every bar:

    scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker

Each stage is the real skill script, called with --json, so every number the
bot acts on is one the skills would have shown you. The bot adds three things
the skills do not have: a clock that waits for the next bar, a broker that
places the ticket (a built-in paper simulator, Alpaca's REST API, or a
MetaTrader 5 terminal with a prop firm's rules enforced on top; see
mt5link.py), and a replay that runs a whole recorded session in a second.

Execution modes, from safest to least:

    dry-run   the full pipeline up to the READY ticket, nothing is placed (default)
    confirm   the ticket is shown, you type yes or no for that one order
    auto      the bot says yes itself: PAPER accounts only, it refuses on live
              (a REAL MetaTrader 5 account needs mt5.allow_auto_on_real as well)

Nothing here is financial advice. Most people who day trade lose money.
"""

import argparse
import datetime
import gzip
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "2.5.1"

# Frozen (the Windows build made by installer/DayTraderBot.spec): the pack's
# folders are bundled under <bundle>/pack, and the exe itself is the Python
# that runs the tool scripts (see run_tool and Tool.run). Otherwise this file
# sits in trader-bot/ inside the pack and the system python3 runs the tools.
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = getattr(sys, "_MEIPASS", None) or (os.path.dirname(sys.executable) if FROZEN else None)
HERE = os.path.join(BUNDLE, "pack", "trader-bot") if FROZEN else os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
TOOLS = {
    "scan": os.path.join(PACK, "trader-scanner", "scan.py"),
    "news": os.path.join(PACK, "trader-news", "news.py"),
    "entry": os.path.join(PACK, "trader-entry", "entry.py"),
    "exit": os.path.join(PACK, "trader-exit", "exit.py"),
    "timemachine": os.path.join(PACK, "trader-timemachine", "timemachine.py"),
    "size": os.path.join(PACK, "trader-budget", "size.py"),
    "check": os.path.join(PACK, "trader-risk", "check.py"),
    "ticket": os.path.join(PACK, "trader-broker", "ticket.py"),
}
CONFIG_EXAMPLE = os.path.join(PACK, "trader-budget", "config.example.json")
BOT_EXAMPLE = os.path.join(HERE, "bot.example.json")
FIXTURES = os.path.join(PACK, "tests", "fixtures")
README = os.path.join(BUNDLE if FROZEN else PACK, "README.md")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import mt5link  # noqa: E402  (the MetaTrader 5 link; standard library until the bot connects)
import update as updater  # noqa: E402  (one-click updates for the installed app)
import vault  # noqa: E402  (broker credentials, encrypted for the Windows user)


def default_home():
    """Where trader/ lives when no --dir is given: the current folder for the
    script, a folder in the user's home for the installed app (Program Files
    is not writable). DAYTRADERBOT_HOME overrides both."""
    env = os.environ.get("DAYTRADERBOT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if FROZEN:
        return os.path.join(os.path.expanduser("~"), "DayTraderBot")
    return os.path.abspath(".")

UA = "day-trader-skill/2.0 (trader-bot; python-urllib)"
CHART_URL = ("https://query2.finance.yahoo.com/v8/finance/chart/%s"
             "?range=%s&interval=%s&includePrePost=%s")
ALPACA_PAPER = "https://paper-api.alpaca.markets"
ALPACA_LIVE = "https://api.alpaca.markets"
ALPACA_DATA = "https://data.alpaca.markets"
LIVE_PHRASE = "TRADE REAL MONEY"
MODES = ("dry-run", "confirm", "auto")
BROKERS = ("sim", "alpaca", "mt5")
DONE = {"expired", "skipped", "blocked", "dry-run", "declined", "failed", "open", "closed", "no-order"}

DEFAULTS = {
    "symbols": ["TSLA"],
    "scan": {"enabled": False, "watchlist": None, "symbols": None, "limit": None, "every_minutes": 5,
             "min_price": 5.0, "min_volume": 500000, "max_candidates": 10},
    "setup": {"kind": "orb", "or_minutes": 15, "interval": "5m", "min_volume_x": None, "stale_minutes": 5},
    "plan": {"stop": "or-mid", "target": "1R"},
    "news": {"enabled": False, "hours": 12, "block_tags": []},
    "timemachine": {"enabled": True, "replays": 10000, "seed": 19, "min_edge_pct": 0.0,
                    "min_history_triggers": 0, "required": True},
    "execution": {"mode": "dry-run", "broker": "sim", "max_open_positions": 1, "fill_wait_seconds": 60,
                  "allow_auto_on_live": False},
    "mt5": json.loads(json.dumps(mt5link.DEFAULTS)),
    "update": dict(updater.DEFAULTS),
    "poll_seconds": 20,
    "bar_grace_seconds": 25,
}


class BotError(Exception):
    pass


class MT5Bridge(object):
    """The MetaTrader 5 link's objects, with its MT5Error turned into the bot's
    BotError so every handler in here sees one exception type."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kw):
            try:
                return attr(*args, **kw)
            except mt5link.MT5Error as e:
                raise BotError(str(e))
        return call


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(et):
    guess = et - datetime.timedelta(hours=-5)
    return et - datetime.timedelta(hours=eastern_offset(guess))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise BotError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ny_text(utc):
    return to_eastern(utc).strftime("%Y-%m-%d %H:%M:%S")


def ny_hhmm(utc):
    return to_eastern(utc).strftime("%H:%M")


def utc_iso(utc):
    return utc.replace(microsecond=0).isoformat() + "Z"


def hhmm_to_utc(day, hhmm):
    h, m = hhmm.split(":")
    return eastern_to_utc(datetime.datetime.combine(day, datetime.time(int(h), int(m))))


def from_iso(text):
    return parse_now(text) if text else None


# ---------------------------------------------------------------- files

def read_json(path, required=True, what="file"):
    if not path or not os.path.exists(path):
        if required:
            raise BotError("no %s at %s" % (what, path))
        return None
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except ValueError as e:
            raise BotError("%s is not valid JSON: %s" % (path, e))


def _scrub(obj):
    """JSON values only. NaN and infinity become null so a save cannot write a file Python will refuse to read."""
    if isinstance(obj, float):
        if obj != obj or obj == float("inf") or obj == float("-inf"):
            return None
        return obj
    if isinstance(obj, dict):
        return dict((k, _scrub(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def write_json(path, doc):
    """Replace path with doc. The temp file is unique, so two saves cannot truncate each other."""
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s-" % os.path.basename(path), suffix=".tmp", dir=folder or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(_scrub(doc), fh, indent=2, allow_nan=False)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _closers(text):
    """The brackets still open in text, or None when a string is unfinished or a bracket mismatches."""
    stack = []
    in_str = False
    esc = False
    for c in text:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            stack.append("}")
        elif c == "[":
            stack.append("]")
        elif c in "}]":
            if not stack or stack[-1] != c:
                return None
            stack.pop()
    if in_str:
        return None
    return "".join(reversed(stack))


def _cut_points(text):
    """Offsets where a complete value ends, so a cut-off tail can be dropped."""
    points = []
    in_str = False
    esc = False
    depth = 0
    for i, c in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            if depth:
                depth -= 1
            points.append(i + 1)
        elif c == "," and depth:
            points.append(i)
    return points


def salvage_json_text(text):
    """A dict from text, or from the longest intact prefix when the tail was cut off. None when nothing is."""
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        doc = json.loads(text)
        return doc if isinstance(doc, dict) else None
    except ValueError as err:
        limit = getattr(err, "pos", None)
        if limit is None:
            limit = len(text)
    prefix = text[:limit]
    points = _cut_points(prefix)
    points.append(len(prefix))
    seen = 0
    tried = set()
    for end in reversed(points):
        if end in tried or end <= 0:
            continue
        tried.add(end)
        seen += 1
        head = prefix[:end].rstrip()
        while head and head[-1] in ",:":
            head = head[:-1].rstrip()
        closers = _closers(head) if head else None
        if closers is None:
            if seen > 80:
                break
            continue
        try:
            doc = json.loads(head + closers)
        except ValueError:
            if seen > 80:
                break
            continue
        if isinstance(doc, dict) and doc:
            return doc
    return None


def _copy_aside(path):
    folder = os.path.dirname(path)
    name = os.path.basename(path) + ".broken"
    dest = os.path.join(folder, name)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(folder, "%s-%d" % (name, n))
        n += 1
    shutil.copy2(path, dest)
    return dest


# Set when a session file was damaged, so the window can say so once.
STATE_NOTE = None


def _note_state(text):
    """Remember text for the window. The windowed exe has no console, so stderr is None."""
    global STATE_NOTE
    STATE_NOTE = text
    err = sys.stderr
    if err is None:
        return
    try:
        err.write("trader-bot: %s\n" % text)
        err.flush()
    except (AttributeError, OSError, ValueError):
        pass


def read_state(path):
    """The session at path. A damaged file is copied aside. Trades that can still
    be read are written back; otherwise the bad file is moved aside and this
    returns None so the bot can start a fresh session."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    except ValueError as e:
        dest = _copy_aside(path)
        try:
            os.remove(path)
        except OSError:
            pass
        _note_state("%s could not be read (%s). It was moved to %s. The bot will start a fresh session. "
                    "Check open positions at the broker before you run it." % (path, e, dest))
        return None
    try:
        doc = json.loads(text)
    except ValueError as e:
        dest = _copy_aside(path)
        salvaged = salvage_json_text(text)
        keep = isinstance(salvaged, dict) and any(
            k in salvaged for k in ("positions", "candidates", "events", "date", "created_by"))
        if keep:
            try:
                write_json(path, salvaged)
            except OSError:
                keep = False
        if keep:
            _note_state("%s is not valid JSON (%s). A copy is at %s. The readable trades were kept."
                        % (path, e, dest))
            return salvaged
        try:
            os.remove(path)
        except OSError:
            pass
        _note_state("%s is not valid JSON (%s). It was moved to %s. The bot will start a fresh session. "
                    "Check open positions at the broker before you run it." % (path, e, dest))
        return None
    return doc if isinstance(doc, dict) else None


def merged(defaults, user):
    out = {}
    for k, v in defaults.items():
        if isinstance(v, dict) and isinstance((user or {}).get(k), dict):
            out[k] = merged(v, user[k])
        elif user and k in user:
            out[k] = user[k]
        else:
            out[k] = json.loads(json.dumps(v))
    for k, v in (user or {}).items():
        if k not in out:
            out[k] = v
    return out


def money(x):
    return "$%s" % format(round(float(x), 2), ",.2f")


# ---------------------------------------------------------------- bars

def parse_chart(doc):
    """Yahoo v8 chart JSON -> {"symbol", "bars": [{"t" (naive UTC start), "o","h","l","c","v"}], "meta"}."""
    try:
        res = doc["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        err = (doc or {}).get("chart", {}).get("error") if isinstance(doc, dict) else None
        raise BotError("chart has no result%s" % (": %s" % err.get("description") if err else ""))
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        bars.append({"t": datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)),
                     "o": q["open"][i] if q["open"][i] is not None else c,
                     "h": q["high"][i] if q["high"][i] is not None else c,
                     "l": q["low"][i] if q["low"][i] is not None else c,
                     "c": c, "v": q["volume"][i] or 0})
    meta = res.get("meta") or {}
    return {"symbol": (meta.get("symbol") or "").upper(), "bars": bars, "meta": meta}


def chart_request_url(symbol, interval="5m", span="1d", prepost=False):
    """Yahoo v8 chart URL. The bot's own bars stay on the regular session (prepost off, one day)."""
    yahoo = symbol.upper().replace(".", "-")
    return CHART_URL % (urllib.parse.quote(yahoo, safe=""), span, interval, "true" if prepost else "false")


def fetch_chart(symbol, interval="5m", span="1d", prepost=False):
    req = urllib.request.Request(chart_request_url(symbol, interval, span, prepost),
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BotError("Yahoo chart %s: HTTP %s" % (symbol, e.code))
    except (urllib.error.URLError, OSError) as e:
        raise BotError("Yahoo chart %s: %s" % (symbol, e))


class BarSource(object):
    """Where today's bars come from: Yahoo (live), one chart file, a folder of
    <SYM>.json files, or a scanner "wall" (one JSON of {SYM: chart}).

    entry_path(sym) is what trader-entry's --offline takes (None means live)."""

    def __init__(self, path=None, workdir=None, interval="5m", fallback=None):
        self.path, self.workdir, self.interval = path, workdir, interval
        self.fallback = fallback
        self.kind = "live"
        self.wall = None
        self.cache = {}
        if path:
            if os.path.isdir(path):
                self.kind = "dir"
            else:
                doc = read_json(path, what="bars")
                if isinstance(doc, dict) and "chart" in doc:
                    self.kind = "file"
                    self.cache[parse_chart(doc)["symbol"] or "?"] = doc
                    self.file_symbol = parse_chart(doc)["symbol"]
                else:
                    self.kind = "wall"
                    self.wall = doc

    def describe(self):
        text = {"live": "Yahoo Finance, 5-minute bars (free, unofficial, can lag)", "file": self.path,
                "dir": self.path, "wall": self.path}[self.kind]
        if self.fallback:
            text += " (other symbols: %s)" % self.fallback.describe()
        return text

    def has(self, sym):
        sym = sym.upper()
        if self.kind == "live":
            return True
        if self.kind == "file":
            return sym == self.file_symbol
        if self.kind == "dir":
            return any(os.path.exists(os.path.join(self.path, sym + ext)) for ext in (".json", ".json.gz"))
        return sym in self.wall

    def raw(self, sym, fresh=False):
        sym = sym.upper()
        if not self.has(sym) and self.fallback:
            return self.fallback.raw(sym, fresh=fresh)
        if self.kind == "live":
            if fresh or sym not in self.cache:
                self.cache[sym] = fetch_chart(sym, self.interval)
            return self.cache[sym]
        if self.kind == "file":
            if sym != self.file_symbol:
                raise BotError("the bars file is for %s, not %s" % (self.file_symbol, sym))
            return self.cache[sym]
        if self.kind == "dir":
            for name in (sym + ".json", sym + ".json.gz"):
                p = os.path.join(self.path, name)
                if os.path.exists(p):
                    return read_json(p)
            raise BotError("no %s.json in %s" % (sym, self.path))
        if sym not in self.wall:
            raise BotError("%s is not in %s" % (sym, self.path))
        return self.wall[sym]

    def entry_path(self, sym):
        sym = sym.upper()
        if not self.has(sym) and self.fallback:
            return self.fallback.entry_path(sym)
        if self.kind == "live":
            return None
        if self.kind in ("file", "dir"):
            return self.path
        folder = os.path.join(self.workdir, "bars")
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, sym + ".json")
        if not os.path.exists(p):
            write_json(p, self.raw(sym))
        return folder

    def bars(self, sym, now, fresh=False):
        """Closed bars (start + interval <= now), oldest first, plus the last known tick."""
        chart = parse_chart(self.raw(sym, fresh=fresh))
        minutes = 1 if self.interval == "1m" else 5
        step = datetime.timedelta(minutes=minutes)
        # Yahoo appends the live tick (10:49:45) as if it were a bar; a real bar starts on the interval.
        aligned = [b for b in chart["bars"] if b["t"].second == 0 and b["t"].minute % minutes == 0]
        ticks = [b for b in chart["bars"] if b not in aligned and b["t"] <= now]
        closed = [b for b in aligned if b["t"] + step <= now]
        last_tick = None
        rmt = chart["meta"].get("regularMarketTime")
        if rmt:
            t = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(rmt))
            if t <= now:
                last_tick = {"t": t, "price": chart["meta"].get("regularMarketPrice")}
        if ticks and (last_tick is None or last_tick.get("price") is None):
            last_tick = {"t": ticks[-1]["t"], "price": ticks[-1]["c"]}
        return closed, last_tick


class HistorySource(object):
    """The 60-day 5-minute history the Time Machine replays: one file, or a folder of <SYM>.json(.gz)."""

    def __init__(self, path=None):
        self.path = path

    def path_for(self, sym):
        if not self.path:
            return None
        if os.path.isdir(self.path):
            for name in (sym + ".json.gz", sym + ".json", sym + "-60d5m.json.gz", sym + "-60d5m.json"):
                p = os.path.join(self.path, name)
                if os.path.exists(p):
                    return p
            return ""
        doc = read_json(self.path, what="history")
        if parse_chart(doc)["symbol"] not in (sym.upper(), ""):
            return ""
        return self.path


# ---------------------------------------------------------------- the skills as subprocesses

def tool_command(script):
    """The command that runs one of the pack's scripts: python3 when this is a
    script, the frozen exe itself (which answers --tool) when it is the app."""
    if FROZEN:
        return [sys.executable, "--tool", script]
    return [sys.executable, script]


def run_tool(script, args):
    """Run a pack script in this process as if it were `python3 script args`.
    The frozen exe calls itself this way, so the tools need no Python installed."""
    if not os.path.exists(script):
        print("trader-bot: no tool at %s" % script, file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass
    sys.argv = [script] + list(args)
    sys.path.insert(0, os.path.dirname(script))
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        code = e.code
        return 0 if code is None else (code if isinstance(code, int) else 1)
    return 0


class Tool(object):
    def __init__(self, workdir, now_fn, log):
        self.workdir, self.now_fn, self.log = workdir, now_fn, log

    def run(self, name, args, pin_now=True, now_iso=False, want_json=True):
        cmd = tool_command(TOOLS[name]) + [str(a) for a in args]
        if pin_now and self.now_fn() is not None:
            now = self.now_fn()
            cmd += ["--now", utc_iso(now) if now_iso else ny_text(now)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.run(cmd, cwd=self.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        if not want_json:
            return p.returncode, out, err
        data = None
        if out.strip():
            try:
                data = json.loads(out)
            except ValueError:
                start = out.find("{")
                try:
                    data = json.loads(out[start:]) if start >= 0 else None
                except ValueError:
                    data = None
        if data is None and p.returncode != 0:
            raise BotError("%s failed: %s" % (os.path.basename(TOOLS[name]), (err or out).strip()[-400:]))
        return p.returncode, data, err


# ---------------------------------------------------------------- brokers

class SimBroker(object):
    """A paper broker inside the bot. Fills a market buy at the latest known
    price, then walks the bracket bar by bar: a bar whose low touches the stop
    fills the stop (at the open when it gaps through), a bar whose high reaches
    the target fills the target. If one bar does both, the stop wins: the sim
    never gives itself the benefit of the doubt."""

    name = "sim"

    def __init__(self, bars):
        self.bars = bars
        self.n = 0

    def place(self, order, now):
        closed, tick = self.bars.bars(order["symbol"], now, fresh=True)
        price = None
        if tick and tick.get("price") and closed and tick["t"] >= closed[-1]["t"]:
            price = float(tick["price"])
        elif closed:
            price = float(closed[-1]["c"])
        if price is None:
            raise BotError("no price to fill %s" % order["symbol"])
        self.n += 1
        return {"broker_id": "sim-%d" % self.n, "filled_qty": int(float(order["qty"])),
                "filled_price": round(price, 2), "at": now}

    def watch(self, pos, now):
        closed, _ = self.bars.bars(pos["symbol"], now, fresh=True)
        entry_t = from_iso(pos.get("entry_bar_t")) or (parse_now(pos["entry_time"]) - datetime.timedelta(minutes=5))
        since = [b for b in closed if b["t"] > entry_t]
        for b in since:
            if b["l"] <= pos["stop"]:
                px = b["o"] if b["o"] < pos["stop"] else pos["stop"]
                return {"reason": "stop", "price": round(float(px), 2), "at": b["t"] + datetime.timedelta(minutes=5)}
            if b["h"] >= pos["target"]:
                px = b["o"] if b["o"] > pos["target"] else pos["target"]
                return {"reason": "target", "price": round(float(px), 2), "at": b["t"] + datetime.timedelta(minutes=5)}
        if closed:
            pos["last_bar_t"] = utc_iso(closed[-1]["t"])
            pos["mark"] = round(float(closed[-1]["c"]), 2)
        return None

    def flatten(self, pos, now):
        closed, tick = self.bars.bars(pos["symbol"], now, fresh=True)
        price = float(tick["price"]) if tick and tick.get("price") else (float(closed[-1]["c"]) if closed else None)
        if price is None:
            raise BotError("no price to flatten %s" % pos["symbol"])
        return {"reason": "flat_by", "price": round(price, 2), "at": now}


class AlpacaBroker(object):
    """Alpaca's REST API, straight from the bot. Keys from the environment
    (ALPACA_API_KEY, ALPACA_SECRET_KEY, the same names the MCP server uses),
    never from a file in the repo and never printed. Paper unless the pack's
    config says "paper": false."""

    name = "alpaca"

    def __init__(self, paper, key=None, secret=None):
        self.paper = paper
        self.base = ALPACA_PAPER if paper else ALPACA_LIVE
        self.key = key or os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
        self.secret = secret or os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
        if not self.key or not self.secret:
            raise BotError("Alpaca needs an API key and secret: save them on the window's Broker tab, or set "
                           "ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment (%s keys from the Alpaca "
                           "dashboard). Never paste them in a chat." % ("paper" if paper else "live"))

    def call(self, method, path, body=None, base=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request((base or self.base) + path, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise BotError("Alpaca %s %s: HTTP %s %s" % (method, path, e.code, detail))
        except (urllib.error.URLError, OSError) as e:
            raise BotError("Alpaca %s %s: %s" % (method, path, e))

    def account(self):
        return self.call("GET", "/v2/account")

    def positions(self):
        """Every open position at the broker, in the desk's shape."""
        out = []
        for p in self.call("GET", "/v2/positions") or []:
            qty = float(p.get("qty") or 0)
            out.append({"symbol": p.get("symbol"), "side": "short" if qty < 0 or p.get("side") == "short" else "long",
                        "qty": abs(qty), "entry": round(float(p.get("avg_entry_price") or 0), 2),
                        "mark": round(float(p.get("current_price") or 0), 2),
                        "pnl": round(float(p.get("unrealized_pl") or 0), 2),
                        "value": round(float(p.get("market_value") or 0), 2), "broker_id": p.get("asset_id")})
        return out

    def orders(self):
        out = []
        for o in self.call("GET", "/v2/orders?status=open&nested=true&limit=100") or []:
            for leg in [o] + (o.get("legs") or []):
                out.append({"id": leg.get("id"), "symbol": leg.get("symbol"), "side": leg.get("side"),
                            "qty": leg.get("qty"), "type": leg.get("type"), "status": leg.get("status"),
                            "limit": leg.get("limit_price"), "stop": leg.get("stop_price")})
        return out

    def quote(self, symbol):
        """The last trade from Alpaca's data API (free IEX feed)."""
        doc = self.call("GET", "/v2/stocks/%s/trades/latest?feed=iex" % symbol, base=ALPACA_DATA)
        trade = (doc or {}).get("trade") or {}
        return float(trade.get("p") or 0) or None

    def close(self, symbol, now):
        """Close one position at market and cancel its bracket legs."""
        res = self.call("DELETE", "/v2/positions/%s?cancel_orders=true" % symbol)
        oid = res.get("id")
        deadline = time.time() + 60
        while oid and time.time() < deadline and res.get("status") not in ("filled", "canceled", "rejected", "expired"):
            time.sleep(2)
            res = self.call("GET", "/v2/orders/%s" % oid)
        return {"symbol": symbol, "broker_id": oid, "status": res.get("status"),
                "price": round(float(res.get("filled_avg_price") or 0), 2) or None, "at": now}

    def cancel_all(self):
        return len(self.call("DELETE", "/v2/orders") or [])

    def flatten_all(self, now, everything=True):
        """Every position closed at market, every open order cancelled. Returns the symbols."""
        self.cancel_all()
        res = self.call("DELETE", "/v2/positions?cancel_orders=true") or []
        return [r.get("symbol") for r in res if r.get("symbol")]

    def place(self, order, now, wait_seconds=60):
        body = {"symbol": order["symbol"], "qty": str(order["qty"]), "side": order["side"],
                "type": order.get("type", "market"), "time_in_force": order.get("time_in_force", "day")}
        if order.get("limit_price") is not None:
            body["limit_price"] = str(order["limit_price"])
        if order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None:
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": str(order["take_profit_limit_price"])}
            sl = {"stop_price": str(order["stop_loss_stop_price"])}
            if order.get("stop_loss_limit_price") is not None:
                sl["limit_price"] = str(order["stop_loss_limit_price"])
            body["stop_loss"] = sl
        res = self.call("POST", "/v2/orders", body)
        oid = res.get("id")
        deadline = time.time() + wait_seconds
        while time.time() < deadline and res.get("status") not in ("filled", "canceled", "rejected", "expired"):
            time.sleep(2)
            res = self.call("GET", "/v2/orders/%s" % oid)
        if res.get("status") != "filled":
            raise BotError("Alpaca order %s is %s, not filled, after %ds" % (oid, res.get("status"), wait_seconds))
        return {"broker_id": oid, "filled_qty": int(float(res.get("filled_qty") or order["qty"])),
                "filled_price": round(float(res.get("filled_avg_price") or 0), 2),
                "at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)}

    def watch(self, pos, now):
        try:
            self.call("GET", "/v2/positions/%s" % pos["symbol"])
            return None
        except BotError as e:
            if "HTTP 404" not in str(e):
                raise
        closed = self.call("GET", "/v2/orders?status=closed&symbols=%s&limit=50&nested=true" % pos["symbol"])
        for o in closed:
            legs = [o] + (o.get("legs") or [])
            for leg in legs:
                if leg.get("side") == "sell" and leg.get("status") == "filled":
                    px = float(leg.get("filled_avg_price") or 0)
                    reason = "target" if leg.get("type") == "limit" else "stop"
                    return {"reason": reason, "price": round(px, 2), "at": now, "broker_id": leg.get("id")}
        return {"reason": "closed at the broker", "price": pos.get("mark") or pos["entry"], "at": now}

    def flatten(self, pos, now):
        res = self.call("DELETE", "/v2/positions/%s?cancel_orders=true" % pos["symbol"])
        oid = res.get("id")
        deadline = time.time() + 60
        while oid and time.time() < deadline and res.get("status") != "filled":
            time.sleep(2)
            res = self.call("GET", "/v2/orders/%s" % oid)
        px = float(res.get("filled_avg_price") or pos.get("mark") or pos["entry"])
        return {"reason": "flat_by", "price": round(px, 2), "at": now, "broker_id": oid}


# ---------------------------------------------------------------- the bot

class Bot(object):
    def __init__(self, workdir, bot_cfg, mode=None, broker=None, bars=None, history=None, news_dir=None,
                 scan_offline=None, replay=False, reply_fn=None, out=None, quiet=False, mt5=None):
        self.workdir = os.path.abspath(workdir)
        self.trader = os.path.join(self.workdir, "trader")
        self.cfg = read_json(os.path.join(self.trader, "config.json"),
                             what="trader/config.json (run: %s init)" % ("DayTraderBot" if FROZEN else "bot.py"))
        self.bot = merged(DEFAULTS, bot_cfg or {})
        ex = self.bot["execution"]
        self.mode = mode or ex.get("mode") or "dry-run"
        self.broker_name = broker or ex.get("broker") or "sim"
        if self.mode not in MODES:
            raise BotError("mode must be one of %s" % ", ".join(MODES))
        if self.broker_name not in BROKERS:
            raise BotError("broker must be one of %s" % ", ".join(BROKERS))
        self.paper = bool(self.cfg.get("paper", True))
        self.vault = vault.Vault(self.workdir)
        auto_on_real = (self.broker_name == "mt5" and bool(self.bot["mt5"].get("allow_auto_on_real"))) or \
            bool(ex.get("allow_auto_on_live"))
        if self.mode == "auto" and not self.paper and not auto_on_real:
            raise BotError("auto mode is for paper accounts only: trader/config.json says \"paper\": false. "
                           "Use --mode confirm and say yes to each order yourself, or set paper back to true. "
                           "To let the bot trade real money by itself, arm it on purpose: the window's Settings tab "
                           "(\"let auto mode trade the live account\") or execution.allow_auto_on_live true in "
                           "trader/bot.json"
                           + (" (a REAL MetaTrader 5 account also takes mt5.allow_auto_on_real)." if self.broker_name == "mt5"
                              else "."))
        if self.broker_name != "sim" and replay:
            raise BotError("a replay runs on the built-in sim broker, not %s"
                           % {"alpaca": "Alpaca", "mt5": "MetaTrader 5"}[self.broker_name])
        self.replay = replay
        self.reply_fn = reply_fn or (lambda prompt: input(prompt))
        self.out = out or sys.stdout
        self.quiet = quiet
        self.now = None
        self.account_info = None
        self.guard = None
        if self.broker_name == "mt5":
            self.broker, bars, history = self._connect_mt5(bars, history, mt5)
        self.bars = bars or BarSource(None, self.workdir, self.bot["setup"]["interval"])
        self.history = history or HistorySource(None)
        self.news_dir = news_dir
        self.scan_offline = scan_offline
        self.tool = Tool(self.workdir, lambda: self.now if (self.replay or self.now is not None) else None, self.log)
        self.state_path = os.path.join(self.trader, "state", "bot.json")
        self.state = read_state(self.state_path) or {}
        if self.broker_name == "sim":
            self.broker = SimBroker(self.bars)
        elif self.broker_name == "alpaca":
            self.broker = AlpacaBroker(self.paper, key=self.vault.get("alpaca.key"), secret=self.vault.get("alpaca.secret"))
        self.last_scan = None

    def _connect_mt5(self, bars, history, mt5):
        """The MetaTrader 5 terminal as broker (and, unless told otherwise, as the
        source of candles). The account's kind is checked against the config:
        a config that says paper may only drive a demo or contest account, which
        is what prop-firm challenge and funded logins report."""
        mcfg = self.bot["mt5"]
        try:
            link = mt5link.Link(mcfg, mt5=mt5, password=self.vault.get("mt5.password"))
            acct = link.connect()
        except mt5link.MT5Error as e:
            raise BotError(str(e))
        self.account_info = acct
        if self.paper and acct["kind"] == "real":
            link.shutdown()
            raise BotError("trader/config.json says \"paper\": true, but MetaTrader 5 login %s at %s is a REAL "
                           "account. The bot will not treat real money as paper. Set \"paper\": false to run it in "
                           "confirm mode, or log in to the demo / challenge account instead."
                           % (acct.get("login"), acct.get("server")))
        broker = MT5Bridge(mt5link.MT5Broker(mcfg, self.workdir, link=link))
        self.guard = MT5Bridge(broker.inner.guard)
        if bars is None and mcfg.get("use_bars", True):
            bars = MT5Bridge(mt5link.MT5Bars(link, self.workdir, self.bot["setup"]["interval"]))
            if history is None:
                history = mt5link.MT5History(bars, int(mcfg.get("history_days") or 60))
        return broker, bars, history

    # ---- logging and state

    def log(self, text):
        stamp = ny_hhmm(self.now) if self.now else "--:--"
        line = "%s  %s" % (stamp, text)
        self.state.setdefault("events", []).append({"at": ny_text(self.now) if self.now else None, "text": text})
        if not self.quiet:
            self.out.write(line + "\n")
            self.out.flush()

    def note(self, text):
        """Printed, not recorded: the bar-by-bar heartbeat of a live session."""
        if not self.quiet and not self.replay:
            self.out.write("%s  %s\n" % (ny_hhmm(self.now) if self.now else "--:--", text))
            self.out.flush()

    def save(self):
        write_json(self.state_path, self.state)

    def fresh_state(self, day):
        self.state = {"created_by": "trader-bot", "date": day.isoformat(), "mode": self.mode,
                      "broker": self.broker_name, "paper": self.paper, "replay": self.replay,
                      "bars": self.bars.describe(), "candidates": {}, "positions": {}, "events": [],
                      "note": ("Education and tooling, not financial advice. A trade the bot took in dry-run "
                               "or on a paper account is not money made or lost.")}
        self.save()

    def cand(self, sym):
        return self.state["candidates"].setdefault(sym, {"status": "waiting", "stages": {}, "reasons": []})

    def open_positions(self):
        return [p for p in self.state["positions"].values() if p["status"] == "open"]

    # ---- one tick

    def begin_day(self, now):
        """The state belongs to today; a new day starts a fresh one."""
        self.now = now
        day = to_eastern(now).date()
        if self.state.get("date") != day.isoformat():
            self.fresh_state(day)
        return day

    def halt_path(self):
        return os.path.join(self.trader, "state", "halt.json")

    def check_halt_file(self):
        """A kill switch pressed in another process (the window's FLATTEN ALL, bot.py flatten)
        leaves trader/state/halt.json for today: this session takes no more trades."""
        if self.state.get("halted"):
            return
        doc = read_json(self.halt_path(), required=False)
        if doc and doc.get("date") == self.state.get("date"):
            self.state["halted"] = doc.get("reason") or "halted from the desk"
            self.log("HALTED: %s. No more entries today; open positions are watched to the close." % self.state["halted"])

    def tick(self, now):
        self.begin_day(now)
        self.state["last_tick"] = ny_text(now)
        hhmm = ny_hhmm(now)
        flat_by = self.cfg.get("flat_by") or "15:55"
        no_after = self.cfg.get("no_entries_after") or "15:30"

        self.check_halt_file()
        self.prop_emergency(now)
        for pos in self.open_positions():
            self.watch_position(pos, hhmm >= flat_by or bool(self.state.get("halted")))

        if self.state.get("halted"):
            for sym in self.candidates_in_order():
                c = self.cand(sym)
                if c["status"] not in DONE:
                    c["status"] = "blocked"
                    c["reasons"].append("the session is halted: %s" % self.state["halted"])
        elif hhmm < no_after and len(self.open_positions()) < int(self.bot["execution"]["max_open_positions"]):
            self.maybe_scan(now)
            for sym in list(self.candidates_in_order()):
                c = self.cand(sym)
                if c["status"] in DONE:
                    continue
                if len(self.open_positions()) >= int(self.bot["execution"]["max_open_positions"]):
                    break
                try:
                    self.consider(sym, c)
                except BotError as e:
                    c["reasons"].append(str(e))
                    c["status"] = "failed"
                    self.log("%s: %s" % (sym, e))
        elif hhmm >= no_after:
            for sym, c in self.state["candidates"].items():
                if c["status"] not in DONE:
                    c["status"] = "expired"
                    c["reasons"].append("no entries after %s" % no_after)
        self.save()

    def prop_emergency(self, now):
        """On a prop-firm account: equity inside the buffer of a firm rule means
        everything the bot opened is closed at market and the day is over."""
        if self.guard is None or self.state.get("halted"):
            return
        try:
            reason = self.guard.emergency(now)
        except BotError as e:
            self.log("could not read the account for the prop rules: %s" % e)
            return
        if not reason:
            return
        self.state["halted"] = reason
        self.log("PROP RULE EMERGENCY: %s. Closing everything the bot opened and taking no more trades today." % reason)
        try:
            closed = self.broker.flatten_all(now)
            self.log("flattened %d position%s at the broker" % (len(closed), "" if len(closed) == 1 else "s"))
        except BotError as e:
            self.log("could not flatten at the broker: %s. Close the positions in the terminal yourself." % e)

    def candidates_in_order(self):
        order = []
        for s in self.bot.get("symbols") or []:
            s = s.upper()
            self.cand(s)
            order.append(s)
        for s in self.state["candidates"]:
            if s not in order:
                order.append(s)
        return order

    # ---- scanner

    def maybe_scan(self, now):
        sc = self.bot["scan"]
        if not sc.get("enabled"):
            return
        every = datetime.timedelta(minutes=int(sc.get("every_minutes") or 5))
        if self.last_scan is not None and now - self.last_scan < every:
            return
        first_bar = hhmm_to_utc(to_eastern(now).date(), "09:30") + datetime.timedelta(
            minutes=int(self.bot["setup"]["or_minutes"]) + (1 if self.bot["setup"]["interval"] == "1m" else 5))
        if now < first_bar or ny_hhmm(now) < (self.cfg.get("no_entries_before") or "09:45"):
            return
        self.last_scan = now
        args = ["orb", "--json", "--out", "trader/watchlist.json", "--or-minutes", self.bot["setup"]["or_minutes"],
                "--min-price", sc.get("min_price") or 0, "--min-volume", sc.get("min_volume") or 0, "--quiet"]
        if sc.get("watchlist"):
            args += ["--watchlist", sc["watchlist"]]
        elif sc.get("symbols"):
            args += ["--symbols", ",".join(s.upper() for s in sc["symbols"])]
        elif sc.get("limit"):
            args += ["--limit", int(sc["limit"])]
        if self.scan_offline:
            args += ["--offline", self.scan_offline]
        try:
            code, data, err = self.tool.run("scan", args)
        except BotError as e:
            self.log("scanner: %s" % e)
            return
        hits = (data or {}).get("hits") or []
        added = []
        for h in hits[: int(sc.get("max_candidates") or 10)]:
            sym = h["symbol"].upper()
            if sym not in self.state["candidates"]:
                self.cand(sym)["stages"]["scan"] = h
                added.append(sym)
        self.state["scan"] = {"at": ny_text(now), "scanned": (data or {}).get("scanned"), "hits": len(hits),
                              "symbols": [h["symbol"] for h in hits]}
        self.log("scanner: %s charts, %d opening-range breakouts%s" % (
            (data or {}).get("scanned", "?"), len(hits), (": " + ", ".join(added)) if added else ""))

    # ---- the pipeline for one symbol

    def consider(self, sym, c):
        setup = self.bot["setup"]
        args = ["check", sym, "--json", "--write", "trader/entry.json", "--setup", setup["kind"],
                "--or-minutes", setup["or_minutes"], "--interval", setup["interval"]]
        if setup.get("min_volume_x"):
            args += ["--min-volume-x", setup["min_volume_x"]]
        if setup["kind"] == "level":
            level = (c["stages"].get("scan") or {}).get("level") or setup.get("level")
            if not level:
                raise BotError("a level setup needs a level (bot.json setup.level or a scanner hit with one)")
            args += ["--level", level]
        off = self.bars.entry_path(sym)
        if off:
            args += ["--offline", off]
        code, ent, err = self.tool.run("entry", args)
        if not ent:
            raise BotError("entry check printed nothing: %s" % err.strip()[-200:])
        c["stages"]["entry"] = {k: ent.get(k) for k in ("status", "phase", "reason", "level", "trigger",
                                                        "last", "opening_range", "minutes_since")}
        status = ent.get("status")
        if status == "expired":
            c["status"] = "expired"
            c["reasons"].append(ent.get("reason") or "setup expired")
            self.log("%s: expired (%s)" % (sym, ent.get("reason")))
            return
        if status != "triggered":
            c["status"] = "waiting"
            self.note("%s: %s" % (sym, (ent.get("reason") or status).rstrip(".")))
            return
        trig = ent["trigger"]
        self.log("%s: TRIGGERED %s at %s (%s bar closed above %s, +%s%%)" % (
            sym, trig["time"], money(trig["price"]), trig["bar_start"], money(ent["level"]), trig["pct_over"]))
        if (ent.get("minutes_since") or 0) > int(setup.get("stale_minutes") or 5):
            c["status"] = "skipped"
            c["reasons"].append("the trigger is %s minutes old: a late entry is a different trade" % ent["minutes_since"])
            self.log("%s: skipped, the trigger is %s minutes old" % (sym, ent["minutes_since"]))
            return

        if not self.news_ok(sym, c):
            return
        plan = self.plan(sym, c)
        if not self.timemachine_ok(sym, c, plan):
            return
        self.write_price(sym, plan)
        orders = self.size(sym, c)
        if not orders:
            return
        if not self.risk_ok(sym, c):
            return
        if not self.prop_ok(sym, c, orders["orders"][0]):
            return
        ticket = self.ticket(sym, c)
        if not ticket:
            return
        self.execute(sym, c, ticket, orders["orders"][0], plan)

    def news_ok(self, sym, c):
        nw = self.bot["news"]
        if not nw.get("enabled"):
            return True
        args = ["--tickers", sym, "--json", "--hours", nw.get("hours") or 12, "--dry-run", "--no-moves"]
        if not self.cfg.get("sec_user_agent") or "you@example.com" in str(self.cfg.get("sec_user_agent")):
            args.append("--no-sec")
        if self.news_dir:
            args += ["--offline", self.news_dir]
        try:
            code, data, err = self.tool.run("news", args, now_iso=True)
        except BotError as e:
            c["stages"]["news"] = {"error": str(e)}
            self.log("%s: news unavailable (%s), going on without it" % (sym, str(e)[:80]))
            return True
        items = [i for i in (data or {}).get("items", []) if sym in (i.get("touches") or [i.get("ticker")])]
        tags = sorted({t for i in items for t in (i.get("tags") or [])})
        c["stages"]["news"] = {"items": len(items), "tags": tags,
                               "headlines": [i.get("title") for i in items[:5]]}
        self.log("%s: news: %d headline%s%s" % (sym, len(items), "" if len(items) == 1 else "s",
                                                 (", tagged " + ", ".join(tags)) if tags else ""))
        blocked = sorted(set(tags) & {t.lower() for t in nw.get("block_tags") or []})
        if blocked:
            c["status"] = "skipped"
            c["reasons"].append("news tagged %s (bot.json news.block_tags)" % ", ".join(blocked))
            self.log("%s: skipped, the news is tagged %s" % (sym, ", ".join(blocked)))
            return False
        return True

    def plan(self, sym, c):
        p = self.bot["plan"]
        args = ["plan", "--from", "trader/entry.json", "--stop", p.get("stop") or "or-mid",
                "--target", p.get("target") or "1R", "--write", "trader/plan.json", "--json"]
        off = self.bars.entry_path(sym)
        if off and str(p.get("stop", "")).startswith("atr"):
            args += ["--offline", off]
        code, plan, err = self.tool.run("exit", args)
        if not plan or plan.get("stop") is None:
            raise BotError("exit plan failed: %s" % (err or "").strip()[-200:])
        c["stages"]["plan"] = {k: plan.get(k) for k in ("entry", "stop", "target", "risk_per_share", "reward_risk",
                                                        "stop_label", "target_label", "flat_by")}
        self.log("%s: plan: entry %s, stop %s (%s), target %s (%s), %s a share at risk, flat by %s" % (
            sym, money(plan["entry"]), money(plan["stop"]), plan.get("stop_label"), money(plan["target"]),
            plan.get("target_label"), money(plan["risk_per_share"]), plan.get("flat_by")))
        return plan

    def timemachine_ok(self, sym, c, plan):
        tm = self.bot["timemachine"]
        if not tm.get("enabled"):
            return True
        hist = self.history.path_for(sym)
        if hist == "" or (hist is None and self.replay):
            c["stages"]["timemachine"] = {"skipped": "no history for %s" % sym}
            if tm.get("required", True):
                c["status"] = "skipped"
                c["reasons"].append("no 60-day history to replay (timemachine.required is true)")
                self.log("%s: skipped, no history for the Time Machine" % sym)
                return False
            self.log("%s: no history for the Time Machine, going on without it" % sym)
            return True
        args = ["run", "--plan", "trader/plan.json", "--replays", tm.get("replays") or 10000,
                "--seed", tm.get("seed") or 19, "--write", "trader/timemachine.json", "--json"]
        if hist:
            args += ["--offline", hist]
        code, doc, err = self.tool.run("timemachine", args)
        if not doc or "mc" not in doc:
            raise BotError("the Time Machine failed: %s" % (err or "").strip()[-200:])
        mc, hs = doc["mc"], doc.get("history") or {}
        c["stages"]["timemachine"] = {"p_target": mc["p_target"], "p_stop": mc["p_stop"],
                                      "p_neither": mc["p_neither"], "avg_R": mc.get("avg_R"),
                                      "history": {k: hs.get(k) for k in ("sessions", "triggered", "target", "stop",
                                                                         "close", "avg_R")},
                                      "verdict": doc.get("verdict")}
        edge = round(100 * (mc["p_target"] - mc["p_stop"]), 1)
        self.log("%s: time machine: target first %.1f%%, stop first %.1f%%, open at the close %.1f%% "
                 "(%s replays); history: %s of %s fired, %s target / %s stop / %s close. %s" % (
                     sym, 100 * mc["p_target"], 100 * mc["p_stop"], 100 * mc["p_neither"], mc["replays"],
                     hs.get("triggered", "?"), hs.get("sessions", "?"), hs.get("target", "?"),
                     hs.get("stop", "?"), hs.get("close", "?"), doc.get("verdict") or ""))
        if edge < float(tm.get("min_edge_pct") or 0):
            c["status"] = "skipped"
            c["reasons"].append("time machine edge %.1f%% (target first minus stop first) under %.1f%%"
                                % (edge, float(tm.get("min_edge_pct") or 0)))
            self.log("%s: skipped, the odds are not there (edge %.1f%%)" % (sym, edge))
            return False
        if (hs.get("triggered") or 0) < int(tm.get("min_history_triggers") or 0):
            c["status"] = "skipped"
            c["reasons"].append("the setup fired only %s times in the history, under %s"
                                % (hs.get("triggered"), tm["min_history_triggers"]))
            self.log("%s: skipped, too little history" % sym)
            return False
        return True

    def write_price(self, sym, plan):
        """A fresh price in trader/prices.json, so trader-risk measures the risk
        from the higher of the plan's entry and the market now."""
        try:
            closed, tick = self.bars.bars(sym, self.now, fresh=True)
        except BotError as e:
            self.log("%s: no fresh price (%s); trader-risk measures from the plan" % (sym, e))
            return
        price, when = None, None
        if tick and tick.get("price") and (not closed or tick["t"] >= closed[-1]["t"]):
            price, when = float(tick["price"]), tick["t"]
        elif closed:
            price, when = float(closed[-1]["c"]), closed[-1]["t"] + datetime.timedelta(minutes=5)
        if price is None:
            return
        path = os.path.join(self.trader, "prices.json")
        doc = read_json(path, required=False) or {}
        prices, detail = dict(doc.get("prices") or {}), dict(doc.get("detail") or {})
        prices[sym] = round(price, 2)
        detail[sym] = {"time": utc_iso(when), "fetched": utc_iso(self.now), "source": "trader-bot/" + self.bars.kind}
        write_json(path, {"as_of": utc_iso(self.now), "source": "trader-bot", "prices": prices, "detail": detail,
                          "note": "Written by trader-bot from the bars it watches. Your broker's price is the one that fills."})

    def size(self, sym, c):
        code, doc, err = self.tool.run("size", ["--plan", "trader/plan.json", "--write", "trader/orders.json", "--json"])
        orders = (doc or {}).get("orders") or []
        sizing = (doc or {}).get("sizing") or {}
        c["stages"]["size"] = {"qty": sizing.get("qty"), "entry": sizing.get("entry"), "entry_from": sizing.get("entry_from"),
                               "reward_risk": sizing.get("reward_risk"), "max_loss": sizing.get("max_loss"),
                               "bound_by": sizing.get("bound_by"), "refusal": sizing.get("refusal")}
        if not orders:
            c["status"] = "no-order"
            c["reasons"].append(sizing.get("refusal") or (err or "").strip()[-200:] or "the budget sized zero shares")
            self.log("%s: no order: %s" % (sym, c["reasons"][-1]))
            return None
        o = orders[0]
        self.log("%s: budget: %s shares (%s), stop-loss %s, take-profit %s, max loss %s" % (
            sym, o["qty"], money(float(o["qty"]) * float(sizing.get("entry") or o["plan"]["entry"])),
            o.get("stop_loss_stop_price"), o.get("take_profit_limit_price"), money(o["plan"]["max_loss"])))
        return doc

    def risk_ok(self, sym, c):
        code, doc, err = self.tool.run("check", ["--json"])
        results = (doc or {}).get("results") or []
        r = results[0] if results else {}
        c["stages"]["risk"] = {"verdict": r.get("verdict"), "reasons": r.get("reasons"), "warnings": r.get("warnings"),
                               "max_loss": r.get("max_loss"), "reward_risk": r.get("reward_risk"),
                               "fingerprint": r.get("fingerprint")}
        for w in (r.get("warnings") or []) + ((doc or {}).get("warnings") or []):
            self.log("%s: risk warning: %s" % (sym, w))
        if r.get("verdict") != "PASS":
            c["status"] = "blocked"
            c["reasons"].extend(r.get("reasons") or ["trader-risk blocked it"])
            self.log("%s: BLOCK by trader-risk: %s" % (sym, "; ".join(r.get("reasons") or ["no reason given"])))
            return False
        self.log("%s: risk: PASS. %s" % (sym, " ".join(r.get("info") or [])))
        return True

    def prop_ok(self, sym, c, order):
        """The prop firm's rules, measured on the live account, as one more gate
        after trader-risk: the trade's loss at the stop has to fit inside the
        room left under the daily loss limit and the max drawdown, buffer included."""
        if self.guard is None:
            return True
        planned = float((order.get("plan") or {}).get("max_loss") or (c["stages"].get("size") or {}).get("max_loss") or 0)
        ok, reason, snap = self.guard.check(self.now, planned)
        c["stages"]["prop"] = {"ok": ok, "planned_loss": planned,
                               "daily_used": snap["daily_used"], "daily_limit": snap["daily_limit"],
                               "drawdown_used": snap["drawdown_used"], "drawdown_limit": snap["drawdown_limit"],
                               "equity": snap["equity"]}
        if not ok:
            c["status"] = "blocked"
            c["reasons"].append(reason)
            self.log("%s: BLOCK by the prop rules: %s" % (sym, reason))
            return False
        self.log("%s: %s" % (sym, reason))
        return True

    def ticket(self, sym, c):
        code, doc, err = self.tool.run("ticket", ["show", "--index", "1", "--json"])
        if not doc or not doc.get("ready"):
            c["status"] = "blocked"
            c["reasons"].append((doc or {}).get("status") or "the ticket is not ready")
            self.log("%s: ticket not ready: %s" % (sym, c["reasons"][-1]))
            return None
        c["stages"]["ticket"] = {"plain": doc["plain"], "fingerprint": doc["fingerprint"], "paper": doc["paper"],
                                 "tool_input": doc["tool_input"]}
        return doc

    # ---- execution

    def execute(self, sym, c, ticket, order, plan):
        account = "PAPER" if self.paper else "LIVE"
        self.log("%s: READY: %s [%s account, %s]" % (sym, ticket["plain"], account, self.broker_name))
        if self.mode == "dry-run":
            c["status"] = "dry-run"
            c["reasons"].append("dry-run: nothing placed")
            self.log("%s: DRY RUN, nothing placed. The ticket is in trader/orders.json; "
                     "run with --mode confirm to be asked, or --mode auto on a paper account." % sym)
            return
        if self.mode == "confirm":
            if not self.ask(sym, c, ticket):
                return
        else:
            self.record_auto_yes(ticket, order)
            self.log("%s: auto mode on a %s account: the bot says yes to this one order" % (sym, account))
        try:
            if self.broker_name == "sim":
                fill = self.broker.place(ticket["tool_input"], self.now)
            else:
                fill = self.broker.place(ticket["tool_input"], self.now,
                                         wait_seconds=int(self.bot["execution"].get("fill_wait_seconds") or 60))
        except BotError as e:
            c["status"] = "failed"
            c["reasons"].append(str(e))
            self.tool.run("ticket", ["result", "--index", "1", "--status", "failed", "--note", str(e)[:200]],
                          want_json=False)
            self.log("%s: order FAILED: %s" % (sym, e))
            return
        self.tool.run("ticket", ["result", "--index", "1", "--status", "placed", "--broker-id", fill["broker_id"],
                                 "--filled-qty", fill["filled_qty"], "--filled-price", fill["filled_price"]],
                      want_json=False)
        entry_bar_t = None
        try:
            closed, _ = self.bars.bars(sym, self.now)
            entry_bar_t = utc_iso(closed[-1]["t"]) if closed else None
        except BotError:
            pass
        pos = {"symbol": sym, "qty": fill["filled_qty"], "entry": fill["filled_price"], "entry_time": ny_text(self.now),
               "entry_bar_t": entry_bar_t, "stop": float(plan["stop"]), "target": float(plan["target"]),
               "flat_by": plan.get("flat_by") or self.cfg.get("flat_by") or "15:55", "broker_id": fill["broker_id"],
               "status": "open", "mark": fill["filled_price"], "max_loss": round(
                   fill["filled_qty"] * (fill["filled_price"] - float(plan["stop"])), 2)}
        if fill.get("lots") is not None:
            pos.update({"lots": fill["lots"], "mt5_symbol": fill.get("mt5_symbol")})
            if fill.get("risk_usd"):
                pos["max_loss"] = float(fill["risk_usd"])
        self.state["positions"][sym] = pos
        c["status"] = "open"
        for note in fill.get("notes") or []:
            self.log("%s: %s" % (sym, note))
        self.log("%s: FILLED buy %d at %s (%s), bracket at the broker: stop-loss %s, take-profit %s%s" % (
            sym, fill["filled_qty"], money(fill["filled_price"]), fill["broker_id"], money(pos["stop"]),
            money(pos["target"]), " [%s lots of %s]" % (fill["lots"], fill["mt5_symbol"]) if fill.get("lots") else ""))

    def ask(self, sym, c, ticket):
        code, text, err = self.tool.run("ticket", ["show", "--index", "1"], want_json=False)
        self.out.write("\n" + text.strip() + "\n\n")
        self.out.write("trader-bot: a yes here sends this one order to the %s broker (%s account) at once. "
                       "There is no second Allow once / Deny card outside Claude.\n\n"
                       % (self.broker_name, "PAPER" if self.paper else "LIVE"))
        self.out.flush()
        for attempt in range(3):
            try:
                reply = self.reply_fn("Place this order? Reply yes or no: ")
            except EOFError:
                reply = ""
            reply = (reply or "").strip()
            if not reply or reply.lower().split()[0] in ("no", "n", "skip", "pass"):
                self.tool.run("ticket", ["decline", "--index", "1", "--reply", reply or "no"], want_json=False)
                c["status"] = "declined"
                c["reasons"].append("you said no")
                self.log("%s: declined, nothing sent" % sym)
                return False
            code, text, err = self.tool.run("ticket", ["approve", "--index", "1", "--reply", reply], want_json=False)
            if code == 0:
                self.log("%s: your yes is recorded (\"%s\")" % (sym, reply))
                return True
            self.out.write(text.strip() + "\n")
        self.tool.run("ticket", ["decline", "--index", "1", "--reply", "(no clear yes after three tries)"],
                      want_json=False)
        c["status"] = "declined"
        c["reasons"].append("no clear yes after three tries")
        self.log("%s: no clear yes, nothing sent" % sym)
        return False

    def record_auto_yes(self, ticket, order):
        """Auto mode writes its own approval into trader/state/broker.json, in the
        same shape trader-broker writes a human yes, and says plainly that it was
        the bot. ticket.py result then accepts the placement, and trader-risk
        counts the trade's max loss toward the daily limit."""
        path = os.path.join(self.trader, "state", "broker.json")
        state = read_json(path, required=False) or {"orders": []}
        off = eastern_offset(self.now)
        state.setdefault("orders", []).append({
            "at": "%s%+03d:00" % (to_eastern(self.now).isoformat(), off), "date": to_eastern(self.now).date().isoformat(),
            "index": 1, "fingerprint": ticket["fingerprint"], "symbol": order["symbol"], "side": order["side"],
            "qty": order.get("qty"), "kind": "entry", "max_loss": ticket.get("max_loss"), "stop": ticket.get("stop"),
            "plain": ticket["plain"], "status": "approved", "auto": True,
            "reply": "yes (trader-bot auto mode, paper account, bot.json execution.mode=auto; nobody typed this)",
            "paper": self.paper, "broker": self.broker_name})
        write_json(path, state)

    # ---- open positions

    def watch_position(self, pos, past_flat_by):
        sym = pos["symbol"]
        try:
            hit = self.broker.watch(pos, self.now)
            if hit is None and past_flat_by:
                hit = self.broker.flatten(pos, self.now)
        except BotError as e:
            self.log("%s: could not check the position: %s" % (sym, e))
            return
        if hit is None:
            mark = pos.get("mark") or pos["entry"]
            self.note("%s: open, %d shares from %s, marked %s (stop %s, target %s, flat by %s)" % (
                sym, pos["qty"], money(pos["entry"]), money(mark), money(pos["stop"]), money(pos["target"]), pos["flat_by"]))
            return
        self.record_exit(pos, hit)

    def record_exit(self, pos, hit):
        """A position is closed: the state, the ticket's fill record and the log."""
        sym = pos["symbol"]
        if hit.get("price") is None:
            hit["price"] = pos.get("mark") or pos["entry"]
        if hit.get("pnl") is not None:
            pnl = round(float(hit["pnl"]), 2)  # the broker's own number: commission and swap included
        else:
            pnl = round((hit["price"] - pos["entry"]) * pos["qty"], 2)
        pos.update({"status": "closed", "exit": hit["price"], "exit_reason": hit["reason"],
                    "exit_time": ny_text(hit["at"]) if isinstance(hit.get("at"), datetime.datetime) else ny_text(self.now),
                    "pnl": pnl, "r": round(pnl / pos["max_loss"], 2) if pos.get("max_loss") else None})
        self.tool.run("ticket", ["fill", "--symbol", sym, "--side", "sell", "--qty", pos["qty"], "--price", hit["price"]],
                      want_json=False)
        self.state["candidates"][sym]["status"] = "closed"
        label = {"stop": "STOP filled", "target": "TARGET filled",
                 "flat_by": "flat by %s, sold" % pos.get("flat_by")}.get(hit["reason"], hit["reason"])
        self.log("%s: %s at %s: %s%s on %d shares (%s R)" % (
            sym, label, money(hit["price"]), "+" if pnl >= 0 else "-", money(abs(pnl)), pos["qty"], pos["r"]))

    def quote(self, sym):
        """About where the symbol trades now: the broker's last price, else the bars'."""
        try:
            if hasattr(self.broker, "quote"):
                px = self.broker.quote(sym)
                if px:
                    return float(px)
        except BotError as e:
            self.log("%s: the broker has no quote (%s)" % (sym, e))
        try:
            closed, tick = self.bars.bars(sym, self.now, fresh=True)
        except BotError:
            return None
        if tick and tick.get("price"):
            return float(tick["price"])
        return float(closed[-1]["c"]) if closed else None

    # ---- summaries

    def summary(self):
        s = self.state
        lines = ["", "Session %s: %s mode, %s broker, %s account%s" % (
            s.get("date"), s.get("mode"), s.get("broker"), "PAPER" if s.get("paper") else "LIVE",
            " (replay)" if s.get("replay") else "")]
        if s.get("halted"):
            lines.append("  HALTED: %s" % s["halted"])
        for sym, c in s.get("candidates", {}).items():
            why = ("; ".join(c["reasons"])) if c.get("reasons") else ""
            lines.append("  %-6s %-9s %s" % (sym, c["status"], why[:110]))
        total = 0.0
        for sym, p in s.get("positions", {}).items():
            if p["status"] == "closed":
                total += p["pnl"]
                lines.append("  trade  %s: %d x %s -> %s (%s), %s%s" % (
                    sym, p["qty"], money(p["entry"]), money(p["exit"]), p["exit_reason"],
                    "+" if p["pnl"] >= 0 else "-", money(abs(p["pnl"]))))
            else:
                mark = p.get("mark") or p["entry"]
                unreal = round((mark - p["entry"]) * p["qty"], 2)
                lines.append("  open   %s: %d x %s, marked %s, %s%s unrealised" % (
                    sym, p["qty"], money(p["entry"]), money(mark), "+" if unreal >= 0 else "-", money(abs(unreal))))
        closed = [p for p in s.get("positions", {}).values() if p["status"] == "closed"]
        if closed:
            lines.append("  realised %s%s on %d trade%s" % ("+" if total >= 0 else "-", money(abs(total)), len(closed),
                                                             "" if len(closed) == 1 else "s"))
        lines.append("  Not financial advice. Paper or dry-run results are not money.")
        return "\n".join(lines)

    # ---- loops

    def run_replay(self, day, start="09:30", end="16:00", step_minutes=5, stop_when_done=True):
        t = hhmm_to_utc(day, start)
        last = hhmm_to_utc(day, end)
        step = datetime.timedelta(minutes=step_minutes)
        self.fresh_state(day)
        self.log("replay of %s from %s to %s, %s" % (day.isoformat(), start, end, self.bars.describe()))
        while t <= last:
            self.tick(t)
            if stop_when_done and self.done():
                break
            t += step
        self.state["finished"] = ny_text(self.now)
        self.save()
        self.out.write(self.summary() + "\n")
        return self.state

    def done(self):
        waiting = [s for s, c in self.state["candidates"].items() if c["status"] not in DONE]
        scanning = self.bot["scan"].get("enabled") and ny_hhmm(self.now) < (self.cfg.get("no_entries_after") or "15:30")
        if waiting or scanning:
            return False
        for p in self.open_positions():
            last_bar = from_iso(p.get("last_bar_t") or p.get("entry_bar_t"))
            if last_bar is None or self.now - last_bar < datetime.timedelta(minutes=20):
                return False
            self.log("%s: the bars end at %s; the position is still open, marked at %s" % (
                p["symbol"], ny_hhmm(last_bar + datetime.timedelta(minutes=5)), money(p.get("mark") or p["entry"])))
            p["note"] = "open when the data ended"
        return True

    def log_mt5_account(self):
        acct = self.account_info or self.broker.account()
        self.log("MetaTrader 5 %s account %s at %s (%s), %s: balance %s, equity %s, leverage 1:%s" % (
            acct.get("kind", "?").upper(), acct.get("login", "?"), acct.get("server", "?"), acct.get("company", "?"),
            acct.get("currency", "USD"), money(acct.get("balance") or 0), money(acct.get("equity") or 0),
            acct.get("leverage", "?")))
        try:
            s = self.guard.snapshot(self.now or parse_now(None))
        except BotError as e:
            self.log("prop rules: could not read the account (%s)" % e)
            return
        self.log("prop rules: account size %s, daily loss limit %s (%s used today, day start %s), max drawdown %s "
                 "(%s used), %.0f%% buffer under each" % (
                     money(s["initial_balance"]), money(s["daily_limit"]), money(s["daily_used"]), money(s["day_start"]),
                     money(s["drawdown_limit"]), money(s["drawdown_used"]), s["buffer_pct"]))

    def run_live(self, until="16:05"):
        day = to_eastern(parse_now(None)).date()
        if day.weekday() >= 5:
            raise BotError("it is the weekend in New York; the market is closed")
        self.log("live session %s: %s mode, %s broker, %s account. Ctrl-C stops the bot (open positions stay "
                 "at the broker)." % (day.isoformat(), self.mode, self.broker_name, "PAPER" if self.paper else "LIVE"))
        if self.broker_name == "alpaca":
            acct = self.broker.account()
            self.log("Alpaca account %s, %s, equity %s" % (acct.get("account_number", "?"), acct.get("status", "?"),
                                                            money(acct.get("equity") or 0)))
        elif self.broker_name == "mt5":
            self.log_mt5_account()
        step = datetime.timedelta(minutes=1 if self.bot["setup"]["interval"] == "1m" else 5)
        grace = datetime.timedelta(seconds=int(self.bot.get("bar_grace_seconds") or 25))
        poll = int(self.bot.get("poll_seconds") or 20)
        while True:
            now = parse_now(None)
            if ny_hhmm(now) >= until:
                break
            try:
                self.tick(now)
            except BotError as e:
                self.log("tick failed: %s" % e)
            now = parse_now(None)
            secs = step.total_seconds()
            next_bar = now.replace(second=0, microsecond=0) + datetime.timedelta(
                seconds=secs - (now.minute * 60 + now.second) % secs) + grace
            wait = (next_bar - now).total_seconds()
            if self.open_positions():
                wait = min(wait, poll)
            if ny_hhmm(now) < "09:30":
                wait = min(max(wait, 1), 300)
            time.sleep(max(1.0, wait))
        self.state["finished"] = ny_text(parse_now(None))
        self.save()
        self.out.write(self.summary() + "\n")
        return self.state


# ---------------------------------------------------------------- commands

def _workdir(a):
    return os.path.abspath(getattr(a, "dir", None) or default_home())


def _load_bot_cfg(workdir, path=None):
    p = path or os.path.join(workdir, "trader", "bot.json")
    doc = read_json(p, required=False)
    if doc is None:
        doc = read_json(BOT_EXAMPLE)
    return doc


def cmd_init(a):
    workdir = _workdir(a)
    trader = os.path.join(workdir, "trader")
    os.makedirs(os.path.join(trader, "state"), exist_ok=True)
    made = []
    for src, dst in ((CONFIG_EXAMPLE, "config.json"), (BOT_EXAMPLE, "bot.json")):
        target = os.path.join(trader, dst)
        if os.path.exists(target) and not a.force:
            print("kept   %s" % os.path.relpath(target))
            continue
        shutil.copy(src, target)
        made.append(target)
        print("wrote  %s" % os.path.relpath(target))
    print("Edit trader/config.json (budget, max loss, the entry window) and trader/bot.json (symbols, mode).")
    print("Paper trading is on. Try it first on a recorded day: bot.py replay --fixtures")
    return 0


def _ensure_config(workdir):
    """A first run gets the example limits (paper, $5,000 budget, $50 max loss) and is told so."""
    target = os.path.join(workdir, "trader", "config.json")
    if not os.path.exists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy(CONFIG_EXAMPLE, target)
        print("No trader/config.json yet: wrote the example limits to %s (paper trading, $5,000 budget, "
              "$50 max loss a trade). Edit it to taste." % target)
    return target


def _bot_from_args(a, replay, bars=None, history=None, news_dir=None, scan_offline=None, reply_fn=None, out=None):
    workdir = _workdir(a)
    _ensure_config(workdir)
    cfg = _load_bot_cfg(workdir, getattr(a, "bot", None))
    if getattr(a, "symbols", None):
        cfg["symbols"] = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if getattr(a, "scan", False):
        cfg.setdefault("scan", {})["enabled"] = True
    return Bot(workdir, cfg, mode=getattr(a, "mode", None), broker=getattr(a, "broker", None), bars=bars,
               history=history, news_dir=news_dir, scan_offline=scan_offline, replay=replay, reply_fn=reply_fn,
               out=out, quiet=getattr(a, "quiet", False))


def cmd_run(a):
    bot = _bot_from_args(a, replay=False)
    bot.run_live(until=a.until)
    return 0


def cmd_once(a):
    bot = _bot_from_args(a, replay=False)
    now = parse_now(a.now)
    bot.tick(now)
    return 0


def cmd_replay(a):
    workdir = _workdir(a)
    date = a.date
    bars_path, hist_path, news_dir, wall = a.bars, a.history, a.news, a.wall
    if a.fixtures:
        date = date or "2026-10-19"
        bars_path = bars_path or os.path.join(FIXTURES, "trader-entry", "TSLA-2026-10-19-5m.json")
        hist_path = hist_path or os.path.join(FIXTURES, "trader-timemachine", "TSLA-60d5m.json.gz")
        news_dir = news_dir or os.path.join(FIXTURES, "trader-news")
        wall = wall or os.path.join(FIXTURES, "trader-scanner", "wall5m-2026-10-19.json.gz")
    if not date:
        raise BotError("replay wants --date YYYY-MM-DD (or --fixtures for the reel's day)")
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        raise BotError("--date wants YYYY-MM-DD")
    if not bars_path and not wall:
        raise BotError("replay wants --bars (a Yahoo chart JSON, or a folder of SYMBOL.json) or --wall")
    home = _workdir(a)
    workdir = os.path.abspath(a.into) if a.into else os.path.join(home, "trader", "replays", day.isoformat())
    trader = os.path.join(workdir, "trader")
    if a.fresh and os.path.isdir(trader):
        shutil.rmtree(trader)
    os.makedirs(os.path.join(trader, "state"), exist_ok=True)
    src_cfg = os.path.join(home, "trader", "config.json")
    if not os.path.exists(os.path.join(trader, "config.json")):
        shutil.copy(src_cfg if os.path.exists(src_cfg) else CONFIG_EXAMPLE, os.path.join(trader, "config.json"))
    src_bot = a.bot or os.path.join(home, "trader", "bot.json")
    if not os.path.exists(os.path.join(trader, "bot.json")):
        shutil.copy(src_bot if os.path.exists(src_bot) else BOT_EXAMPLE, os.path.join(trader, "bot.json"))
    a.dir = workdir
    if a.fixtures and not a.symbols and not a.scan:
        a.symbols = "TSLA"
    fallback = BarSource(wall, workdir) if (wall and bars_path) else None
    bars = BarSource(bars_path or wall, workdir, fallback=fallback)
    scan_offline = wall or (bars_path if os.path.isdir(bars_path or "") else None)
    if a.scan and not scan_offline:
        raise BotError("--scan in a replay needs --wall (the scanner's recorded charts) or a --bars folder")
    reply_fn = None
    if a.reply is not None:
        replies = iter(a.reply)
        reply_fn = lambda prompt: next(replies, "no")
    bot = _bot_from_args(a, replay=True, bars=bars, history=HistorySource(hist_path), news_dir=news_dir,
                         scan_offline=scan_offline, reply_fn=reply_fn)
    bot.run_replay(day, start=a.start, end=a.end, stop_when_done=not a.whole_day)
    if a.json:
        print(json.dumps(bot.state, indent=2, default=str))
    return 0


def cmd_status(a):
    workdir = _workdir(a)
    path = os.path.join(workdir, "trader", "state", "bot.json")
    st = read_state(path)
    if not st:
        # no live session: the newest replay, if there is one
        replays = os.path.join(workdir, "trader", "replays")
        days = sorted(os.listdir(replays)) if os.path.isdir(replays) else []
        for day in reversed(days):
            alt = os.path.join(replays, day, "trader", "state", "bot.json")
            st = read_state(alt)
            if st:
                if not a.json:
                    print("No live session yet; this is the newest replay (%s):\n" % day)
                break
    if not st:
        print("no session yet (nothing at %s)" % path)
        return 1
    if a.json:
        print(json.dumps(st, indent=2, default=str))
        return 0
    bot = Bot.__new__(Bot)
    bot.state = st
    print(bot.summary().strip())
    if a.events:
        print("")
        for e in st.get("events", [])[-a.events:]:
            print("%s  %s" % ((e.get("at") or "")[11:16], e["text"]))
    return 0


MENU = [
    ("1", "Set up: write trader\\config.json and trader\\bot.json (keeps existing files)", ["init"]),
    ("2", "Replay the reel day (TSLA, Mon 2026-10-19), dry-run: the method, nothing placed",
     ["replay", "--fixtures", "--fresh"]),
    ("3", "Replay the reel day, auto mode: the paper sim takes the trade",
     ["replay", "--fixtures", "--fresh", "--mode", "auto"]),
    ("4", "Replay the reel day with the scanner picking from 231 recorded charts",
     ["replay", "--fixtures", "--fresh", "--scan", "--mode", "auto"]),
    ("5", "Run today, live, dry-run: watch the symbols, place nothing", ["run"]),
    ("6", "Run today, live, confirm mode: you type yes or no for each order", ["run", "--mode", "confirm"]),
    ("7", "Run today, live, auto mode on the paper sim", ["run", "--mode", "auto", "--broker", "sim"]),
    ("8", "Run today, live, auto mode on your Alpaca PAPER account (keys in the environment)",
     ["run", "--mode", "auto", "--broker", "alpaca"]),
    ("9", "Run today, live, auto mode on your MetaTrader 5 prop-firm account (terminal open, MT5_PASSWORD set)",
     ["run", "--mode", "auto", "--broker", "mt5"]),
    ("c", "Run today, live, confirm mode on MetaTrader 5: you type yes or no for each order",
     ["run", "--mode", "confirm", "--broker", "mt5"]),
    ("a", "Account at the broker: balance, equity, open positions", ["account"]),
    ("k", "Kill switch: close every position and halt the day", ["flatten"]),
    ("s", "Status of the current session", ["status", "--events", "15"]),
    ("u", "Check for a newer version and update this copy (one key, the app restarts on the new one)", ["update"]),
    ("e", "Edit the settings (opens the trader folder)", None),
    ("q", "Quit", None),
]


def open_folder(path):
    if hasattr(os, "startfile"):
        os.startfile(path)  # noqa: Windows only
        return True
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener:
        subprocess.Popen([opener, path])
        return True
    return False


def cmd_menu(a, input_fn=None, out=None):
    """The console menu the installed app shows when it is double-clicked."""
    ask = input_fn or input
    out = out or sys.stdout
    home = _workdir(a)
    os.makedirs(os.path.join(home, "trader"), exist_ok=True)
    once = getattr(a, "once", False)
    while True:
        cfg = read_json(os.path.join(home, "trader", "config.json"), required=False) or {}
        out.write("\nDay Trader Bot %s   paper trading%s   data folder: %s\n" % (
            VERSION, "" if cfg.get("paper", True) else " OFF (LIVE account in your config)", home))
        out.write("Education and tooling, not financial advice. Most people who day trade lose money.\n\n")
        for key, label, _ in MENU:
            out.write("  %s  %s\n" % (key, label))
        out.flush()
        try:
            choice = ask("\nChoose: ").strip().lower()
        except EOFError:
            choice = "q"
        if choice in ("q", "quit", "exit", ""):
            return 0
        pick = {k: argv for k, _, argv in MENU}.get(choice)
        if choice == "e":
            paths = [os.path.join(home, "trader", f) for f in ("config.json", "bot.json")]
            if not all(os.path.exists(p) for p in paths):
                main(["init", "--dir", home])
            out.write("\n".join(paths) + "\n")
            if not open_folder(os.path.join(home, "trader")):
                out.write("Open those two files in any text editor.\n")
        elif pick is None:
            out.write("Pick a number from the list, or q.\n")
            continue
        else:
            argv = list(pick) + ["--dir", home]
            if pick[0] == "run" and not getattr(a, "no_prompt", False):
                try:
                    syms = ask("Symbols, comma separated (Enter keeps trader\\bot.json): ").strip()
                except EOFError:
                    syms = ""
                if syms:
                    argv += ["--symbols", syms.upper()]
            out.write("\n> DayTraderBot %s\n\n" % " ".join(argv[:-2]))
            out.flush()
            try:
                code = main(argv)
            except KeyboardInterrupt:
                code = 130
                out.write("\nStopped. Open positions stay at the broker with their bracket orders.\n")
            if code not in (0, None):
                out.write("\n(exit code %s)\n" % code)
        if once:
            return 0
        try:
            ask("\nPress Enter for the menu. ")
        except EOFError:
            return 0


def cmd_gui(a):
    sys.path.insert(0, HERE)
    import gui
    return gui.main(["--dir", _workdir(a)])


def cmd_update(a, input_fn=None, out=None):
    """Check the release manifest; download, verify and run the installer on a yes."""
    out = out or sys.stdout
    ask = input_fn or input
    home = _workdir(a)
    url = getattr(a, "url", None) or updater.settings(home)["url"]
    try:
        info = updater.check(url, VERSION)
    except updater.UpdateError as e:
        raise BotError("update check failed: %s" % e)
    if not info["available"]:
        out.write("Day Trader Bot %s is the newest version (the release list at %s says %s).\n"
                  % (VERSION, url, info["latest"]))
        return 0
    out.write("Day Trader Bot %s is available (you have %s)%s.\n" % (
        info["latest"], VERSION, ", published %s" % info["published"] if info.get("published") else ""))
    if info["notes"]:
        out.write("\n" + info["notes"] + "\n\n")
    if getattr(a, "check", False):
        out.write("Run `update` without --check to update this copy.\n")
        return 0
    if not getattr(a, "yes", False):
        try:
            reply = ask("Update to %s now? Reply yes or no: " % info["latest"])
        except EOFError:
            reply = ""
        if (reply or "").strip().lower() not in ("y", "yes"):
            out.write("Not now. Nothing was downloaded.\n")
            return 0
    last = [0]

    def progress(got, total):
        pct = int(100 * got / total) if total else 0
        if pct >= last[0] + 10 or got == total:
            last[0] = pct
            out.write("  downloaded %s%s\n" % (format(got, ",d"), " of %s (%d%%)" % (format(total, ",d"), pct) if total else ""))
            out.flush()
    try:
        path = updater.download(info["manifest"], getattr(a, "into", None), progress=progress)
        out.write("Verified %s (SHA-256 matches the release list).\n" % os.path.basename(path))
        if getattr(a, "download_only", False):
            out.write("Saved to %s. Double-click it to install.\n" % path)
            return 0
        args = updater.install(path, relaunch=updater.app_exe() if getattr(a, "relaunch", False) else None)
    except updater.UpdateError as e:
        raise BotError(str(e))
    out.write("Updating to %s. This program closes now; the new version opens from the Start menu%s.\n"
              % (info["latest"], " and comes back by itself" if args[0] == "cmd" else ""))
    out.flush()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--tool"]:
        return run_tool(argv[1], argv[2:]) if len(argv) > 1 else 2
    if not argv and FROZEN:
        argv = ["menu"]
    ap = argparse.ArgumentParser(prog="DayTraderBot" if FROZEN else None,
                                 description="trader-bot: the Day Trader skill's tools, run as one loop. "
                                             "Paper by default. Not financial advice.")
    ap.add_argument("--version", action="version", version="Day Trader Bot %s" % VERSION)
    ap.add_argument("--pause", action="store_true", help="wait for Enter before closing (Start menu shortcuts)")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, replay=False):
        p.add_argument("--dir", help="the folder with trader/ in it (default: here)")
        p.add_argument("--bot", help="bot config (default trader/bot.json, else the example)")
        p.add_argument("--mode", choices=MODES, help="dry-run (default), confirm, or auto (paper only)")
        p.add_argument("--symbols", help="comma-separated tickers to watch (overrides bot.json)")
        p.add_argument("--scan", action="store_true", help="add the scanner's opening-range breakouts")
        p.add_argument("--quiet", action="store_true")
        if not replay:
            p.add_argument("--broker", choices=BROKERS,
                           help="sim (built-in paper), alpaca (REST, keys in env) or mt5 (MetaTrader 5 terminal, "
                                "prop-firm rules enforced, MT5_PASSWORD in env)")

    p = sub.add_parser("init", help="write trader/config.json and trader/bot.json from the examples")
    p.add_argument("--dir")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("run", help="watch today's session live, bar by bar")
    common(p)
    p.add_argument("--until", default="16:05", help="stop at this New York time (default 16:05)")
    p = sub.add_parser("once", help="one tick now (for a cron or /loop)")
    common(p)
    p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
    p = sub.add_parser("replay", help="run a recorded session end to end, offline, on the sim broker")
    common(p, replay=True)
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--bars", help="a Yahoo 1d/5m chart JSON for the symbol, or a folder of SYMBOL.json")
    p.add_argument("--wall", help="the scanner's recorded charts ({SYMBOL: chart} JSON, .gz ok)")
    p.add_argument("--history", help="60d/5m chart JSON for the Time Machine, or a folder of SYMBOL.json.gz")
    p.add_argument("--news", help="a folder of saved headlines for trader-news --offline")
    p.add_argument("--fixtures", action="store_true", help="the reel: TSLA on 2026-10-19 from tests/fixtures")
    p.add_argument("--into", metavar="FOLDER", help="the replay's own folder (default <dir>/trader/replays/<date>)")
    p.add_argument("--start", default="09:30")
    p.add_argument("--end", default="16:00")
    p.add_argument("--whole-day", action="store_true", help="keep ticking to --end even when nothing is left to do")
    p.add_argument("--fresh", action="store_true", help="wipe the replay folder first")
    p.add_argument("--reply", action="append", help="scripted answers for confirm mode (repeatable)")
    p.add_argument("--json", action="store_true", help="print the session state at the end")
    p = sub.add_parser("status", help="the current session: candidates, positions, P&L")
    p.add_argument("--dir")
    p.add_argument("--events", type=int, default=0, help="also print the last N log lines")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("menu", help="the console menu (what the console exe opens with)")
    p.add_argument("--dir")
    p.add_argument("--once", action="store_true", help="run one choice and return")
    p.add_argument("--no-prompt", action="store_true", help="do not ask for symbols before a run")
    p = sub.add_parser("gui", help="the window (what the installed app opens with)")
    p.add_argument("--dir")
    desk.add_parsers(sub, common)
    p = sub.add_parser("update", help="check the release list; download, verify and install a newer version")
    p.add_argument("--dir")
    p.add_argument("--url", help="the release manifest (default: update.url in trader/bot.json)")
    p.add_argument("--check", action="store_true", help="only say whether a newer version exists")
    p.add_argument("--yes", action="store_true", help="install without asking")
    p.add_argument("--download-only", action="store_true", help="save the verified installer and stop")
    p.add_argument("--into", metavar="FOLDER", help="where to save the installer (default: the temp folder)")
    p.add_argument("--relaunch", action="store_true", help="reopen the window when the installer is done")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        commands = {"init": cmd_init, "run": cmd_run, "once": cmd_once, "replay": cmd_replay, "status": cmd_status,
                    "menu": cmd_menu, "gui": cmd_gui, "update": cmd_update}
        commands.update(desk.COMMANDS)
        code = commands[a.cmd](a)
    except BotError as e:
        print("trader-bot: %s" % e, file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("\ntrader-bot: stopped. Open positions stay at the broker with their bracket orders; "
              "check them yourself.", file=sys.stderr)
        code = 130
    if a.pause:
        try:
            input("\nPress Enter to close. ")
        except EOFError:
            pass
    return code


# desk.py imports bot. A test loader executes this file under another name, so register
# it before that import or desk would load a second copy and its errors would slip past main.
sys.modules.setdefault("bot", sys.modules[__name__])
import desk  # noqa: E402  (the desk's commands: account, order, close, flatten, settings, live, creds)

if __name__ == "__main__":
    sys.exit(main())
