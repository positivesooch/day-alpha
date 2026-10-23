"""Live prices and the candle layout for the window's Market tab.

Yahoo Finance's chart endpoint is the same free feed the bot already uses for
today's bars. This module turns one of those documents into a quote, and turns
closed bars into candle coordinates the window can draw. Nothing here places
an order.

Standard library only.
"""

import datetime

import bot

# How much history each candle size asks Yahoo for. 1-minute stays on today
# so a refresh stays small; coarser candles need a longer span to fill the chart.
SPANS = {"1m": "1d", "5m": "1d", "15m": "5d", "1h": "1mo"}
INTERVALS = tuple(SPANS)
LEVELS = ("entry", "stop", "target", "mark")
STATES = {
    "PRE": "pre-market",
    "REGULAR": "open",
    "POST": "after hours",
    "POSTPOST": "after hours",
    "PREPRE": "closed",
    "CLOSED": "closed",
}


def split_symbols(text, limit=8):
    """The first `limit` distinct tickers in a comma-separated box."""
    out = []
    for part in (text or "").replace(";", ",").split(","):
        sym = part.strip().upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def price_text(value):
    if value is None or value == "":
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 1:
        return format(v, ",.2f")
    return format(v, ".4f")


def state_text(state):
    return STATES.get((state or "").upper(), state or "")


def change_text(snap):
    change = snap.get("change")
    if change is None:
        return "-"
    sign = "+" if change > 0 else ""
    body = "%s%s" % (sign, price_text(change))
    pct = snap.get("change_pct")
    if pct is None:
        return body
    psign = "+" if pct > 0 else ""
    return "%s (%s%.2f%%)" % (body, psign, pct)


def snapshot(doc):
    """A Yahoo chart document -> the quote the Market tab shows.

    price is the exchange's last print (regularMarketPrice) when Yahoo sends
    one, otherwise the last candle's close. change is against the previous close.
    """
    parsed = bot.parse_chart(doc)
    meta = parsed["meta"]
    bars = parsed["bars"]
    price = meta.get("regularMarketPrice")
    if price is None and bars:
        price = bars[-1]["c"]
    price = float(price) if price is not None else None
    prev = meta.get("chartPreviousClose")
    if prev is None:
        prev = meta.get("previousClose")
    prev = float(prev) if prev is not None else None
    change = (price - prev) if price is not None and prev is not None else None
    pct = (change / prev * 100.0) if change is not None and prev else None
    asof = None
    if meta.get("regularMarketTime"):
        asof = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(meta["regularMarketTime"]))
    return {
        "symbol": parsed["symbol"] or (meta.get("symbol") or "").upper(),
        "price": price,
        "prev_close": prev,
        "change": None if change is None else float(change),
        "change_pct": None if pct is None else float(pct),
        "asof": asof,
        "bars": bars,
        "high": max(b["h"] for b in bars) if bars else None,
        "low": min(b["l"] for b in bars) if bars else None,
        "volume": sum(b.get("v") or 0 for b in bars) if bars else 0,
        "currency": meta.get("currency") or "",
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or "",
        "state": meta.get("marketState") or "",
        "interval": meta.get("dataGranularity") or "",
    }


def headline(snap):
    """One line: TSLA  366.07  +11.35 (+3.20%)  NMS  open  5m  10:49:45 ET."""
    when = ""
    if snap.get("asof"):
        when = bot.to_eastern(snap["asof"]).strftime("%H:%M:%S ET")
    bits = [snap.get("symbol") or "?", price_text(snap.get("price")), change_text(snap)]
    extra = [x for x in (snap.get("exchange"), state_text(snap.get("state")), snap.get("interval"), when) if x]
    if snap.get("high") is not None:
        extra.append("high %s" % price_text(snap["high"]))
    if snap.get("low") is not None:
        extra.append("low %s" % price_text(snap["low"]))
    return "  ".join(bits) + ("    " + "  ".join(extra) if extra else "")


def levels_for(symbol, positions):
    """Entry, stop, target and mark for this symbol from the account document's positions."""
    want = (symbol or "").upper().split(".")[0]
    if not want:
        return {}
    for row in positions or []:
        got = str(row.get("symbol") or "").upper().split(".")[0]
        if got != want:
            continue
        out = {}
        for key in LEVELS:
            if row.get(key) is not None and row.get(key) != "":
                try:
                    out[key] = float(row[key])
                except (TypeError, ValueError):
                    continue
        return out
    return {}


def layout(bars, width, height, levels=None, pad_l=64, pad_r=16, pad_t=16, pad_b=22):
    """Candle coordinates in a width x height box. Y grows downward, as on a canvas.

    `levels` is {entry, stop, target, mark: price}. Prices outside the candles
    stretch the scale so a stop below the day's low still lands on the chart.
    """
    empty = {"candles": [], "marks": [], "labels": [], "left": pad_l, "right": pad_r, "top": pad_t, "bottom": pad_b}
    width, height = int(width), int(height)
    if not bars or width <= pad_l + pad_r + 8 or height <= pad_t + pad_b + 8:
        return empty
    prices = []
    for bar in bars:
        prices.append(float(bar["l"]))
        prices.append(float(bar["h"]))
    marks_in = []
    for name in LEVELS:
        if not levels or levels.get(name) is None:
            continue
        try:
            px = float(levels[name])
        except (TypeError, ValueError):
            continue
        prices.append(px)
        marks_in.append((name, px))
    lo, hi = min(prices), max(prices)
    if hi == lo:
        hi = lo + 1.0
    span = hi - lo
    lo -= span * 0.06
    hi += span * 0.06
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(bars)
    slot = plot_w / float(n)
    body = max(1.0, min(slot * 0.62, 14.0))

    def y_of(price):
        return pad_t + (hi - float(price)) / (hi - lo) * plot_h

    candles = []
    for i, bar in enumerate(bars):
        candles.append({
            "x": pad_l + slot * (i + 0.5),
            "w": body,
            "high": y_of(bar["h"]),
            "low": y_of(bar["l"]),
            "open": y_of(bar["o"]),
            "close": y_of(bar["c"]),
            "up": float(bar["c"]) >= float(bar["o"]),
            "t": bar.get("t"),
        })
    labels = []
    for i in range(4):
        price = lo + (hi - lo) * i / 3.0
        labels.append({"price": price, "y": y_of(price)})
    marks = [{"name": name, "price": px, "y": y_of(px)} for name, px in marks_in]
    return {"candles": candles, "marks": marks, "labels": labels, "left": pad_l, "right": pad_r,
            "top": pad_t, "bottom": pad_b, "last_y": y_of(bars[-1]["c"]), "last": float(bars[-1]["c"])}


def load(symbol, interval="5m"):
    """One live snapshot. Raises BotError when Yahoo does not answer."""
    if interval not in SPANS:
        raise bot.BotError("candle size must be one of %s" % ", ".join(INTERVALS))
    doc = bot.fetch_chart(symbol, interval, span=SPANS[interval], prepost=True)
    snap = snapshot(doc)
    snap["interval"] = interval
    if not snap["symbol"]:
        snap["symbol"] = symbol.upper()
    return snap
