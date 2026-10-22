import copy
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

bot = load("trader-bot", "bot")

BARS = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
HIST = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
WALL = os.path.join(FIX, "trader-scanner", "wall5m-2026-10-19.json.gz")
NEWS = os.path.join(FIX, "trader-news")
CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
DAY = datetime.date(2026, 10, 19)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = bot.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(day, hhmm):
    """New York wall clock -> unix seconds, through the tool's own DST rule."""
    utc = bot.hhmm_to_utc(day, hhmm)
    return int((utc - datetime.datetime(1970, 1, 1)).total_seconds())


def tsla_doc():
    with open(BARS, encoding="utf-8") as fh:
        return json.load(fh)


def extend(doc, bars, drop_tick=True):
    """Append synthetic 5-minute bars (hhmm, o, h, l, c) to the TSLA chart.
    The fixture ends with Yahoo's 10:49:45 live tick; drop it so the day reads clean."""
    d = copy.deepcopy(doc)
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    if drop_tick:
        keep = [i for i, t in enumerate(res["timestamp"]) if t % 300 == 0]
        res["timestamp"] = [res["timestamp"][i] for i in keep]
        for k in q:
            q[k] = [q[k][i] for i in keep]
    for hhmm, o, h, l, c in bars:
        res["timestamp"].append(ts(DAY, hhmm))
        q["open"].append(o)
        q["high"].append(h)
        q["low"].append(l)
        q["close"].append(c)
        q["volume"].append(500000)
    res["meta"].pop("regularMarketTime", None)
    res["meta"].pop("regularMarketPrice", None)
    return d


def flat_bars(start, end, around=365.0):
    """5-minute bars from start to end (inclusive) that touch neither 358.32 nor 370.04."""
    out = []
    t = datetime.datetime.combine(DAY, datetime.time(*map(int, start.split(":"))))
    last = datetime.datetime.combine(DAY, datetime.time(*map(int, end.split(":"))))
    while t <= last:
        out.append((t.strftime("%H:%M"), around, around + 0.8, around - 0.8, around + 0.2))
        t += datetime.timedelta(minutes=5)
    return out


class Replay(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bot-")
        self.botcfg = os.path.join(self.d, "bot.json")
        with open(os.path.join(ROOT, "trader-bot", "bot.example.json")) as fh:
            cfg = json.load(fh)
        cfg["timemachine"]["replays"] = 2000  # faster; the gate is what matters
        with open(self.botcfg, "w") as fh:
            json.dump(cfg, fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def replay(self, *extra, bars=BARS, config=None):
        work = os.path.join(self.d, "replay")
        if os.path.isdir(work):
            shutil.rmtree(work)
        os.makedirs(os.path.join(work, "trader"))
        shutil.copy(config or CFG, os.path.join(work, "trader", "config.json"))
        argv = ["replay", "--into", work, "--bot", self.botcfg, "--date", "2026-10-19", "--bars", bars,
                "--history", HIST, "--news", NEWS, "--symbols", "TSLA", "--quiet"] + list(extra)
        code, out, err = run(argv)
        with open(os.path.join(work, "trader", "state", "bot.json")) as fh:
            state = json.load(fh)
        return code, out, err, state, work

    def test_dry_run_reproduces_the_reel_and_places_nothing(self):
        code, out, err, st, work = self.replay()
        self.assertEqual(code, 0, err)
        c = st["candidates"]["TSLA"]
        self.assertEqual(c["status"], "dry-run")
        self.assertEqual(c["stages"]["entry"]["trigger"]["time"], "10:15 AM")
        self.assertEqual(c["stages"]["entry"]["trigger"]["price"], 364.18)
        self.assertEqual((c["stages"]["plan"]["stop"], c["stages"]["plan"]["target"]), (358.32, 370.04))
        self.assertEqual(c["stages"]["timemachine"]["history"]["triggered"], 33)
        self.assertEqual(c["stages"]["size"]["qty"], 8)
        self.assertEqual(c["stages"]["risk"]["verdict"], "PASS")
        self.assertEqual(c["stages"]["risk"]["max_loss"], 46.88)
        self.assertEqual(c["stages"]["ticket"]["tool_input"]["qty"], "8")
        self.assertIn("deal", c["stages"]["news"]["tags"])
        self.assertEqual(st["positions"], {})
        self.assertFalse(os.path.exists(os.path.join(work, "trader", "state", "broker.json")))
        self.assertIn("DRY RUN", " ".join(e["text"] for e in st["events"]))
        self.assertIn("Not financial advice", out)

    def test_auto_mode_takes_the_trade_on_the_sim_and_logs_it_as_the_bot(self):
        code, out, err, st, work = self.replay("--mode", "auto")
        self.assertEqual(code, 0, err)
        p = st["positions"]["TSLA"]
        self.assertEqual((p["qty"], p["entry"], p["stop"], p["target"]), (8, 364.18, 358.32, 370.04))
        self.assertEqual(p["status"], "open")
        self.assertEqual(p["mark"], 366.68)  # the last closed bar the fixture has (10:45)
        self.assertEqual(p["note"], "open when the data ended")
        with open(os.path.join(work, "trader", "state", "broker.json")) as fh:
            log = json.load(fh)["orders"]
        self.assertEqual([r["status"] for r in log], ["approved", "placed", "filled"])
        self.assertTrue(log[0]["auto"])
        self.assertIn("nobody typed this", log[0]["reply"])
        with open(os.path.join(work, "trader", "portfolio.json")) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 2086.56)
        self.assertEqual(pf["positions"][0]["shares"], 8.0)
        self.assertIn("+$20.00 unrealised", out)

    def test_auto_mode_refuses_a_live_account(self):
        with open(CFG) as fh:
            cfg = json.load(fh)
        cfg["paper"] = False
        live = os.path.join(self.d, "live.json")
        with open(live, "w") as fh:
            json.dump(cfg, fh)
        work = os.path.join(self.d, "replay")
        os.makedirs(os.path.join(work, "trader"))
        shutil.copy(live, os.path.join(work, "trader", "config.json"))
        code, out, err = run(["replay", "--into", work, "--bot", self.botcfg, "--date", "2026-10-19", "--bars", BARS,
                              "--history", HIST, "--mode", "auto", "--quiet"])
        self.assertEqual(code, 1)
        self.assertIn("paper accounts only", err)

    def test_confirm_mode_needs_an_explicit_yes(self):
        code, out, err, st, work = self.replay("--mode", "confirm", "--reply", "yes to all", "--reply", "no thanks")
        self.assertEqual(st["candidates"]["TSLA"]["status"], "declined")
        self.assertEqual(st["positions"], {})
        self.assertIn("a yes covers one order at a time", out)
        with open(os.path.join(work, "trader", "state", "broker.json")) as fh:
            log = json.load(fh)["orders"]
        self.assertEqual([r["status"] for r in log], ["declined"])

        code, out, err, st, work = self.replay("--mode", "confirm", "--reply", "yes")
        self.assertEqual(st["positions"]["TSLA"]["status"], "open")
        self.assertIn("ORDER 1 of 1, PAPER account", out)
        self.assertIn("no second Allow once / Deny card", out)
        with open(os.path.join(work, "trader", "state", "broker.json")) as fh:
            log = json.load(fh)["orders"]
        self.assertEqual(log[0]["reply"], "yes")
        self.assertNotIn("auto", log[0])

    def test_the_target_fills_on_the_sim(self):
        bars = flat_bars("10:50", "10:55") + [("11:00", 366.0, 370.50, 365.5, 369.0)] + flat_bars("11:05", "11:15")
        path = os.path.join(self.d, "target.json")
        with open(path, "w") as fh:
            json.dump(extend(tsla_doc(), bars), fh)
        code, out, err, st, work = self.replay("--mode", "auto", bars=path)
        p = st["positions"]["TSLA"]
        self.assertEqual((p["status"], p["exit_reason"], p["exit"]), ("closed", "target", 370.04))
        self.assertEqual(p["exit_time"], "2026-10-19 11:05:00")
        self.assertEqual(p["pnl"], 46.88)
        self.assertEqual(p["r"], 1.0)
        self.assertEqual(st["candidates"]["TSLA"]["status"], "closed")
        self.assertIn("realised +$46.88 on 1 trade", out)
        with open(os.path.join(work, "trader", "portfolio.json")) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], round(2086.56 + 8 * 370.04, 2))
        self.assertEqual(pf["positions"], [])

    def test_the_stop_fills_on_the_sim_and_a_gap_fills_at_the_open(self):
        bars = flat_bars("10:50", "10:55") + [("11:00", 357.00, 357.50, 356.00, 356.50)]
        path = os.path.join(self.d, "stop.json")
        with open(path, "w") as fh:
            json.dump(extend(tsla_doc(), bars), fh)
        code, out, err, st, work = self.replay("--mode", "auto", bars=path)
        p = st["positions"]["TSLA"]
        self.assertEqual((p["status"], p["exit_reason"], p["exit"]), ("closed", "stop", 357.0))
        self.assertEqual(p["pnl"], round(8 * (357.0 - 364.18), 2))
        self.assertLess(p["r"], -1.0)

    def test_stop_wins_when_one_bar_does_both(self):
        bars = [("10:50", 365.0, 371.0, 358.0, 365.0)]
        path = os.path.join(self.d, "both.json")
        with open(path, "w") as fh:
            json.dump(extend(tsla_doc(), bars), fh)
        code, out, err, st, work = self.replay("--mode", "auto", bars=path)
        p = st["positions"]["TSLA"]
        self.assertEqual((p["exit_reason"], p["exit"]), ("stop", 358.32))

    def test_flat_by_the_close(self):
        path = os.path.join(self.d, "flat.json")
        with open(path, "w") as fh:
            json.dump(extend(tsla_doc(), flat_bars("10:50", "15:55")), fh)
        code, out, err, st, work = self.replay("--mode", "auto", bars=path)
        p = st["positions"]["TSLA"]
        self.assertEqual((p["status"], p["exit_reason"]), ("closed", "flat_by"))
        self.assertEqual(p["exit_time"], "2026-10-19 15:55:00")
        self.assertEqual(p["exit"], 365.2)  # the 15:50 bar's close, the last one closed at 15:55
        self.assertEqual(p["pnl"], round(8 * (365.2 - 364.18), 2))
        with open(os.path.join(work, "trader", "state", "broker.json")) as fh:
            log = json.load(fh)["orders"]
        self.assertEqual(log[-1]["side"], "sell")
        self.assertEqual(log[-1]["status"], "filled")

    def test_time_machine_gate_can_skip_the_trade(self):
        with open(self.botcfg) as fh:
            cfg = json.load(fh)
        cfg["timemachine"]["min_edge_pct"] = 10.0  # the reel's edge is about 4 points
        with open(self.botcfg, "w") as fh:
            json.dump(cfg, fh)
        code, out, err, st, work = self.replay("--mode", "auto")
        c = st["candidates"]["TSLA"]
        self.assertEqual(c["status"], "skipped")
        self.assertIn("time machine edge", c["reasons"][0])
        self.assertNotIn("size", c["stages"])
        self.assertEqual(st["positions"], {})

    def test_news_tags_can_skip_the_trade(self):
        with open(self.botcfg) as fh:
            cfg = json.load(fh)
        cfg["news"]["block_tags"] = ["earnings"]
        with open(self.botcfg, "w") as fh:
            json.dump(cfg, fh)
        code, out, err, st, work = self.replay("--mode", "auto")
        c = st["candidates"]["TSLA"]
        self.assertEqual(c["status"], "skipped")
        self.assertIn("news tagged earnings", c["reasons"][0])
        self.assertNotIn("plan", c["stages"])

    def test_risk_block_is_final(self):
        with open(CFG) as fh:
            cfg = json.load(fh)
        cfg["max_loss_per_trade_usd"] = 5  # 0 shares fit: the budget refuses before risk even sees it
        tight = os.path.join(self.d, "tight.json")
        with open(tight, "w") as fh:
            json.dump(cfg, fh)
        code, out, err, st, work = self.replay("--mode", "auto", config=tight)
        c = st["candidates"]["TSLA"]
        self.assertIn(c["status"], ("no-order", "blocked"))
        self.assertEqual(st["positions"], {})

    def test_the_scanner_feeds_candidates_and_the_gates_hold(self):
        code, out, err, st, work = self.replay("--mode", "auto", "--scan", "--wall", WALL)
        self.assertEqual(code, 0, err)
        c = st["candidates"]
        self.assertGreater(len(c), 10)
        self.assertEqual(c["TSLA"]["status"], "open")
        others = {s: v for s, v in c.items() if s != "TSLA"}
        self.assertTrue(all(v["status"] in ("skipped", "expired", "waiting") for v in others.values()), others)
        self.assertTrue(any("no 60-day history" in " ".join(v["reasons"]) for v in others.values()))
        self.assertTrue(any("minutes old" in " ".join(v["reasons"]) for v in others.values()))
        self.assertEqual(len(st["positions"]), 1)

    def test_status_reads_the_session(self):
        code, out, err, st, work = self.replay("--mode", "auto")
        code, text, err = run(["status", "--dir", work, "--events", "3"])
        self.assertEqual(code, 0)
        self.assertIn("auto mode, sim broker, PAPER account (replay)", text)
        self.assertIn("open   TSLA: 8 x $364.18", text)
        self.assertIn("FILLED buy 8 at $364.18", text)
        code, text, err = run(["status", "--dir", self.d])
        self.assertEqual(code, 1)
        self.assertIn("no session yet", text)

    def test_init_writes_both_configs_and_keeps_them(self):
        work = os.path.join(self.d, "init")
        code, out, err = run(["init", "--dir", work])
        self.assertEqual(code, 0)
        for f in ("config.json", "bot.json"):
            self.assertTrue(os.path.exists(os.path.join(work, "trader", f)))
        with open(os.path.join(work, "trader", "config.json")) as fh:
            self.assertIs(json.load(fh)["paper"], True)
        with open(os.path.join(work, "trader", "bot.json")) as fh:
            self.assertEqual(json.load(fh)["execution"]["mode"], "dry-run")
        code, out, err = run(["init", "--dir", work])
        self.assertIn("kept", out)


class Bars(unittest.TestCase):
    def test_the_live_tick_is_not_a_bar(self):
        src = bot.BarSource(BARS, tempfile.gettempdir())
        now = bot.hhmm_to_utc(DAY, "10:55")
        closed, tick = src.bars("TSLA", now)
        self.assertEqual(bot.to_eastern(closed[-1]["t"]).strftime("%H:%M"), "10:45")
        self.assertEqual(bot.to_eastern(tick["t"]).strftime("%H:%M:%S"), "10:49:45")
        self.assertEqual(tick["price"], 366.07)
        closed, tick = src.bars("TSLA", bot.hhmm_to_utc(DAY, "10:15"))
        self.assertEqual(bot.to_eastern(closed[-1]["t"]).strftime("%H:%M"), "10:10")
        self.assertIsNone(tick)

    def test_a_wall_serves_many_symbols_and_a_file_falls_back_to_it(self):
        d = tempfile.mkdtemp(prefix="bot-wall-")
        try:
            wall = bot.BarSource(WALL, d)
            self.assertEqual(wall.kind, "wall")
            self.assertTrue(wall.has("AAPL"))
            folder = wall.entry_path("AAPL")
            self.assertTrue(os.path.exists(os.path.join(folder, "AAPL.json")))
            src = bot.BarSource(BARS, d, fallback=wall)
            self.assertEqual(src.entry_path("TSLA"), BARS)
            self.assertEqual(src.entry_path("AAPL"), folder)
            closed, _ = src.bars("AAPL", bot.hhmm_to_utc(DAY, "10:00"))
            self.assertEqual(len(closed), 6)
            with self.assertRaises(bot.BotError):
                bot.BarSource(BARS, d).raw("AAPL")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class App(unittest.TestCase):
    """The installed-app paths: the exe runs the tools itself, data lives in a home folder, a menu opens."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bot-app-")
        self.old_home = os.environ.get("DAYTRADERBOT_HOME")
        os.environ["DAYTRADERBOT_HOME"] = self.d

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("DAYTRADERBOT_HOME", None)
        else:
            os.environ["DAYTRADERBOT_HOME"] = self.old_home
        shutil.rmtree(self.d, ignore_errors=True)

    def test_tool_dispatch_runs_a_pack_script_in_process(self):
        code, out, err = run(["--tool", bot.TOOLS["exit"], "plan", "--symbol", "TSLA", "--entry", "364.18",
                              "--or-high", "362.18", "--or-low", "354.47", "--stop", "or-mid", "--target", "1R",
                              "--date", "2026-10-19", "--json"])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan["stop"], plan["target"]), (358.32, 370.04))
        code, out, err = run(["--tool", os.path.join(self.d, "missing.py")])
        self.assertEqual(code, 2)
        self.assertEqual(run(["--tool"])[0], 2)

    def test_tool_command_uses_python_here_and_the_exe_when_frozen(self):
        self.assertEqual(bot.tool_command("x.py"), [sys.executable, "x.py"])
        old = bot.FROZEN
        bot.FROZEN = True
        try:
            self.assertEqual(bot.tool_command("x.py"), [sys.executable, "--tool", "x.py"])
        finally:
            bot.FROZEN = old

    def test_home_folder_comes_from_the_environment_and_is_the_default_dir(self):
        self.assertEqual(bot.default_home(), self.d)
        code, out, err = run(["init"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "trader", "bot.json")))
        os.environ.pop("DAYTRADERBOT_HOME")
        self.assertEqual(bot.default_home(), os.path.abspath("."))

    def test_a_first_run_writes_the_example_limits(self):
        with redirect_stdout(io.StringIO()) as out:
            path = bot._ensure_config(self.d)
        self.assertTrue(os.path.exists(path))
        self.assertIn("wrote the example limits", out.getvalue())
        with open(path) as fh:
            self.assertIs(json.load(fh)["paper"], True)
        with redirect_stdout(io.StringIO()) as out:
            bot._ensure_config(self.d)
        self.assertEqual(out.getvalue(), "")

    def test_the_menu_runs_a_choice_and_quits(self):
        answers = iter(["3", "", "9", "", "q"])
        out = io.StringIO()
        a = type("A", (), {"dir": self.d, "once": False, "no_prompt": True, "pause": False})()
        with redirect_stdout(out):
            code = bot.cmd_menu(a, input_fn=lambda prompt: next(answers), out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Day Trader Bot %s" % bot.VERSION, text)
        self.assertIn("> DayTraderBot replay --fixtures --fresh --mode auto", text)
        self.assertIn("FILLED buy 8 at $364.18", text)
        self.assertIn("> DayTraderBot status --events 15", text)
        self.assertIn("open   TSLA: 8 x $364.18", text)
        self.assertTrue(os.path.isdir(os.path.join(self.d, "trader", "replays", "2026-10-19")))

    def test_the_menu_handles_a_bad_choice_and_an_eof(self):
        answers = iter(["x", ""])
        out = io.StringIO()
        a = type("A", (), {"dir": self.d, "once": False, "no_prompt": True, "pause": False})()
        code = bot.cmd_menu(a, input_fn=lambda prompt: next(answers), out=out)
        self.assertEqual(code, 0)
        self.assertIn("Pick a number", out.getvalue())
        code = bot.cmd_menu(a, input_fn=lambda prompt: (_ for _ in ()).throw(EOFError()), out=io.StringIO())
        self.assertEqual(code, 0)

    def test_the_packaging_files_cover_the_pack(self):
        inst = os.path.join(ROOT, "installer")
        with open(os.path.join(inst, "DayTraderBot.spec"), encoding="utf-8") as fh:
            spec = fh.read()
        for skill in ("trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
                      "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
                      "trader-pelosi", "trader-bot"):
            self.assertIn('"%s"' % skill, spec, skill)
        for fixture in ("TSLA-2026-10-19-5m.json", "TSLA-60d5m.json.gz", "wall5m-2026-10-19.json.gz", "trader-news"):
            self.assertIn(fixture, spec, fixture)
        self.assertIn('name="DayTraderBot"', spec)
        self.assertIn("console=True", spec)
        with open(os.path.join(inst, "DayTraderBot.iss"), encoding="utf-8") as fh:
            iss = fh.read()
        self.assertIn('#define MyAppVersion "%s"' % bot.VERSION, iss)
        self.assertIn("DayTraderBot.exe", iss)
        self.assertIn("--pause replay --fixtures --fresh --mode auto", iss)
        self.assertIn("PrivilegesRequired=lowest", iss)
        with open(os.path.join(inst, "build.ps1"), encoding="utf-8") as fh:
            ps1 = fh.read()
        self.assertIn("installer\\DayTraderBot.spec", ps1)
        self.assertIn("DayTraderBot.iss", ps1)
        self.assertIn("replay --fixtures --fresh --mode auto", ps1)
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".github", "workflows", "windows-installer.yml")))


class Config(unittest.TestCase):
    def test_defaults_merge_under_the_user_file(self):
        m = bot.merged(bot.DEFAULTS, {"symbols": ["AAPL"], "execution": {"mode": "auto"}, "extra": 1})
        self.assertEqual(m["symbols"], ["AAPL"])
        self.assertEqual(m["execution"]["mode"], "auto")
        self.assertEqual(m["execution"]["broker"], "sim")
        self.assertEqual(m["extra"], 1)
        self.assertEqual(m["plan"], bot.DEFAULTS["plan"])

    def test_the_example_is_dry_run_sim_and_matches_the_defaults_keys(self):
        with open(os.path.join(ROOT, "trader-bot", "bot.example.json")) as fh:
            ex = json.load(fh)
        self.assertEqual(ex["execution"]["mode"], "dry-run")
        self.assertEqual(ex["execution"]["broker"], "sim")
        self.assertEqual(sorted(ex), sorted(bot.DEFAULTS))
        for k, v in bot.DEFAULTS.items():
            if isinstance(v, dict):
                self.assertEqual(sorted(ex[k]), sorted(v), k)

    def test_bad_mode_and_broker_are_refused(self):
        d = tempfile.mkdtemp(prefix="bot-cfg-")
        try:
            os.makedirs(os.path.join(d, "trader"))
            shutil.copy(CFG, os.path.join(d, "trader", "config.json"))
            with self.assertRaises(bot.BotError):
                bot.Bot(d, {}, mode="yolo")
            with self.assertRaises(bot.BotError):
                bot.Bot(d, {}, broker="robinhood")
            with self.assertRaises(bot.BotError):
                bot.Bot(d, {}, broker="alpaca", replay=True)
            os.environ.pop("ALPACA_API_KEY", None)
            os.environ.pop("APCA_API_KEY_ID", None)
            with self.assertRaises(bot.BotError) as cm:
                bot.Bot(d, {}, broker="alpaca")
            self.assertIn("ALPACA_API_KEY", str(cm.exception))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_alpaca_bracket_body(self):
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "k", "s"
        try:
            a = bot.AlpacaBroker(paper=True)
            self.assertEqual(a.base, bot.ALPACA_PAPER)
            sent = {}

            def fake(method, path, body=None):
                sent.update({"method": method, "path": path, "body": body})
                return {"id": "o1", "status": "filled", "filled_qty": "8", "filled_avg_price": "364.2"}
            a.call = fake
            fill = a.place({"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
                            "order_class": "bracket", "take_profit_limit_price": "370.04",
                            "stop_loss_stop_price": "358.32"}, None, wait_seconds=1)
            self.assertEqual((sent["method"], sent["path"]), ("POST", "/v2/orders"))
            self.assertEqual(sent["body"]["order_class"], "bracket")
            self.assertEqual(sent["body"]["take_profit"], {"limit_price": "370.04"})
            self.assertEqual(sent["body"]["stop_loss"], {"stop_price": "358.32"})
            self.assertEqual((fill["filled_qty"], fill["filled_price"]), (8, 364.2))
            self.assertEqual(bot.AlpacaBroker(paper=False).base, bot.ALPACA_LIVE)
        finally:
            os.environ.pop("ALPACA_API_KEY", None)
            os.environ.pop("ALPACA_SECRET_KEY", None)


if __name__ == "__main__":
    unittest.main()
