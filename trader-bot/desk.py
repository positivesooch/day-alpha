"""The trading desk: what the window's buttons run besides the bot's loop.

    account    connect to the broker; the account, its positions and open orders
    positions  the open positions (bot state and broker), one line each
    order      a manual bracket order through the same plan / size / risk / ticket gates
    close      close one position at market (cancels its bracket)
    flatten    the kill switch: close everything, cancel everything, halt the day
    settings   read or change the limits in trader/config.json and trader/bot.json
    live       switch the account between PAPER and LIVE (real money), on purpose
    creds      save, list or forget broker credentials (values from stdin, never argv)

Every command is a subcommand of bot.py and prints plain text, or JSON with
--json for the window. Real-money actions ask for a typed yes unless --yes is
given; the window's dialogs supply --yes only after you confirmed in them.
"""

import argparse
import json
import os
import sys

import bot
import vault
from bot import BotError, money, ny_hhmm, ny_text, parse_now, read_json, write_json

CONFIG_KEYS = {
    "budget": float, "cash_buffer_pct": float, "max_position_pct": float, "max_order_usd": float,
    "min_order_usd": float, "max_loss_per_trade_usd": float, "max_daily_loss_usd": float,
    "max_trades_per_day": int, "max_orders_per_day": int, "min_reward_risk": float, "require_stop": bool,
    "no_entries_before": str, "no_entries_after": str, "flat_by": str, "fractional": bool, "broker": str,
    "blocked": list, "allow_short": bool, "pdt_guard": bool, "sec_user_agent": str,
}
LOCKED_CONFIG = ("paper", "allow_options", "allow_margin")  # paper: the live command; the others: v2 is cash, long only
BOT_KEYS = {
    "symbols": list, "poll_seconds": int, "bar_grace_seconds": int,
    "scan.enabled": bool, "scan.limit": int, "scan.every_minutes": int, "scan.min_price": float,
    "scan.min_volume": int, "scan.max_candidates": int,
    "setup.or_minutes": int, "setup.interval": str, "setup.stale_minutes": int,
    "plan.stop": str, "plan.target": str,
    "news.enabled": bool, "news.hours": int, "news.block_tags": list,
    "timemachine.enabled": bool, "timemachine.replays": int, "timemachine.min_edge_pct": float,
    "timemachine.required": bool,
    "execution.mode": str, "execution.broker": str, "execution.max_open_positions": int,
    "execution.fill_wait_seconds": int,
    "mt5.terminal_path": str, "mt5.login": int, "mt5.server": str, "mt5.symbol_suffix": str,
    "mt5.server_utc_offset_hours": float, "mt5.magic": int, "mt5.deviation_points": int, "mt5.use_bars": bool,
    "mt5.history_days": int, "mt5.symbols": dict,
    "mt5.prop.initial_balance": float, "mt5.prop.max_daily_loss_pct": float, "mt5.prop.max_total_drawdown_pct": float,
    "mt5.prop.buffer_pct": float, "mt5.prop.daily_reset_server_time": str,
    "update.url": str, "update.check_on_start": bool,
}
LOCKED_BOT = ("execution.allow_auto_on_live", "mt5.allow_auto_on_real")  # the live command, with the phrase


# ---------------------------------------------------------------- helpers

def _paths(workdir):
    trader = os.path.join(workdir, "trader")
    return {"config": os.path.join(trader, "config.json"), "bot": os.path.join(trader, "bot.json"),
            "state": os.path.join(trader, "state", "bot.json"), "halt": os.path.join(trader, "state", "halt.json")}


def _say(a, text, out=None):
    if not getattr(a, "json", False):
        (out or sys.stdout).write(text + "\n")


def _emit(a, doc, out=None):
    if getattr(a, "json", False):
        (out or sys.stdout).write(json.dumps(doc, indent=2, default=str) + "\n")


def _desk_bot(a, mode="confirm", reply_fn=None, out=None):
    """A Bot connected to the broker, in confirm mode: the desk never says yes by itself."""
    workdir = bot._workdir(a)
    bot._ensure_config(workdir)
    cfg = bot._load_bot_cfg(workdir, getattr(a, "bot", None))
    broker = getattr(a, "broker", None) or cfg.get("execution", {}).get("broker") or "sim"
    bars = None
    if getattr(a, "bars", None):
        bars = bot.BarSource(a.bars, workdir, (cfg.get("setup") or {}).get("interval") or "5m")
    b = bot.Bot(workdir, cfg, mode=mode, broker=broker, bars=bars, replay=False, reply_fn=reply_fn, out=out,
                quiet=getattr(a, "quiet", False))
    b.begin_day(parse_now(getattr(a, "now", None)))
    return b


def _sim_positions(b):
    out = []
    for p in b.open_positions():
        mark = p.get("mark") or p["entry"]
        out.append({"symbol": p["symbol"], "side": "long", "qty": p["qty"], "entry": p["entry"], "mark": mark,
                    "pnl": round((mark - p["entry"]) * p["qty"], 2), "stop": p.get("stop"), "target": p.get("target"),
                    "broker_id": p.get("broker_id"), "bot": True})
    return out


def _merge_state(b, positions):
    """The bot's stop / target / entry time onto the broker's positions."""
    mine = {p["symbol"]: p for p in b.open_positions()}
    for row in positions:
        s = mine.get(row["symbol"]) or mine.get(str(row["symbol"]).split(".")[0])
        if s:
            row.setdefault("stop", s.get("stop"))
            row.setdefault("target", s.get("target"))
            row["stop"] = row.get("stop") or s.get("stop")
            row["target"] = row.get("target") or s.get("target")
            row["entry_time"] = s.get("entry_time")
            row["flat_by"] = s.get("flat_by")
            row["bot"] = True
        else:
            row.setdefault("bot", False)
    return positions


def account_doc(b):
    """The account as the window shows it: one shape for sim, Alpaca and MT5."""
    name = b.broker_name
    realised = round(sum(p["pnl"] for p in b.state.get("positions", {}).values() if p["status"] == "closed"), 2)
    doc = {"broker": name, "paper": b.paper, "connected": True, "mode": b.mode,
           "credentials": b.vault.status(), "session": {
               "date": b.state.get("date"), "halted": b.state.get("halted"), "realised": realised,
               "trades": len([p for p in b.state.get("positions", {}).values() if p["status"] == "closed"]),
               "last_tick": b.state.get("last_tick")}}
    if name == "sim":
        positions = _sim_positions(b)
        unreal = round(sum(p["pnl"] for p in positions), 2)
        budget = float(b.cfg.get("budget") or 0)
        doc["account"] = {"kind": "paper (built-in sim)", "id": "sim", "currency": "USD", "balance": budget + realised,
                          "equity": round(budget + realised + unreal, 2), "cash": round(
                              budget + realised - sum(p["qty"] * p["entry"] for p in positions), 2),
                          "buying_power": None, "margin_free": None, "day_pnl": round(realised + unreal, 2),
                          "unrealised": unreal}
        doc["positions"], doc["orders"] = positions, []
    elif name == "alpaca":
        acct = b.broker.account()
        positions = _merge_state(b, b.broker.positions())
        equity, last = float(acct.get("equity") or 0), float(acct.get("last_equity") or 0)
        doc["account"] = {"kind": "paper" if b.paper else "LIVE", "id": acct.get("account_number"),
                          "status": acct.get("status"), "currency": acct.get("currency", "USD"),
                          "balance": round(float(acct.get("cash") or 0) + sum(p["value"] for p in positions), 2),
                          "equity": round(equity, 2), "cash": round(float(acct.get("cash") or 0), 2),
                          "buying_power": round(float(acct.get("buying_power") or 0), 2),
                          "margin_free": round(float(acct.get("regt_buying_power") or 0), 2),
                          "day_pnl": round(equity - last, 2) if last else None,
                          "unrealised": round(sum(p["pnl"] for p in positions), 2),
                          "daytrade_count": acct.get("daytrade_count"), "pdt": acct.get("pattern_day_trader"),
                          "blocked": bool(acct.get("trading_blocked") or acct.get("account_blocked"))}
        doc["positions"], doc["orders"] = positions, b.broker.orders()
    else:
        acct = b.account_info or b.broker.account()
        positions = _merge_state(b, b.broker.positions())
        snap = None
        try:
            snap = b.guard.snapshot(b.now)
        except BotError as e:
            doc["prop_error"] = str(e)
        doc["account"] = {"kind": acct.get("kind"), "id": acct.get("login"), "server": acct.get("server"),
                          "company": acct.get("company"), "name": acct.get("name"),
                          "currency": acct.get("currency", "USD"), "balance": round(acct.get("balance") or 0, 2),
                          "equity": round(acct.get("equity") or 0, 2), "cash": None,
                          "buying_power": None, "margin_free": round(acct.get("margin_free") or 0, 2),
                          "leverage": acct.get("leverage"),
                          "day_pnl": round(acct["equity"] - snap["day_start"], 2) if snap else None,
                          "unrealised": round(sum(p["pnl"] for p in positions), 2)}
        if snap:
            doc["prop"] = {k: snap.get(k) for k in ("initial_balance", "daily_limit", "daily_used", "day_start",
                                                    "drawdown_limit", "drawdown_used", "buffer_pct")}
        doc["positions"], doc["orders"] = positions, []
    return doc


def account_text(doc):
    a = doc["account"]
    kind = (a.get("kind") or "?").upper()
    lines = ["%s broker, %s account%s: %s" % (
        doc["broker"], kind, " (REAL MONEY)" if not doc["paper"] else "", a.get("id") or "")]
    if a.get("server"):
        lines.append("  server %s (%s)%s" % (a["server"], a.get("company") or "?",
                                             ", leverage 1:%s" % a["leverage"] if a.get("leverage") else ""))
    lines.append("  balance %s, equity %s%s%s" % (
        money(a.get("balance") or 0), money(a.get("equity") or 0),
        ", cash %s" % money(a["cash"]) if a.get("cash") is not None else "",
        ", buying power %s" % money(a["buying_power"]) if a.get("buying_power") is not None else ""))
    if a.get("margin_free") is not None and doc["broker"] == "mt5":
        lines.append("  free margin %s" % money(a["margin_free"]))
    if a.get("day_pnl") is not None:
        lines.append("  today %s%s at the broker, %s unrealised on open positions" % (
            "+" if a["day_pnl"] >= 0 else "-", money(abs(a["day_pnl"])), money(a.get("unrealised") or 0)))
    if a.get("blocked"):
        lines.append("  TRADING BLOCKED by the broker")
    if doc.get("prop"):
        p = doc["prop"]
        lines.append("  prop rules: daily loss %s of %s used, drawdown %s of %s used, %.0f%% buffer" % (
            money(p["daily_used"]), money(p["daily_limit"]), money(p["drawdown_used"]), money(p["drawdown_limit"]),
            p["buffer_pct"]))
    s = doc["session"]
    lines.append("  bot session %s: %d closed trade%s, realised %s%s%s" % (
        s.get("date"), s["trades"], "" if s["trades"] == 1 else "s", "+" if s["realised"] >= 0 else "-",
        money(abs(s["realised"])), "; HALTED: %s" % s["halted"] if s.get("halted") else ""))
    c = doc["credentials"]
    lines.append("  credentials: %s" % ", ".join("%s %s" % (k, v or "missing") for k, v in sorted(c.items())))
    lines.append(positions_text(doc["positions"]))
    if doc.get("orders"):
        lines.append("  open orders:")
        for o in doc["orders"]:
            lines.append("    %s %s %s %s %s%s" % (o.get("side"), o.get("qty"), o.get("symbol"), o.get("type"),
                                                  o.get("limit") or o.get("stop") or "", " [%s]" % o.get("status")))
    return "\n".join(lines)


def positions_text(positions):
    if not positions:
        return "  no open positions"
    lines = ["  open positions:"]
    for p in positions:
        pnl = p.get("pnl") or 0
        lines.append("    %-8s %-5s %8s @ %-10s mark %-10s %s%-9s stop %s target %s%s" % (
            p["symbol"], p.get("side", "long"), ("%g" % p["qty"]) if isinstance(p["qty"], float) else p["qty"],
            money(p["entry"]), money(p.get("mark") or p["entry"]), "+" if pnl >= 0 else "-", money(abs(pnl)),
            money(p["stop"]) if p.get("stop") else "-", money(p["target"]) if p.get("target") else "-",
            "" if p.get("bot", True) else "  (not the bot's)"))
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def cmd_account(a):
    b = _desk_bot(a)
    doc = account_doc(b)
    _emit(a, doc)
    _say(a, account_text(doc))
    return 0


def cmd_positions(a):
    b = _desk_bot(a)
    doc = account_doc(b)
    _emit(a, {"broker": doc["broker"], "paper": doc["paper"], "positions": doc["positions"], "orders": doc["orders"]})
    _say(a, positions_text(doc["positions"]).strip() or "no open positions")
    return 0


def _confirm(a, prompt, phrase="yes"):
    """A typed yes for a real-money action unless --yes was given."""
    if getattr(a, "yes", False):
        return True
    try:
        reply = input(prompt)
    except EOFError:
        return False
    return reply.strip().lower() == phrase.lower()


def cmd_close(a):
    b = _desk_bot(a)
    sym = a.symbol.strip().upper()
    account = "PAPER" if b.paper else "LIVE, REAL MONEY"
    if not b.paper and not _confirm(a, "Close %s at market on the %s account? Reply yes or no: " % (sym, account)):
        _say(a, "not closed")
        return 1
    now = b.now
    mine = b.state.get("positions", {}).get(sym)
    if b.broker_name == "sim":
        if not mine or mine["status"] != "open":
            raise BotError("no open position on %s in the sim" % sym)
        price = mine.get("mark") or mine["entry"]
        try:
            closed, tick = b.bars.bars(sym, now, fresh=True)
            price = float(tick["price"]) if tick and tick.get("price") else (float(closed[-1]["c"]) if closed else price)
        except BotError:
            pass
        hit = {"reason": "closed by you", "price": round(price, 2), "at": now}
    else:
        res = b.broker.close(sym, now)
        hit = {"reason": "closed by you", "price": res.get("price") or (mine or {}).get("mark") or (mine or {}).get("entry"),
               "at": now, "broker_id": res.get("broker_id")}
        if res.get("pnl") is not None:
            hit["pnl"] = res["pnl"]
    if mine and mine["status"] == "open":
        b.record_exit(mine, hit)
    else:
        b.log("%s: closed at the broker at %s (the bot did not open it)" % (sym, money(hit["price"] or 0)))
    b.save()
    _emit(a, {"closed": sym, "price": hit.get("price"), "pnl": (mine or {}).get("pnl")})
    return 0


def cmd_flatten(a):
    b = _desk_bot(a)
    account = "PAPER" if b.paper else "LIVE, REAL MONEY"
    _say(a, "FLATTEN ALL on the %s broker (%s account): every open position closed at market, every open order "
            "cancelled, no more entries today." % (b.broker_name, account))
    if not _confirm(a, "Do it? Reply yes or no: "):
        _say(a, "nothing closed")
        return 1
    now = b.now
    closed = []
    if b.broker_name == "sim":
        for p in b.open_positions():
            price = p.get("mark") or p["entry"]
            b.record_exit(p, {"reason": "flattened by you", "price": price, "at": now})
            closed.append(p["symbol"])
    else:
        try:
            closed = b.broker.flatten_all(now, everything=True)
        except BotError as e:
            b.log("FLATTEN: the broker refused: %s. Close the positions at the broker yourself." % e)
            b.save()
            raise
        for p in b.open_positions():
            try:
                hit = b.broker.watch(p, now)
            except BotError:
                hit = None
            b.record_exit(p, hit or {"reason": "flattened by you", "price": p.get("mark") or p["entry"], "at": now})
    reason = "flattened by you at %s" % ny_hhmm(now)
    b.state["halted"] = reason
    write_json(_paths(b.workdir)["halt"], {"date": b.state.get("date"), "reason": reason, "at": ny_text(now),
                                           "closed": closed})
    b.log("FLATTENED: %d position%s closed (%s). The day is halted; a running bot takes no more trades." % (
        len(closed), "" if len(closed) == 1 else "s", ", ".join(str(c) for c in closed) or "none were open"))
    b.save()
    _emit(a, {"closed": closed, "halted": reason})
    return 0


def cmd_resume(a):
    """Lift a halt set by flatten (the next session starts clean anyway)."""
    workdir = bot._workdir(a)
    paths = _paths(workdir)
    if os.path.exists(paths["halt"]):
        os.remove(paths["halt"])
    st = bot.read_state(paths["state"])
    if st and st.get("halted"):
        st["halted"] = None
        st.setdefault("events", []).append({"at": ny_text(parse_now(None)), "text": "halt lifted by you"})
        write_json(paths["state"], st)
    _say(a, "halt lifted: the next run can take trades again")
    _emit(a, {"halted": None})
    return 0


def cmd_order(a):
    """A manual bracket order: plan -> size -> risk -> ticket -> yes -> broker, like the bot's own."""
    replies = iter([a.reply] if getattr(a, "reply", None) else [])
    reply_fn = (lambda prompt: next(replies, None) or input(prompt)) if getattr(a, "reply", None) else None
    b = _desk_bot(a, mode="dry-run" if getattr(a, "dry_run", False) else "confirm", reply_fn=reply_fn)
    sym = a.symbol.strip().upper()
    if (a.side or "buy") != "buy":
        raise BotError("v2 is long only: side must be buy (close or flatten sell what is open)")
    entry = a.entry
    if entry is None:
        entry = b.quote(sym)
        if entry is None:
            raise BotError("no price for %s: give --entry PRICE (about where it trades now)" % sym)
        b.log("%s: trades about %s now" % (sym, money(entry)))
    stop, target = float(a.stop), float(a.target) if a.target is not None else None
    if stop >= entry:
        raise BotError("the stop (%s) must be under the entry (%s) for a long" % (money(stop), money(entry)))
    if target is None:
        target = round(entry + (entry - stop) * float(a.r or 2.0), 2)
    if target <= entry:
        raise BotError("the target (%s) must be over the entry (%s)" % (money(target), money(entry)))
    c = b.cand(sym)
    c.update({"status": "manual", "reasons": [], "stages": {}})
    plan_args = ["plan", "--symbol", sym, "--entry", repr(float(entry)), "--stop", "price:%s" % stop,
                 "--target", "price:%s" % target, "--setup", "level", "--write", "trader/plan.json", "--json"]
    code, plan, err = b.tool.run("exit", plan_args)
    if not plan or plan.get("stop") is None:
        raise BotError("exit plan failed: %s" % (err or "").strip()[-300:])
    b.log("%s: your plan: entry %s, stop %s, target %s, %s a share at risk (%.2fR)" % (
        sym, money(plan["entry"]), money(plan["stop"]), money(plan["target"]), money(plan["risk_per_share"]),
        plan.get("reward_risk") or 0))
    b.write_price(sym, plan)
    def finish():
        b.save()
        _emit(a, {"symbol": sym, "status": c["status"], "reasons": c["reasons"],
                  "position": b.state.get("positions", {}).get(sym)})
        return 0 if c["status"] in ("open", "dry-run") else 1

    doc = b.size(sym, c)
    if not doc:
        return finish()
    order = doc["orders"][0]
    if a.qty:
        sized = int(float(order["qty"]))
        want = int(a.qty)
        if want > sized:
            raise BotError("your limits size %s at most %d shares (%s); %d is more than that" % (
                sym, sized, doc["sizing"].get("bound_by") or "budget", want))
        if want < sized:
            order["qty"] = str(want)
            order["plan"]["max_loss"] = round(want * float(plan["risk_per_share"]), 2)
            order["why"] = "%s, trimmed to %d shares by you" % (order.get("why") or "manual", want)
            orders_doc = read_json(os.path.join(b.trader, "orders.json"))
            orders_doc["orders"][0] = order
            write_json(os.path.join(b.trader, "orders.json"), orders_doc)
            b.log("%s: trimmed to %d shares by you, max loss %s" % (sym, want, money(order["plan"]["max_loss"])))
    if not b.risk_ok(sym, c) or not b.prop_ok(sym, c, order):
        return finish()
    ticket = b.ticket(sym, c)
    if not ticket:
        return finish()
    if getattr(a, "yes", False):
        b.reply_fn = lambda prompt: "yes"
    b.execute(sym, c, ticket, ticket["tool_input"], plan)
    return finish()


# ---- settings

def _coerce(kind, raw):
    if kind is bool:
        if str(raw).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).strip().lower() in ("0", "false", "no", "off"):
            return False
        raise BotError("expected true or false, not %r" % raw)
    if kind in (list, dict):
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            if kind is list:
                return [s.strip().upper() for s in str(raw).replace(";", ",").split(",") if s.strip()]
            raise BotError("expected JSON for this setting, not %r" % raw)
        if not isinstance(val, kind):
            raise BotError("expected a %s, not %r" % (kind.__name__, raw))
        return val
    if raw is None or str(raw).strip().lower() in ("", "null", "none"):
        return None
    if kind is int:
        return int(float(raw))
    if kind is float:
        return float(raw)
    if kind is str:
        val = str(raw).strip()
        return val
    return raw


def _hhmm_ok(val):
    try:
        h, m = val.split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except (ValueError, AttributeError):
        return False


def apply_settings(workdir, pairs):
    """key=value pairs onto trader/config.json (plain keys) and trader/bot.json (bot.<path>)."""
    paths = _paths(workdir)
    bot._ensure_config(workdir)
    cfg = read_json(paths["config"])
    bcfg = read_json(paths["bot"], required=False) or read_json(bot.BOT_EXAMPLE)
    changed = []
    for pair in pairs:
        if "=" not in pair:
            raise BotError("settings take key=value, not %r" % pair)
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key.startswith("bot."):
            path = key[4:]
            if path in LOCKED_BOT:
                raise BotError("%s is armed with: bot.py live --auto on --confirm \"%s\"" % (key, bot.LIVE_PHRASE))
            if path not in BOT_KEYS:
                raise BotError("unknown bot setting %s (one of %s)" % (path, ", ".join(sorted(BOT_KEYS))))
            val = _coerce(BOT_KEYS[path], raw)
            node = bcfg
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            old = node.get(parts[-1])
            node[parts[-1]] = val
        else:
            if key in LOCKED_CONFIG:
                raise BotError("%s is switched with: bot.py live --on | --off" % key if key == "paper"
                               else "%s stays false in v2 (cash account, stocks, long only)" % key)
            if key not in CONFIG_KEYS:
                raise BotError("unknown setting %s (one of %s)" % (key, ", ".join(sorted(CONFIG_KEYS))))
            val = _coerce(CONFIG_KEYS[key], raw)
            if key in ("no_entries_before", "no_entries_after", "flat_by") and not _hhmm_ok(val):
                raise BotError("%s must be HH:MM New York time, not %r" % (key, val))
            if key in ("budget", "max_loss_per_trade_usd", "max_daily_loss_usd") and (val is None or val <= 0):
                raise BotError("%s must be a positive number" % key)
            if key == "broker" and val not in ("alpaca", "mt5", "sim"):
                raise BotError("broker must be alpaca, mt5 or sim")
            old = cfg.get(key)
            cfg[key] = val
        changed.append({"key": key, "old": old, "new": val})
    if any(c["key"] == "max_loss_per_trade_usd" or c["key"] == "max_daily_loss_usd" for c in changed):
        if float(cfg.get("max_loss_per_trade_usd") or 0) > float(cfg.get("max_daily_loss_usd") or 0):
            raise BotError("max_loss_per_trade_usd (%s) cannot be more than max_daily_loss_usd (%s)" % (
                cfg["max_loss_per_trade_usd"], cfg["max_daily_loss_usd"]))
    if changed:
        write_json(paths["config"], cfg)
        write_json(paths["bot"], bcfg)
    return changed, cfg, bcfg


def cmd_settings(a):
    workdir = bot._workdir(a)
    paths = _paths(workdir)
    changed, cfg, bcfg = apply_settings(workdir, a.set or [])
    for c in changed:
        _say(a, "%s: %s -> %s" % (c["key"], json.dumps(c["old"]), json.dumps(c["new"])))
    if a.json:
        _emit(a, {"config": cfg, "bot": bot.merged(bot.DEFAULTS, bcfg), "paths": paths, "changed": changed,
                  "editable": {"config": sorted(CONFIG_KEYS), "bot": sorted(BOT_KEYS)},
                  "credentials": vault.Vault(workdir).status()})
    elif not changed:
        _say(a, "trader/config.json (your limits):")
        for k in sorted(CONFIG_KEYS) + ["paper"]:
            _say(a, "  %-24s %s" % (k, json.dumps(cfg.get(k))))
        _say(a, "trader/bot.json (the bot):")
        merged_bot = bot.merged(bot.DEFAULTS, bcfg)
        for k in sorted(BOT_KEYS) + list(LOCKED_BOT):
            node = merged_bot
            for part in k.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            _say(a, "  bot.%-20s %s" % (k, json.dumps(node)))
        _say(a, "change one: bot.py settings --set max_daily_loss_usd=200 --set bot.execution.max_open_positions=2")
    return 0


# ---- live / paper

def cmd_live(a):
    workdir = bot._workdir(a)
    paths = _paths(workdir)
    bot._ensure_config(workdir)
    cfg = read_json(paths["config"])
    bcfg = read_json(paths["bot"], required=False) or read_json(bot.BOT_EXAMPLE)
    phrase_ok = (a.confirm or "").strip().upper() == bot.LIVE_PHRASE
    if a.off:
        cfg["paper"] = True
        write_json(paths["config"], cfg)
        _say(a, "PAPER: trader/config.json says \"paper\": true. Orders go to the paper endpoint / demo account; "
                "a REAL MetaTrader 5 login is refused.")
    if a.on:
        if not phrase_ok:
            raise BotError("switching to the LIVE account needs the exact phrase: bot.py live --on --confirm \"%s\". "
                           "Real money, real losses; most people who day trade lose money." % bot.LIVE_PHRASE)
        cfg["paper"] = False
        write_json(paths["config"], cfg)
        _say(a, "LIVE: trader/config.json says \"paper\": false. Alpaca orders go to api.alpaca.markets with your "
                "live keys; MetaTrader 5 may be a REAL account. Confirm mode asks you for every order; auto mode "
                "stays refused until you arm it (live --auto on).")
    if a.auto is not None:
        on = a.auto.lower() in ("on", "true", "yes", "1")
        if on and not phrase_ok:
            raise BotError("arming auto mode on a live account needs the exact phrase: bot.py live --auto on "
                           "--confirm \"%s\"" % bot.LIVE_PHRASE)
        bcfg.setdefault("execution", {})["allow_auto_on_live"] = on
        bcfg.setdefault("mt5", {})["allow_auto_on_real"] = on
        write_json(paths["bot"], bcfg)
        _say(a, "auto mode on a live account: %s" % ("ARMED: the bot says yes to its own orders with real money, "
                                                       "inside your limits" if on else "off: the bot refuses to start "
                                                       "in auto mode on a live account"))
    merged_bot = bot.merged(bot.DEFAULTS, bcfg)
    doc = {"paper": bool(cfg.get("paper", True)), "allow_auto_on_live": bool(
        merged_bot["execution"].get("allow_auto_on_live") or merged_bot["mt5"].get("allow_auto_on_real")),
        "phrase": bot.LIVE_PHRASE}
    if not (a.on or a.off or a.auto is not None):
        _say(a, "account: %s; auto mode on a live account: %s" % (
            "PAPER" if doc["paper"] else "LIVE (real money)", "armed" if doc["allow_auto_on_live"] else "off"))
    _emit(a, doc)
    return 0


# ---- credentials

def cmd_creds(a):
    v = vault.Vault(bot._workdir(a))
    if a.set:
        name = a.set.strip()
        if name not in vault.NAMES:
            raise BotError("unknown credential %s (one of %s)" % (name, ", ".join(sorted(vault.NAMES))))
        value = sys.stdin.readline().rstrip("\r\n")
        try:
            v.set(name, value)
        except vault.VaultError as e:
            raise BotError(str(e))
        _say(a, "%s %s" % (name, "saved (encrypted for your Windows account)" if value and sys.platform == "win32"
                           else "saved" if value else "forgotten"))
    if a.clear:
        if a.clear == "all":
            v.clear()
        elif a.clear in vault.NAMES:
            v.delete(a.clear)
        else:
            raise BotError("unknown credential %s" % a.clear)
        _say(a, "%s forgotten" % a.clear)
    status = v.status()
    if not a.set and not a.clear:
        for k, src in sorted(status.items()):
            _say(a, "  %-15s %s" % (k, src or "missing"))
        _say(a, "values are never printed. Save one: echo THE_VALUE | bot.py creds --set alpaca.key")
    _emit(a, {"credentials": status, "file": v.path})
    return 0


def add_parsers(sub, common_):
    def common(p):
        common_(p)
        p.add_argument("--now", help=argparse.SUPPRESS)
        p.add_argument("--bars", help=argparse.SUPPRESS)

    p = sub.add_parser("account", help="connect to the broker: the account, its positions and open orders")
    common(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("positions", help="the open positions at the broker and in the bot's state")
    common(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("order", help="a manual bracket order through the plan / size / risk / ticket gates")
    common(p)
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", default="buy", choices=("buy",))
    p.add_argument("--entry", type=float, help="about where it trades (default: ask the broker)")
    p.add_argument("--stop", type=float, required=True, help="stop-loss price")
    p.add_argument("--target", type=float, help="take-profit price (default: --r times the risk)")
    p.add_argument("--r", type=float, default=2.0, help="target in R when --target is not given (default 2)")
    p.add_argument("--qty", type=int, help="shares; trimmed to this when your limits allow more")
    p.add_argument("--dry-run", action="store_true", help="build the ticket, place nothing")
    p.add_argument("--yes", action="store_true", help="place without asking (the window's dialog already asked)")
    p.add_argument("--reply", help="a scripted answer to the yes or no")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("close", help="close one position at market, cancel its bracket")
    common(p)
    p.add_argument("--symbol", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("flatten", help="KILL SWITCH: close every position, cancel every order, halt the day")
    common(p)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("resume", help="lift a halt set by flatten")
    p.add_argument("--dir")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("settings", help="show or change the limits in trader/config.json and trader/bot.json")
    p.add_argument("--dir")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="e.g. max_daily_loss_usd=200, bot.symbols=TSLA,NVDA")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("live", help="switch between the PAPER and the LIVE (real money) account, on purpose")
    p.add_argument("--dir")
    p.add_argument("--on", action="store_true", help="LIVE: needs --confirm \"%s\"" % bot.LIVE_PHRASE)
    p.add_argument("--off", action="store_true", help="back to PAPER")
    p.add_argument("--auto", choices=("on", "off"), help="let auto mode trade the live account (on needs --confirm)")
    p.add_argument("--confirm", metavar="PHRASE")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("creds", help="save or forget broker credentials (the value is read from stdin)")
    p.add_argument("--dir")
    p.add_argument("--set", metavar="NAME", help="alpaca.key, alpaca.secret or mt5.password; value on stdin")
    p.add_argument("--stdin", action="store_true", help="(default) read the value from the first line of stdin")
    p.add_argument("--clear", metavar="NAME", help="a name, or all")
    p.add_argument("--json", action="store_true")


COMMANDS = {"account": cmd_account, "positions": cmd_positions, "order": cmd_order, "close": cmd_close,
            "flatten": cmd_flatten, "resume": cmd_resume, "settings": cmd_settings, "live": cmd_live,
            "creds": cmd_creds}
