"""trader-bot's MetaTrader 5 link: a prop-firm account as the bot's broker.

Funded prop firms (FTMO, FundedNext, The5ers, E8 and the like) hand you a
MetaTrader 5 login. This module connects the bot to it through MetaQuotes'
own `MetaTrader5` Python package, which runs on Windows next to an open MT5
terminal. Three jobs:

  MT5Bars    the firm's own 5-minute candles, served to the pack's tools in
             the shape they read (a Yahoo chart document), so entry, exit and
             the Time Machine run unchanged on CFD symbols Yahoo has never
             heard of (TSLA.US, US100, XAUUSD, ...).
  MT5Broker  places the bracket as one market deal with SL and TP on the
             position itself, sized in lots from the symbol's contract spec
             and capped so the loss at the stop fits the plan's max loss;
             watches the position; closes it by the flat-by time.
  PropGuard  the firm's rules as a hard gate: the daily loss limit measured
             from the day's starting balance, the overall drawdown from the
             account size, a safety buffer under each, and an emergency
             flatten when open positions carry the equity near the line.

Standard library only here; `MetaTrader5` is imported when the bot connects.
The password comes from the MT5_PASSWORD environment variable, never from a
file in the repo and never printed.

A funded account is not a toy: a breached rule ends it. Replay and paper
first. Nothing here is financial advice.
"""

import datetime
import json
import os
import time

# MetaTrader5 constants, by value, so the fake module in the tests and a real
# terminal agree (these are stable in the official package).
TRADE_ACTION_DEAL = 1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
TIMEFRAME_M5 = 5
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008
DEAL_ENTRY_OUT = 1
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

DEFAULTS = {
    "terminal_path": None,
    "login": None,
    "server": None,
    "symbols": {},
    "symbol_suffix": "",
    "server_utc_offset_hours": None,
    "magic": 20261019,
    "deviation_points": 20,
    "use_bars": True,
    "history_days": 60,
    "allow_auto_on_real": False,
    "prop": {
        "initial_balance": None,
        "max_daily_loss_pct": 5.0,
        "max_total_drawdown_pct": 10.0,
        "buffer_pct": 20.0,
        "daily_reset_server_time": "00:00",
    },
}


class MT5Error(Exception):
    pass


def load_mt5():
    try:
        import MetaTrader5 as mt5  # noqa: the official package, Windows only
    except ImportError:
        raise MT5Error("the MetaTrader5 package is not installed. It runs on Windows only: "
                       "pip install MetaTrader5, with the MT5 terminal installed and logged in.")
    return mt5


def _get(obj, key, default=None):
    """A field from a MetaTrader5 named tuple, a dict, or a plain object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "_asdict"):
        return obj._asdict().get(key, default)
    return getattr(obj, key, default)


def merged(user):
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in (user or {}).items():
        if k == "prop" and isinstance(v, dict):
            out["prop"].update(v)
        else:
            out[k] = v
    return out


def utc_of(ts):
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc - datetime.datetime(1970, 1, 1)).total_seconds())


# ---------------------------------------------------------------- symbols and lots

def firm_symbol(cfg, ticker):
    """Our ticker -> the firm's symbol: an explicit map first, then a suffix (TSLA -> TSLA.US)."""
    t = ticker.upper()
    m = {str(k).upper(): v for k, v in (cfg.get("symbols") or {}).items()}
    if t in m:
        return m[t]
    return t + (cfg.get("symbol_suffix") or "")


def round_step(value, step, floor=True):
    if not step or step <= 0:
        return value
    n = value / step
    n = int(n + 1e-9) if floor else round(n)
    return round(n * step, 8)


def lots_for(order, info, max_loss=None):
    """Shares in the pack's order -> lots at the firm, capped by the loss at the stop.

    Returns (lots, risk_usd, notes). lots is 0 when even the smallest lot risks
    more than max_loss, or the symbol allows no volume."""
    qty = float(order["qty"])
    contract = float(_get(info, "trade_contract_size", 1) or 1)
    vmin = float(_get(info, "volume_min", 0.01) or 0.01)
    vmax = float(_get(info, "volume_max", 0) or 0)
    step = float(_get(info, "volume_step", vmin) or vmin)
    tick_size = float(_get(info, "trade_tick_size", 0) or _get(info, "point", 0) or 0)
    tick_value = float(_get(info, "trade_tick_value_loss", 0) or _get(info, "trade_tick_value", 0) or 0)
    entry = float(order.get("plan", {}).get("entry") or order.get("limit_price") or 0)
    stop = float(order.get("stop_loss_stop_price") or order.get("plan", {}).get("stop") or 0)
    notes = []
    lots = round_step(qty / contract, step)
    if vmax and lots > vmax:
        lots = vmax
        notes.append("capped at the symbol's max volume %s lots" % vmax)
    per_lot = None
    if entry and stop and tick_size and tick_value:
        per_lot = abs(entry - stop) / tick_size * tick_value  # loss at the stop for one lot, account currency
    elif entry and stop:
        per_lot = abs(entry - stop) * contract
        notes.append("no tick value from the terminal: risk per lot taken as price distance x contract size")
    if per_lot and max_loss is not None and max_loss > 0:
        fit = round_step(max_loss / per_lot, step)
        if fit < lots:
            notes.append("reduced from %s to %s lots so the loss at the stop fits $%.2f" % (lots, fit, max_loss))
            lots = fit
    if lots < vmin:
        risk_min = round(per_lot * vmin, 2) if per_lot else None
        if max_loss is not None and risk_min is not None and risk_min > max_loss:
            return 0.0, risk_min, notes + ["the smallest lot (%s) risks $%.2f at the stop, over the $%.2f max loss"
                                          % (vmin, risk_min, max_loss)]
        if lots <= 0:
            return 0.0, 0.0, notes + ["the order rounds to no volume at all"]
        notes.append("raised to the symbol's minimum %s lots" % vmin)
        lots = vmin
    risk = round(per_lot * lots, 2) if per_lot else None
    return lots, risk, notes


def filling_mode(info):
    flags = int(_get(info, "filling_mode", 0) or 0)
    if flags & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    if flags & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    return ORDER_FILLING_RETURN


# ---------------------------------------------------------------- the connection

class Link(object):
    """One connection to the terminal, shared by the bars, the broker and the guard."""

    def __init__(self, cfg, mt5=None):
        self.cfg = merged(cfg)
        self.mt5 = mt5 or load_mt5()
        self.offset_hours = self.cfg.get("server_utc_offset_hours")
        self.connected = False
        self._info_cache = {}

    def connect(self):
        kw = {}
        if self.cfg.get("terminal_path"):
            kw["path"] = self.cfg["terminal_path"]
        if self.cfg.get("login"):
            pw = os.environ.get("MT5_PASSWORD")
            if not pw:
                raise MT5Error("MT5_PASSWORD is not set. Put the account's password in that environment variable "
                               "(never in a file here, never in a chat), or log the terminal in yourself and "
                               "leave mt5.login empty.")
            kw.update(login=int(self.cfg["login"]), password=pw)
            if self.cfg.get("server"):
                kw["server"] = self.cfg["server"]
        ok = self.mt5.initialize(**kw)
        if not ok:
            raise MT5Error("MetaTrader 5 would not connect: %s. Is the terminal installed, open and logged in?"
                           % (self.mt5.last_error(),))
        self.connected = True
        acct = self.account()
        if not acct:
            raise MT5Error("connected to the terminal, but no account is logged in")
        return acct

    def account(self):
        a = self.mt5.account_info()
        if a is None:
            return None
        mode = int(_get(a, "trade_mode", 0) or 0)
        return {"login": _get(a, "login"), "server": _get(a, "server"), "name": _get(a, "name"),
                "company": _get(a, "company"), "currency": _get(a, "currency", "USD"),
                "balance": float(_get(a, "balance", 0) or 0), "equity": float(_get(a, "equity", 0) or 0),
                "margin_free": float(_get(a, "margin_free", 0) or 0), "leverage": _get(a, "leverage"),
                "trade_mode": mode,
                "kind": {ACCOUNT_TRADE_MODE_DEMO: "demo", ACCOUNT_TRADE_MODE_CONTEST: "contest",
                         ACCOUNT_TRADE_MODE_REAL: "real"}.get(mode, "unknown")}

    def symbol_info(self, fsym, fresh=False):
        if fresh or fsym not in self._info_cache:
            if not self.mt5.symbol_select(fsym, True):
                raise MT5Error("the terminal has no symbol %s (set mt5.symbols or mt5.symbol_suffix in bot.json "
                               "to the firm's name for it)" % fsym)
            info = self.mt5.symbol_info(fsym)
            if info is None:
                raise MT5Error("no symbol info for %s" % fsym)
            self._info_cache[fsym] = info
        return self._info_cache[fsym]

    def tick(self, fsym):
        t = self.mt5.symbol_info_tick(fsym)
        if t is None:
            raise MT5Error("no tick for %s" % fsym)
        return t

    def server_offset(self, fsym=None):
        """Hours the server clock runs ahead of UTC. From the config, else measured
        against a live tick (the terminal stamps everything in server time)."""
        if self.offset_hours is not None:
            return int(self.offset_hours)
        if fsym:
            t = self.tick(fsym)
            stamp = _get(t, "time")
            if stamp:
                now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                self.offset_hours = int(round((utc_of(stamp) - now).total_seconds() / 3600.0))
                return self.offset_hours
        return 0

    def to_utc(self, server_ts, fsym=None):
        return utc_of(int(server_ts) - 3600 * self.server_offset(fsym))

    def shutdown(self):
        if self.connected:
            try:
                self.mt5.shutdown()
            except Exception:
                pass
            self.connected = False


# ---------------------------------------------------------------- bars

def rates_to_chart(ticker, rows, offset_hours, last_price=None, last_time_utc=None, interval="5m"):
    """MT5 M5 rates -> a Yahoo-shaped chart document the pack's tools read.
    rows: (time, open, high, low, close, tick_volume, spread, real_volume) tuples
    or dicts, times in server seconds."""
    ts, o, h, l, c, v = [], [], [], [], [], []
    prev_close, prev_day = None, None
    for r in rows:
        if isinstance(r, dict):
            t, ro, rh, rl, rc, rv = r["time"], r["open"], r["high"], r["low"], r["close"], r.get("tick_volume", 0)
        else:
            t, ro, rh, rl, rc, rv = r[0], r[1], r[2], r[3], r[4], (r[5] if len(r) > 5 else 0)
        t_utc = int(t) - 3600 * offset_hours
        ts.append(t_utc)
        o.append(float(ro))
        h.append(float(rh))
        l.append(float(rl))
        c.append(float(rc))
        v.append(int(rv or 0))
    if ts:
        last_day = (utc_of(ts[-1]) - datetime.timedelta(hours=4)).date()
        for i in range(len(ts) - 1, -1, -1):
            day = (utc_of(ts[i]) - datetime.timedelta(hours=4)).date()
            if day < last_day:
                prev_close = c[i]
                break
    meta = {"symbol": ticker.upper(), "dataGranularity": interval, "exchangeTimezoneName": "America/New_York",
            "timezone": "EDT", "currency": "USD", "source": "MetaTrader 5",
            "regularMarketPrice": last_price if last_price is not None else (c[-1] if c else None),
            "regularMarketTime": ts_of(last_time_utc) if last_time_utc else (ts[-1] + 300 if ts else None),
            "previousClose": prev_close, "chartPreviousClose": prev_close}
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c,
                                                            "volume": v}]}}],
                      "error": None}}


class MT5Bars(object):
    """A BarSource over the terminal: `kind` mt5, charts written to <workdir>/bars
    for the tools' --offline, 60-day history to <workdir>/history for the Time Machine."""

    kind = "mt5"

    def __init__(self, link, workdir, interval="5m"):
        self.link, self.workdir, self.interval = link, workdir, interval
        self.cache = {}
        self.fallback = None

    def describe(self):
        return "MetaTrader 5 candles from %s (%s)" % (self.link.cfg.get("server") or "the terminal",
                                                     self.link.account().get("company") if self.link.connected else "")

    def has(self, sym):
        return True

    def fetch(self, sym, count=None, days=None):
        fsym = firm_symbol(self.link.cfg, sym)
        self.link.symbol_info(fsym)
        if count is None:
            count = int((days or 1) * 24 * 12) + 12
        rows = self.link.mt5.copy_rates_from_pos(fsym, TIMEFRAME_M5, 0, count)
        if rows is None:
            raise MT5Error("no candles for %s: %s" % (fsym, self.link.mt5.last_error()))
        rows = rows.tolist() if hasattr(rows, "tolist") else list(rows)
        off = self.link.server_offset(fsym)
        t = self.link.tick(fsym)
        price = _get(t, "bid") or _get(t, "last")
        stamp = _get(t, "time")
        return rates_to_chart(sym, rows, off, last_price=float(price) if price else None,
                              last_time_utc=self.link.to_utc(stamp, fsym) if stamp else None, interval=self.interval)

    def raw(self, sym, fresh=False):
        sym = sym.upper()
        if fresh or sym not in self.cache:
            self.cache[sym] = self.fetch(sym, days=2)
        return self.cache[sym]

    def entry_path(self, sym):
        """A fresh chart for the tools' --offline, every call: the terminal is the live feed."""
        folder = os.path.join(self.workdir, "bars")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, sym.upper() + ".json"), "w", encoding="utf-8") as fh:
            json.dump(self.raw(sym, fresh=True), fh)
        return folder

    def history_path(self, sym, days=60):
        folder = os.path.join(self.workdir, "history")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sym.upper() + ".json")
        stale = (not os.path.exists(path)) or time.time() - os.path.getmtime(path) > 6 * 3600
        if stale:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.fetch(sym, days=days), fh)
        return path

    def bars(self, sym, now, fresh=False):
        from_chart = self.raw(sym, fresh=fresh)
        res = from_chart["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        minutes = 1 if self.interval == "1m" else 5
        step = datetime.timedelta(minutes=minutes)
        closed = []
        for i, ts in enumerate(res["timestamp"]):
            t = utc_of(ts)
            if t + step <= now and t.minute % minutes == 0:
                closed.append({"t": t, "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": q["close"][i],
                               "v": q["volume"][i]})
        meta = res["meta"]
        tick = None
        if meta.get("regularMarketTime") and meta.get("regularMarketPrice") is not None:
            t = utc_of(meta["regularMarketTime"])
            if t <= now:
                tick = {"t": t, "price": meta["regularMarketPrice"]}
        return closed, tick


class MT5History(object):
    def __init__(self, bars, days=60):
        self.bars, self.days = bars, days

    def path_for(self, sym):
        return self.bars.history_path(sym, self.days)


# ---------------------------------------------------------------- the prop firm's rules

class PropGuard(object):
    """The firm's two limits, measured the way they measure them, with a buffer.

    daily loss   today's starting balance (the higher of balance and equity at
                 the firm's reset time) minus equity now, against
                 initial_balance x max_daily_loss_pct
    drawdown     initial_balance minus equity now, against
                 initial_balance x max_total_drawdown_pct
    buffer_pct   stop trading once this much of a limit is used up, and
                 flatten everything if open positions carry equity past it

    State (day start, initial balance) lives in trader/state/mt5.json so a
    restart mid-day does not forget the morning."""

    def __init__(self, link, cfg, state_path):
        self.link, self.cfg, self.state_path = link, merged(cfg)["prop"], state_path
        self.state = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, encoding="utf-8") as fh:
                    self.state = json.load(fh)
            except (OSError, ValueError):
                self.state = {}

    def _save(self):
        d = os.path.dirname(self.state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)

    def firm_day(self, now_utc):
        """The firm's trading day: server time, rolled at daily_reset_server_time."""
        off = self.link.server_offset()
        server = now_utc + datetime.timedelta(hours=off)
        h, m = (self.cfg.get("daily_reset_server_time") or "00:00").split(":")
        reset = server.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        day = server.date() if server >= reset else (server - datetime.timedelta(days=1)).date()
        return day.isoformat()

    def snapshot(self, now_utc):
        acct = self.link.account()
        if not acct:
            raise MT5Error("the account went away")
        day = self.firm_day(now_utc)
        if not self.state.get("initial_balance"):
            self.state["initial_balance"] = float(self.cfg.get("initial_balance") or acct["balance"])
        elif self.cfg.get("initial_balance"):
            self.state["initial_balance"] = float(self.cfg["initial_balance"])
        if self.state.get("day") != day:
            self.state["day"] = day
            self.state["day_start"] = max(acct["balance"], acct["equity"])
            self.state["day_start_at"] = now_utc.replace(microsecond=0).isoformat() + "Z"
            self._save()
        initial = self.state["initial_balance"]
        daily_limit = initial * float(self.cfg.get("max_daily_loss_pct") or 0) / 100.0
        dd_limit = initial * float(self.cfg.get("max_total_drawdown_pct") or 0) / 100.0
        buffer = float(self.cfg.get("buffer_pct") or 0) / 100.0
        daily_used = round(self.state["day_start"] - acct["equity"], 2)
        dd_used = round(initial - acct["equity"], 2)
        return {"day": day, "balance": acct["balance"], "equity": acct["equity"], "currency": acct["currency"],
                "initial_balance": initial, "day_start": self.state["day_start"],
                "daily_limit": round(daily_limit, 2), "daily_used": daily_used,
                "daily_room": round(daily_limit * (1 - buffer) - daily_used, 2),
                "drawdown_limit": round(dd_limit, 2), "drawdown_used": dd_used,
                "drawdown_room": round(dd_limit * (1 - buffer) - dd_used, 2), "buffer_pct": buffer * 100}

    def check(self, now_utc, planned_loss=0.0):
        """(ok, reason, snapshot) for a new trade whose loss at the stop is planned_loss."""
        s = self.snapshot(now_utc)
        planned = float(planned_loss or 0)
        if s["daily_limit"] and planned > s["daily_room"]:
            return False, ("prop rule: daily loss. $%.2f used of the $%.2f limit today (day start $%.2f, equity "
                           "$%.2f); with the %.0f%% buffer there is $%.2f of room and this trade risks $%.2f"
                           % (s["daily_used"], s["daily_limit"], s["day_start"], s["equity"], s["buffer_pct"],
                              s["daily_room"], planned)), s
        if s["drawdown_limit"] and planned > s["drawdown_room"]:
            return False, ("prop rule: max drawdown. $%.2f used of the $%.2f limit (account size $%.2f, equity "
                           "$%.2f); with the %.0f%% buffer there is $%.2f of room and this trade risks $%.2f"
                           % (s["drawdown_used"], s["drawdown_limit"], s["initial_balance"], s["equity"],
                              s["buffer_pct"], s["drawdown_room"], planned)), s
        return True, ("prop: $%.2f of $%.2f daily loss used, $%.2f of $%.2f drawdown used, equity $%.2f"
                      % (s["daily_used"], s["daily_limit"], s["drawdown_used"], s["drawdown_limit"], s["equity"])), s

    def emergency(self, now_utc):
        """A reason to flatten everything now, or None: equity has crossed the buffer line."""
        s = self.snapshot(now_utc)
        if s["daily_limit"] and s["daily_room"] <= 0:
            return "equity $%.2f is within the buffer of the firm's daily loss limit ($%.2f used of $%.2f)" % (
                s["equity"], s["daily_used"], s["daily_limit"])
        if s["drawdown_limit"] and s["drawdown_room"] <= 0:
            return "equity $%.2f is within the buffer of the firm's max drawdown ($%.2f used of $%.2f)" % (
                s["equity"], s["drawdown_used"], s["drawdown_limit"])
        return None


# ---------------------------------------------------------------- the broker

class MT5Broker(object):
    """The bot's broker interface (place, watch, flatten, account) over the terminal."""

    name = "mt5"

    def __init__(self, cfg, workdir, link=None):
        self.cfg = merged(cfg)
        self.link = link or Link(self.cfg)
        self.workdir = workdir
        self.guard = PropGuard(self.link, self.cfg, os.path.join(workdir, "trader", "state", "mt5.json"))

    def connect(self):
        return self.link.connect() if not self.link.connected else self.link.account()

    def account(self):
        return self.link.account()

    def _result_error(self, res):
        return "retcode %s: %s" % (_get(res, "retcode"), _get(res, "comment") or self.link.mt5.last_error())

    def place(self, order, now, wait_seconds=0, max_loss=None):
        fsym = firm_symbol(self.cfg, order["symbol"])
        info = self.link.symbol_info(fsym, fresh=True)
        digits = int(_get(info, "digits", 2) or 2)
        plan_loss = max_loss if max_loss is not None else (order.get("plan") or {}).get("max_loss")
        lots, risk, notes = lots_for(order, info, plan_loss)
        if lots <= 0:
            raise MT5Error("%s: %s" % (fsym, "; ".join(notes) or "no volume"))
        tick = self.link.tick(fsym)
        side_buy = order.get("side", "buy") == "buy"
        price = float(_get(tick, "ask") if side_buy else _get(tick, "bid"))
        sl = order.get("stop_loss_stop_price")
        tp = order.get("take_profit_limit_price")
        req = {"action": TRADE_ACTION_DEAL, "symbol": fsym, "volume": float(lots),
               "type": ORDER_TYPE_BUY if side_buy else ORDER_TYPE_SELL, "price": price,
               "deviation": int(self.cfg.get("deviation_points") or 20), "magic": int(self.cfg.get("magic") or 0),
               "comment": "trader-bot", "type_time": ORDER_TIME_GTC, "type_filling": filling_mode(info)}
        if sl is not None:
            req["sl"] = round(float(sl), digits)
        if tp is not None:
            req["tp"] = round(float(tp), digits)
        res = self.link.mt5.order_send(req)
        if res is None or int(_get(res, "retcode", 0) or 0) not in (TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED):
            raise MT5Error("MT5 rejected the deal on %s (%s lots): %s" % (fsym, lots, self._result_error(res)))
        filled_lots = float(_get(res, "volume", lots) or lots)
        filled_price = float(_get(res, "price", price) or price)
        contract = float(_get(info, "trade_contract_size", 1) or 1)
        ticket = _get(res, "order") or _get(res, "deal")
        pos_ticket = self._position_ticket(fsym, ticket)
        return {"broker_id": str(pos_ticket or ticket), "filled_qty": int(round(filled_lots * contract)),
                "filled_price": round(filled_price, digits), "at": now, "lots": filled_lots, "mt5_symbol": fsym,
                "risk_usd": risk, "notes": notes, "deal": _get(res, "deal"), "order_ticket": ticket}

    def _position_ticket(self, fsym, order_ticket):
        positions = self.link.mt5.positions_get(symbol=fsym) or []
        for p in positions:
            if int(_get(p, "magic", 0) or 0) == int(self.cfg.get("magic") or 0):
                return _get(p, "ticket")
        return positions and _get(positions[-1], "ticket") or order_ticket

    def _position(self, pos):
        found = self.link.mt5.positions_get(ticket=int(pos["broker_id"]))
        return found[0] if found else None

    def watch(self, pos, now):
        p = self._position(pos)
        fsym = pos.get("mt5_symbol") or firm_symbol(self.cfg, pos["symbol"])
        if p is not None:
            pos["mark"] = round(float(_get(p, "price_current", pos.get("mark") or pos["entry"])), 5)
            pos["floating_pnl"] = round(float(_get(p, "profit", 0) or 0), 2)
            return None
        deals = self.link.mt5.history_deals_get(position=int(pos["broker_id"])) or []
        for d in deals:
            if int(_get(d, "entry", -1)) == DEAL_ENTRY_OUT:
                reason = {DEAL_REASON_SL: "stop", DEAL_REASON_TP: "target"}.get(int(_get(d, "reason", -1)),
                                                                                 "closed at the broker")
                pnl = float(_get(d, "profit", 0) or 0) + float(_get(d, "commission", 0) or 0) + \
                    float(_get(d, "swap", 0) or 0)
                at = _get(d, "time")
                return {"reason": reason, "price": round(float(_get(d, "price", 0) or 0), 5),
                        "at": self.link.to_utc(at, fsym) if at else now, "pnl": round(pnl, 2),
                        "broker_id": str(_get(d, "ticket"))}
        return {"reason": "closed at the broker", "price": pos.get("mark") or pos["entry"], "at": now}

    def flatten(self, pos, now):
        p = self._position(pos)
        if p is None:
            return self.watch(pos, now)
        fsym = _get(p, "symbol") or pos.get("mt5_symbol")
        info = self.link.symbol_info(fsym)
        tick = self.link.tick(fsym)
        long = int(_get(p, "type", 0) or 0) == ORDER_TYPE_BUY
        price = float(_get(tick, "bid") if long else _get(tick, "ask"))
        req = {"action": TRADE_ACTION_DEAL, "symbol": fsym, "volume": float(_get(p, "volume")),
               "type": ORDER_TYPE_SELL if long else ORDER_TYPE_BUY, "position": int(_get(p, "ticket")),
               "price": price, "deviation": int(self.cfg.get("deviation_points") or 20),
               "magic": int(self.cfg.get("magic") or 0), "comment": "trader-bot flat by close",
               "type_time": ORDER_TIME_GTC, "type_filling": filling_mode(info)}
        res = self.link.mt5.order_send(req)
        if res is None or int(_get(res, "retcode", 0) or 0) not in (TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED):
            raise MT5Error("MT5 would not close %s: %s" % (fsym, self._result_error(res)))
        out = self.watch(pos, now)
        if out is None:
            out = {"reason": "flat_by", "price": round(float(_get(res, "price", price) or price), 5), "at": now}
        else:
            out["reason"] = "flat_by"
        return out

    def flatten_all(self, now):
        """Every position with the bot's magic number, closed. Returns the tickets."""
        closed = []
        for p in self.link.mt5.positions_get() or []:
            if int(_get(p, "magic", 0) or 0) != int(self.cfg.get("magic") or 0):
                continue
            fake = {"broker_id": _get(p, "ticket"), "symbol": _get(p, "symbol"), "mt5_symbol": _get(p, "symbol"),
                    "entry": float(_get(p, "price_open", 0) or 0)}
            self.flatten(fake, now)
            closed.append(_get(p, "ticket"))
        return closed
