import copy
import datetime
import gzip
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types
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
        answers = iter(["3", "", "s", "", "q"])
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
        self.assertIn('name="DayTraderBotCLI"', spec)
        self.assertIn("console=True", spec)
        self.assertIn("console=False", spec)
        self.assertIn("gui.py", spec)
        self.assertIn('"mt5link"', spec)
        self.assertIn("MetaTrader5", spec)
        with open(os.path.join(inst, "DayTraderBot.iss"), encoding="utf-8") as fh:
            iss = fh.read()
        self.assertIn('#define MyAppVersion "%s"' % bot.VERSION, iss)
        self.assertIn("DayTraderBot.exe", iss)
        self.assertIn("DayTraderBotCLI.exe", iss)
        self.assertIn("--pause replay --fixtures --fresh --mode auto", iss)
        self.assertIn("PrivilegesRequired=lowest", iss)
        with open(os.path.join(inst, "build.ps1"), encoding="utf-8") as fh:
            ps1 = fh.read()
        self.assertIn("installer\\DayTraderBot.spec", ps1)
        self.assertIn("DayTraderBot.iss", ps1)
        self.assertIn("DayTraderBotCLI.exe\" replay --fixtures --fresh --mode auto", ps1)
        self.assertIn("pip install --quiet --upgrade MetaTrader5", ps1)
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".github", "workflows", "windows-installer.yml")))


class Window(unittest.TestCase):
    """trader-bot/gui.py: the parts that need no screen, and a real window when there is one."""

    @classmethod
    def setUpClass(cls):
        cls.gui = load("trader-bot", "gui")

    def test_argv_for_each_button(self):
        g = self.gui
        self.assertEqual(g.build_argv("reel"), ["replay", "--fixtures", "--fresh", "--mode", "dry-run"])
        self.assertEqual(g.build_argv("reel", "tsla, aapl", "auto", home="/h"),
                         ["replay", "--fixtures", "--fresh", "--mode", "auto", "--symbols", "TSLA,AAPL", "--dir", "/h"])
        self.assertEqual(g.build_argv("reel", "TSLA", "auto", scan=True)[-1], "--scan")
        self.assertEqual(g.build_argv("run", "nvda;msft", "confirm", "alpaca"),
                         ["run", "--mode", "confirm", "--broker", "alpaca", "--symbols", "NVDA,MSFT"])
        self.assertEqual(g.build_argv("status"), ["status", "--events", "30"])
        with self.assertRaises(ValueError):
            g.build_argv("run", mode="yolo")
        with self.assertRaises(ValueError):
            g.build_argv("run", broker="robinhood")
        with self.assertRaises(ValueError):
            g.build_argv("dance")

    def test_prompt_detection_and_describe(self):
        g = self.gui
        self.assertTrue(g.looks_like_prompt("Place this order? Reply yes or no: "))
        self.assertTrue(g.looks_like_prompt("Place this order? Reply yes or no:"))
        self.assertTrue(g.looks_like_prompt("Press Enter for the menu. "))
        self.assertFalse(g.looks_like_prompt("10:15  TSLA: TRIGGERED 10:15 AM at $364.18"))
        self.assertFalse(g.looks_like_prompt(""))
        self.assertEqual(g.describe(["run", "--mode", "auto", "--dir", "/some/where", "--symbols", "TSLA"]),
                         "run --mode auto --symbols TSLA")

    def test_cli_command_is_python_bot_py_here(self):
        cmd = self.gui.cli_command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(os.path.basename(cmd[1]), "bot.py")
        self.assertEqual(self.gui.VERSION, bot.VERSION)

    def test_runner_streams_a_subprocess_and_takes_a_reply(self):
        """The Runner against the real bot: a confirm-mode replay answered with yes through stdin."""
        d = tempfile.mkdtemp(prefix="bot-gui-")
        try:
            os.makedirs(os.path.join(d, "trader"))
            shutil.copy(CFG, os.path.join(d, "trader", "config.json"))
            with open(os.path.join(ROOT, "trader-bot", "bot.example.json")) as fh:
                cfg = json.load(fh)
            cfg["timemachine"]["replays"] = 1000
            with open(os.path.join(d, "trader", "bot.json"), "w") as fh:
                json.dump(cfg, fh)
            r = self.gui.Runner(["replay", "--fixtures", "--fresh", "--mode", "confirm", "--quiet", "--dir", d,
                                 "--bars", BARS, "--history", HIST, "--date", "2026-10-19", "--symbols", "TSLA"]).start()
            text, replied, deadline = "", False, time.time() + 120
            while time.time() < deadline:
                try:
                    item = r.queue.get(timeout=1)
                except Exception:
                    continue
                if item is None:
                    break
                text += item
                if not replied and self.gui.looks_like_prompt(text.rsplit("\n", 1)[-1]):
                    self.assertIn("Place this order? Reply yes or no:", text)
                    self.assertTrue(r.send("yes"))
                    replied = True
            self.assertTrue(replied, text[-500:])
            self.assertEqual(r.returncode, 0, text[-500:])
            self.assertIn("ORDER 1 of 1, PAPER account", text)
            self.assertIn("open   TSLA: 8 x $364.18", text)
            self.assertFalse(r.alive())
            self.assertFalse(r.send("too late"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_window_opens_when_there_is_a_screen(self):
        g = self.gui
        if g.tk is None or not os.environ.get("DISPLAY") and sys.platform != "win32":
            self.skipTest("no Tk or no display")
        d = tempfile.mkdtemp(prefix="bot-gui-")
        try:
            root = g.tk.Tk()
        except g.tk.TclError as e:
            shutil.rmtree(d, ignore_errors=True)
            self.skipTest("Tk cannot open a window here: %s" % e)
        try:
            root.withdraw()
            app = g.App(root, home=d)
            self.assertEqual(app.symbols_var.get(), "TSLA")
            self.assertEqual(app.mode_var.get(), "dry-run")
            self.assertIn("no config yet", app.account_var.get())
            app.mode_var.set("auto")
            app._explain_mode()
            self.assertIn("Paper accounts only", app.mode_help.get())
            app._show("10:15  TSLA: FILLED buy 8\nPlace this order? Reply yes or no: ")
            self.assertEqual(app.reply_lbl.cget("text"), "Reply (the bot is waiting)")
            self.assertEqual(app.status_var.get(), "waiting for your yes or no")
            self.assertIn("FILLED", app.log.get("1.0", "end"))
            app.launch("status")
            self.assertTrue(app.runner.alive() or app.runner.returncode is not None)
            deadline = time.time() + 30
            while app.runner.alive() and time.time() < deadline:
                time.sleep(0.2)
            app._drain()
            self.assertIn("no session yet", app.log.get("1.0", "end"))
            self.assertEqual(str(app.btn_reel.cget("state")), "normal")
        finally:
            root.destroy()
            shutil.rmtree(d, ignore_errors=True)


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


mt5link = load("trader-bot", "mt5link")
SERVER_OFFSET = 3  # the terminal's clock runs UTC+3, as many brokers' do


def ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeMT5(object):
    """Enough of the MetaTrader5 package for the bot: one demo account, one
    symbol (TSLA.US) with the TSLA fixture's candles stamped in server time,
    market deals that open and close positions, and a deal history."""

    def __init__(self, balance=100000.0, equity=None, trade_mode=0, contract=1.0, vmin=1.0, step=1.0,
                 tick_value=0.01, rows=None, ask=364.30):
        self.balance, self.equity, self.trade_mode = balance, balance if equity is None else equity, trade_mode
        self.spec = dict(contract=contract, vmin=vmin, step=step, tick_value=tick_value)
        self.rows = rows if rows is not None else self.fixture_rows()
        self.ask, self.bid = ask, round(ask - 0.02, 2)
        self.tick_time_utc = bot.hhmm_to_utc(DAY, "10:50")
        self.init_kw, self.sent, self.positions, self.deals = None, [], [], []
        self.n = 7000
        self.fail_init = False
        self.down = False

    @staticmethod
    def fixture_rows():
        with gzip.open(HIST) as fh:
            res = json.load(fh)["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        rows = []
        for i, t in enumerate(res["timestamp"]):
            if t % 300 == 0 and q["close"][i] is not None:
                rows.append((t + 3600 * SERVER_OFFSET, q["open"][i], q["high"][i], q["low"][i], q["close"][i],
                             q["volume"][i] or 0, 2, 0))
        return rows

    # -- the package's surface
    def initialize(self, **kw):
        self.init_kw = kw
        return not self.fail_init

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        self.down = True

    def account_info(self):
        return ns(login=51001234, server="Prop-Server", name="Challenge 100k", company="A Prop Firm", currency="USD",
                  balance=self.balance, equity=self.equity, margin_free=self.equity, leverage=30,
                  trade_mode=self.trade_mode)

    def symbol_select(self, sym, enable=True):
        return sym == "TSLA.US"

    def symbol_info(self, sym):
        s = self.spec
        return ns(name=sym, digits=2, point=0.01, trade_contract_size=s["contract"], volume_min=s["vmin"],
                  volume_max=1000.0, volume_step=s["step"], trade_tick_size=0.01, trade_tick_value=s["tick_value"],
                  trade_tick_value_loss=s["tick_value"], filling_mode=2)

    def symbol_info_tick(self, sym):
        return ns(time=mt5link.ts_of(self.tick_time_utc) + 3600 * SERVER_OFFSET, bid=self.bid, ask=self.ask,
                  last=self.bid)

    def copy_rates_from_pos(self, sym, tf, start, count):
        return list(self.rows[-count:])

    def order_send(self, req):
        self.sent.append(dict(req))
        self.n += 1
        if "position" in req:
            p = next((x for x in self.positions if x.ticket == req["position"]), None)
            if p is None:
                return ns(retcode=10013, comment="no such position", order=0, deal=0, volume=0, price=0)
            self._close(p, req["price"], reason=0)
            return ns(retcode=10009, comment="done", order=self.n, deal=self.n + 1000, volume=req["volume"],
                      price=req["price"])
        p = ns(ticket=self.n, symbol=req["symbol"], volume=req["volume"], type=req["type"], price_open=req["price"],
               price_current=req["price"], profit=0.0, sl=req.get("sl"), tp=req.get("tp"), magic=req.get("magic", 0))
        self.positions.append(p)
        return ns(retcode=10009, comment="done", order=self.n, deal=self.n + 1000, volume=req["volume"],
                  price=req["price"])

    def positions_get(self, symbol=None, ticket=None):
        out = list(self.positions)
        if symbol is not None:
            out = [p for p in out if p.symbol == symbol]
        if ticket is not None:
            out = [p for p in out if p.ticket == ticket]
        return tuple(out)

    def history_deals_get(self, position=None, **kw):
        return tuple(d for d in self.deals if position is None or d.position_id == position)

    # -- the test's levers
    def _close(self, p, price, reason):
        pnl = round((price - p.price_open) * p.volume * self.spec["contract"], 2)
        self.positions.remove(p)
        self.n += 1
        self.deals.append(ns(ticket=self.n, position_id=p.ticket, entry=1, reason=reason, price=price, profit=pnl,
                             commission=-0.5, swap=0.0,
                             time=mt5link.ts_of(self.tick_time_utc) + 3600 * SERVER_OFFSET))
        self.balance = round(self.balance + pnl - 0.5, 2)
        self.equity = self.balance
        return pnl

    def hit_target(self, price):
        return self._close(self.positions[0], price, reason=5)


class MT5Link(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bot-mt5-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def order(self, qty="8"):
        return {"symbol": "TSLA", "side": "buy", "qty": qty, "type": "market", "stop_loss_stop_price": "358.32",
                "take_profit_limit_price": "370.04", "plan": {"entry": 364.18, "stop": 358.32, "max_loss": 46.88}}

    def test_symbols_map_then_suffix(self):
        cfg = {"symbols": {"tsla": "TSLA.US"}, "symbol_suffix": ".cash"}
        self.assertEqual(mt5link.firm_symbol(cfg, "TSLA"), "TSLA.US")
        self.assertEqual(mt5link.firm_symbol(cfg, "nvda"), "NVDA.cash")
        self.assertEqual(mt5link.firm_symbol({}, "XAUUSD"), "XAUUSD")

    def test_lots_follow_the_contract_and_fit_the_max_loss(self):
        one = FakeMT5().symbol_info("TSLA.US")
        self.assertEqual(mt5link.lots_for(self.order(), one, 46.88), (8.0, 46.88, []))
        cfd = FakeMT5(contract=100.0, vmin=0.01, step=0.01, tick_value=1.0).symbol_info("TSLA.US")
        lots, risk, notes = mt5link.lots_for(self.order(), cfd, 46.88)
        self.assertEqual((lots, risk, notes), (0.08, 46.88, []))
        lots, risk, notes = mt5link.lots_for(self.order(), cfd, 30.0)
        self.assertEqual((lots, risk), (0.05, 29.3))
        self.assertIn("reduced from 0.08 to 0.05 lots", notes[0])
        big = FakeMT5(contract=100.0, vmin=0.1, step=0.1, tick_value=1.0).symbol_info("TSLA.US")
        lots, risk, notes = mt5link.lots_for(self.order(), big, 46.88)
        self.assertEqual(lots, 0.0)
        self.assertIn("smallest lot (0.1) risks $58.60", notes[-1])
        lots, risk, notes = mt5link.lots_for(self.order("3"), cfd, 500.0)
        self.assertEqual(lots, 0.03)

    def test_rates_become_a_chart_the_tools_read(self):
        rows = FakeMT5.fixture_rows()
        doc = mt5link.rates_to_chart("TSLA", rows, SERVER_OFFSET, last_price=366.07,
                                     last_time_utc=bot.hhmm_to_utc(DAY, "10:49"))
        res = doc["chart"]["result"][0]
        self.assertEqual(res["meta"]["symbol"], "TSLA")
        self.assertEqual(res["meta"]["dataGranularity"], "5m")
        self.assertEqual(res["timestamp"][-1], ts(DAY, "10:45"))
        self.assertAlmostEqual(res["meta"]["previousClose"], 354.67, places=2)  # the last bar before the day
        self.assertEqual(res["meta"]["regularMarketPrice"], 366.07)
        path = os.path.join(self.d, "TSLA.json")
        with open(path, "w") as fh:
            json.dump(doc, fh)
        code, out, err = run(["--tool", bot.TOOLS["entry"], "check", "TSLA", "--json", "--now", "2026-10-19 10:50",
                              "--offline", path])
        ent = json.loads(out)
        self.assertEqual(ent["status"], "triggered")
        self.assertEqual((ent["trigger"]["time"], ent["trigger"]["price"]), ("10:15 AM", 364.18))

    def test_bars_close_on_the_clock_and_the_tick_rides_along(self):
        fake = FakeMT5()
        link = mt5link.Link({"server_utc_offset_hours": SERVER_OFFSET, "symbols": {"TSLA": "TSLA.US"}}, mt5=fake)
        link.connect()
        bars = mt5link.MT5Bars(link, self.d)
        closed, tick = bars.bars("TSLA", bot.hhmm_to_utc(DAY, "10:50"))
        self.assertEqual(closed[-1]["t"], bot.hhmm_to_utc(DAY, "10:45"))
        self.assertEqual(tick, {"t": bot.hhmm_to_utc(DAY, "10:50"), "price": 364.28})
        closed, tick = bars.bars("TSLA", bot.hhmm_to_utc(DAY, "10:47"))
        self.assertEqual(closed[-1]["t"], bot.hhmm_to_utc(DAY, "10:40"))
        self.assertIsNone(tick)
        folder = bars.entry_path("TSLA")
        self.assertTrue(os.path.exists(os.path.join(folder, "TSLA.json")))
        hist = mt5link.MT5History(bars).path_for("TSLA")
        with open(hist) as fh:
            self.assertEqual(len(json.load(fh)["chart"]["result"][0]["timestamp"]), len(fake.rows))
        self.assertIn("A Prop Firm", bars.describe())

    def test_server_offset_is_measured_from_a_tick_when_not_configured(self):
        fake = FakeMT5()
        fake.tick_time_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        link = mt5link.Link({"symbols": {"TSLA": "TSLA.US"}}, mt5=fake)
        link.connect()
        self.assertEqual(link.server_offset("TSLA.US"), SERVER_OFFSET)

    def test_connect_wants_the_password_in_the_environment_only(self):
        os.environ.pop("MT5_PASSWORD", None)
        fake = FakeMT5()
        with self.assertRaises(mt5link.MT5Error) as cm:
            mt5link.Link({"login": 51001234, "server": "Prop-Server"}, mt5=fake).connect()
        self.assertIn("MT5_PASSWORD", str(cm.exception))
        os.environ["MT5_PASSWORD"] = "hunter2"
        try:
            acct = mt5link.Link({"login": 51001234, "server": "Prop-Server"}, mt5=fake).connect()
        finally:
            os.environ.pop("MT5_PASSWORD", None)
        self.assertEqual(fake.init_kw, {"login": 51001234, "password": "hunter2", "server": "Prop-Server"})
        self.assertEqual((acct["kind"], acct["balance"]), ("demo", 100000.0))
        fake.fail_init = True
        with self.assertRaises(mt5link.MT5Error):
            mt5link.Link({}, mt5=fake).connect()

    def guard(self, fake, **prop):
        link = mt5link.Link({"server_utc_offset_hours": SERVER_OFFSET}, mt5=fake)
        link.connect()
        return mt5link.PropGuard(link, {"prop": prop}, os.path.join(self.d, "trader", "state", "mt5.json"))

    def test_prop_guard_measures_the_firms_two_limits_with_a_buffer(self):
        fake = FakeMT5(balance=100000.0)
        g = self.guard(fake, max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0, buffer_pct=20.0)
        now = bot.hhmm_to_utc(DAY, "10:50")
        ok, reason, s = g.check(now, 46.88)
        self.assertTrue(ok)
        self.assertEqual((s["daily_limit"], s["drawdown_limit"], s["daily_room"]), (5000.0, 10000.0, 4000.0))
        self.assertIn("$0.00 of $5,000.00".replace(",", ""), reason.replace(",", ""))
        fake.equity = 96500.0  # a bad morning: $3,500 down from the day's start
        ok, reason, s = g.check(now, 600.0)
        self.assertFalse(ok)
        self.assertIn("prop rule: daily loss", reason)
        self.assertEqual(s["daily_room"], 500.0)
        self.assertIsNone(g.emergency(now))
        fake.equity = 95900.0
        self.assertIn("daily loss limit", g.emergency(now))
        # the day start is remembered across a restart, and it is the firm's day, not New York's
        g2 = self.guard(fake)
        self.assertEqual(g2.state["day_start"], 100000.0)
        self.assertEqual(g2.state["initial_balance"], 100000.0)
        self.assertEqual(g2.firm_day(now), "2026-10-19")
        self.assertEqual(g2.firm_day(bot.hhmm_to_utc(DAY, "17:30")), "2026-10-20")  # 00:30 server time
        # the overall drawdown counts from the account size, over days
        fake.balance = fake.equity = 90500.0
        g3 = self.guard(fake, max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0, buffer_pct=20.0,
                        initial_balance=100000.0)
        later = bot.hhmm_to_utc(DAY + datetime.timedelta(days=2), "10:00")
        ok, reason, s = g3.check(later, 100.0)
        self.assertFalse(ok)
        self.assertIn("prop rule: max drawdown", reason)
        self.assertIn("max drawdown", g3.emergency(later))

    def test_broker_places_a_deal_with_sl_tp_and_reads_the_close_from_history(self):
        fake = FakeMT5(ask=364.30)
        b = mt5link.MT5Broker({"server_utc_offset_hours": SERVER_OFFSET, "symbols": {"TSLA": "TSLA.US"}}, self.d,
                              link=mt5link.Link({"server_utc_offset_hours": SERVER_OFFSET}, mt5=fake))
        b.connect()
        now = bot.hhmm_to_utc(DAY, "10:50")
        fill = b.place(self.order(), now)
        req = fake.sent[-1]
        self.assertEqual((req["symbol"], req["volume"], req["type"], req["sl"], req["tp"]),
                         ("TSLA.US", 8.0, 0, 358.32, 370.04))
        self.assertEqual(req["magic"], 20261019)
        self.assertEqual((fill["filled_qty"], fill["filled_price"], fill["lots"], fill["mt5_symbol"]),
                         (8, 364.3, 8.0, "TSLA.US"))
        pos = {"symbol": "TSLA", "broker_id": fill["broker_id"], "entry": 364.3, "qty": 8, "mt5_symbol": "TSLA.US"}
        self.assertIsNone(b.watch(pos, now))
        self.assertEqual(pos["mark"], 364.3)
        fake.hit_target(370.04)
        hit = b.watch(pos, now)
        self.assertEqual((hit["reason"], hit["price"], hit["pnl"]), ("target", 370.04, round(8 * 5.74 - 0.5, 2)))
        self.assertEqual(hit["at"], now)
        # a flatten sends the opposite deal against the position ticket
        fill2 = b.place(self.order(), now)
        pos2 = {"symbol": "TSLA", "broker_id": fill2["broker_id"], "entry": 364.3, "qty": 8}
        out = b.flatten(pos2, now)
        self.assertEqual(fake.sent[-1]["position"], int(fill2["broker_id"]))
        self.assertEqual(fake.sent[-1]["type"], 1)
        self.assertEqual((out["reason"], out["price"]), ("flat_by", 364.28))
        self.assertEqual(fake.positions, [])
        # too small a budget for the smallest lot is a refusal, not a deal
        fake.spec.update(contract=100.0, vmin=0.1, step=0.1, tick_value=1.0)
        with self.assertRaises(mt5link.MT5Error) as cm:
            b.place(self.order(), now)
        self.assertIn("smallest lot", str(cm.exception))
        self.assertEqual(len(fake.sent), 3)


class MT5Bot(unittest.TestCase):
    """The whole bot on a fake terminal: the pack's tools read the firm's candles,
    the fill is a deal with SL/TP, the prop rules gate and halt."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bot-mt5bot-")
        os.makedirs(os.path.join(self.d, "trader"))
        shutil.copy(CFG, os.path.join(self.d, "trader", "config.json"))
        with open(os.path.join(ROOT, "trader-bot", "bot.example.json")) as fh:
            self.cfg = json.load(fh)
        self.cfg["timemachine"]["replays"] = 2000
        self.cfg["mt5"].update({"server_utc_offset_hours": SERVER_OFFSET, "symbols": {"TSLA": "TSLA.US"}})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def make(self, fake, mode="auto", **kw):
        return bot.Bot(self.d, self.cfg, mode=mode, broker="mt5", news_dir=NEWS, mt5=fake, out=io.StringIO(), **kw)

    def walk(self, b, fake, start, end):
        t = bot.hhmm_to_utc(DAY, start)
        last = bot.hhmm_to_utc(DAY, end)
        while t <= last:
            fake.tick_time_utc = t
            b.tick(t)
            t += datetime.timedelta(minutes=5)

    def test_the_reel_day_fills_on_the_terminal_and_closes_at_the_target(self):
        fake = FakeMT5(ask=364.18)  # the ask is the fill; an ask over the plan's entry would cost reward:risk
        b = self.make(fake)
        self.assertEqual(b.bars.kind, "mt5")
        self.assertEqual(b.account_info["kind"], "demo")
        self.walk(b, fake, "09:30", "10:20")
        c = b.state["candidates"]["TSLA"]
        self.assertEqual(c["status"], "open", c["reasons"])
        self.assertEqual(c["stages"]["entry"]["trigger"]["time"], "10:15 AM")
        self.assertEqual(c["stages"]["timemachine"]["history"]["triggered"], 33)
        self.assertEqual(c["stages"]["size"]["qty"], 8)
        self.assertEqual(c["stages"]["risk"]["verdict"], "PASS")
        self.assertTrue(c["stages"]["prop"]["ok"])
        self.assertEqual(c["stages"]["prop"]["daily_limit"], 5000.0)
        p = b.state["positions"]["TSLA"]
        self.assertEqual((p["qty"], p["entry"], p["lots"], p["mt5_symbol"]), (8, 364.18, 8.0, "TSLA.US"))
        self.assertEqual((p["stop"], p["target"]), (358.32, 370.04))
        self.assertEqual(p["max_loss"], 46.88)
        req = fake.sent[-1]
        self.assertEqual((req["symbol"], req["volume"], req["sl"], req["tp"]), ("TSLA.US", 8.0, 358.32, 370.04))
        with open(os.path.join(self.d, "trader", "state", "broker.json")) as fh:
            log = json.load(fh)["orders"]
        self.assertEqual([o["status"] for o in log], ["approved", "placed", "filled"])
        self.assertEqual((log[0]["auto"], log[0]["broker"]), (True, "mt5"))
        # the terminal closes it at the take-profit; the bot reads the deal and books the broker's own P&L
        fake.hit_target(370.04)
        self.walk(b, fake, "10:25", "10:25")
        p = b.state["positions"]["TSLA"]
        self.assertEqual((p["status"], p["exit_reason"], p["exit"]), ("closed", "target", 370.04))
        self.assertEqual(p["pnl"], round(8 * (370.04 - 364.18) - 0.5, 2))  # the deal's profit less commission
        self.assertEqual(b.state["candidates"]["TSLA"]["status"], "closed")
        self.assertIn("TARGET filled at $370.04", " ".join(e["text"] for e in b.state["events"]))
        self.assertIn("realised +$46.38 on 1 trade", b.summary())

    def test_the_prop_rules_block_a_trade_that_does_not_fit_the_room(self):
        fake = FakeMT5(balance=100000.0, equity=99640.0, ask=364.18)  # $360 down, $40 of room under the buffered $500 limit
        self.cfg["mt5"]["prop"].update({"max_daily_loss_pct": 0.5, "initial_balance": 100000.0})
        b = self.make(fake)
        self.walk(b, fake, "10:15", "10:20")
        c = b.state["candidates"]["TSLA"]
        self.assertEqual(c["status"], "blocked", c["reasons"])
        self.assertIn("prop rule: daily loss", c["reasons"][-1])
        self.assertFalse(c["stages"]["prop"]["ok"])
        self.assertEqual(c["stages"]["prop"]["daily_used"], 360.0)
        self.assertEqual(fake.sent, [])
        self.assertEqual(b.state["positions"], {})

    def test_an_emergency_flattens_the_bots_positions_and_halts_the_day(self):
        fake = FakeMT5(balance=100000.0, equity=99500.0)
        fake.order_send({"action": 1, "symbol": "TSLA.US", "volume": 8.0, "type": 0, "price": 364.3, "magic": 20261019})
        fake.order_send({"action": 1, "symbol": "TSLA.US", "volume": 1.0, "type": 0, "price": 364.3, "magic": 1})
        self.cfg["mt5"]["prop"].update({"max_daily_loss_pct": 0.5, "initial_balance": 100000.0})
        b = self.make(fake)
        self.walk(b, fake, "10:20", "10:20")
        self.assertIn("daily loss limit", b.state["halted"])
        self.assertEqual([p.magic for p in fake.positions], [1])  # someone else's position is left alone
        self.assertEqual(fake.sent[-1]["position"], 7001)
        text = " ".join(e["text"] for e in b.state["events"])
        self.assertIn("PROP RULE EMERGENCY", text)
        self.assertIn("flattened 1 position", text)
        self.assertEqual(b.state["candidates"]["TSLA"]["status"], "blocked")
        self.assertIn("HALTED", b.summary())
        self.walk(b, fake, "10:25", "10:25")
        self.assertEqual(len([r for r in fake.sent if "position" in r]), 1)

    def test_account_kind_against_the_config(self):
        with self.assertRaises(bot.BotError) as cm:
            self.make(FakeMT5(trade_mode=2))
        self.assertIn("REAL account", str(cm.exception))
        live = os.path.join(self.d, "trader", "config.json")
        with open(live) as fh:
            cfg = json.load(fh)
        cfg["paper"] = False
        with open(live, "w") as fh:
            json.dump(cfg, fh)
        with self.assertRaises(bot.BotError) as cm:
            self.make(FakeMT5(trade_mode=2), mode="auto")
        self.assertIn("allow_auto_on_real", str(cm.exception))
        b = self.make(FakeMT5(trade_mode=2), mode="confirm")
        self.assertFalse(b.paper)
        self.cfg["mt5"]["allow_auto_on_real"] = True
        b = self.make(FakeMT5(trade_mode=2), mode="auto")
        self.assertEqual(b.account_info["kind"], "real")
        with self.assertRaises(bot.BotError) as cm:
            self.make(FakeMT5(), replay=True)
        self.assertIn("MetaTrader 5", str(cm.exception))

    def test_the_example_config_and_the_window_know_the_broker(self):
        self.assertIn("mt5", bot.BROKERS)
        self.assertEqual(sorted(bot.DEFAULTS["mt5"]), sorted(mt5link.DEFAULTS))
        self.assertIn(["run", "--mode", "auto", "--broker", "mt5"], [argv for _, _, argv in bot.MENU])
        g = load("trader-bot", "gui")
        self.assertIn("mt5", g.BROKERS)
        self.assertEqual(g.build_argv("run", "TSLA", "auto", "mt5")[:5], ["run", "--mode", "auto", "--broker", "mt5"])


updater = load("trader-bot", "update")


class Updates(unittest.TestCase):
    """The one-click update: manifest, version compare, verified download, the installer command."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bot-upd-")
        self.exe = os.path.join(self.d, "DayTraderBot-Setup-9.9.0.exe")
        with open(self.exe, "wb") as fh:
            fh.write(b"MZ fake installer " * 1000)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def serve(self):
        """A local HTTP server over the temp folder; returns (base_url, stop)."""
        import http.server
        import socketserver
        import threading
        folder = self.d

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=folder, **kw)

            def log_message(self, *a):
                pass
        srv = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def stop():
            srv.shutdown()
            srv.server_close()
        return "http://127.0.0.1:%d/" % srv.server_address[1], stop

    def test_versions_compare_as_numbers(self):
        self.assertEqual(updater.parse_version("v2.10.1"), (2, 10, 1, 0))
        self.assertTrue(updater.newer("2.10.0", "2.9.9"))
        self.assertFalse(updater.newer("2.3.0", "2.3.0"))
        self.assertTrue(updater.newer("2.3.0", "2.3.0-beta"))
        self.assertFalse(updater.newer("2.3.0-rc1", "2.3.0"))
        with self.assertRaises(updater.UpdateError):
            updater.parse_version("latest")

    def test_manifest_is_written_from_the_changelog_and_read_back(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as fh:
            self.assertIn("## %s" % bot.VERSION, fh.read())
        notes = updater.notes_from_changelog(os.path.join(ROOT, "CHANGELOG.md"), bot.VERSION)
        self.assertIn("One-click updates", notes)
        self.assertNotIn("## 2.2.0", notes)
        doc = updater.write_manifest(self.exe, "9.9.0", notes=notes)
        path = os.path.join(self.d, "latest.json")
        with open(path) as fh:
            back = json.load(fh)
        self.assertEqual(back["version"], "9.9.0")
        self.assertEqual(back["installer"]["url"], "DayTraderBot-Setup-9.9.0.exe")
        self.assertEqual(back["installer"]["sha256"], updater.sha256_of(self.exe))
        self.assertEqual(back["installer"]["size"], os.path.getsize(self.exe))
        self.assertEqual(doc["notes"], notes)
        # the build script's wrapper does the same from the command line
        import subprocess as sp
        out = sp.run([sys.executable, os.path.join(ROOT, "installer", "make_manifest.py"), self.exe,
                      "--url", "https://example.test/dl/DayTraderBot-Setup-9.9.0.exe", "--version", "9.9.0"],
                     capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["installer"]["url"], "https://example.test/dl/DayTraderBot-Setup-9.9.0.exe")

    def test_check_and_verified_download_over_http_and_from_a_folder(self):
        updater.write_manifest(self.exe, "9.9.0", notes="Big news.")
        base, stop = self.serve()
        try:
            info = updater.check(base + "latest.json", "2.3.0")
            self.assertTrue(info["available"])
            self.assertEqual((info["latest"], info["notes"]), ("9.9.0", "Big news."))
            self.assertEqual(info["manifest"]["installer"]["_location"], base + "DayTraderBot-Setup-9.9.0.exe")
            seen = []
            dest = os.path.join(self.d, "dl")
            path = updater.download(info["manifest"], dest, progress=lambda g, t: seen.append((g, t)))
            self.assertEqual(os.path.basename(path), "DayTraderBot-Setup-9.9.0.exe")
            self.assertEqual(updater.sha256_of(path), updater.sha256_of(self.exe))
            self.assertEqual(seen[-1], (os.path.getsize(self.exe),) * 2)
            self.assertEqual(updater.download(info["manifest"], dest), path)  # cached, same hash
            # a tampered file is refused
            with open(os.path.join(self.d, "latest.json")) as fh:
                m = json.load(fh)
            m["installer"]["sha256"] = "0" * 64
            with open(os.path.join(self.d, "latest.json"), "w") as fh:
                json.dump(m, fh)
            info = updater.check(base + "latest.json", "2.3.0")
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download(info["manifest"], os.path.join(self.d, "dl2"))
            self.assertIn("SHA-256", str(cm.exception))
            self.assertFalse(os.path.exists(os.path.join(self.d, "dl2", "DayTraderBot-Setup-9.9.0.exe")))
            with self.assertRaises(updater.UpdateError):
                updater.check(base + "missing.json", "2.3.0")
        finally:
            stop()
        # the same manifest from a folder path (a network share works the same way)
        updater.write_manifest(self.exe, "9.9.0")
        info = updater.check(os.path.join(self.d, "latest.json"), "2.3.0")
        self.assertEqual(info["manifest"]["installer"]["_location"], self.exe)
        self.assertFalse(updater.check(os.path.join(self.d, "latest.json"), "9.9.0")["available"])
        self.assertFalse(updater.check(os.path.join(self.d, "latest.json"), "10.0.0")["available"])

    def test_the_installer_command_is_silent_and_relaunches(self):
        args, via_cmd = updater.installer_args(r"C:\t\Setup.exe")
        self.assertEqual(args[:3], [r"C:\t\Setup.exe", "/SILENT", "/NORESTART"])
        self.assertIn("/CLOSEAPPLICATIONS", args)
        self.assertFalse(via_cmd)
        args, via_cmd = updater.installer_args(r"C:\t\Setup.exe", relaunch=r"C:\App\DayTraderBot.exe")
        self.assertEqual(args[:2], ["cmd", "/c"])
        self.assertIn('start "" /wait "C:\\t\\Setup.exe" /SILENT', args[2])
        self.assertTrue(args[2].endswith('&& start "" "C:\\App\\DayTraderBot.exe"'))
        if sys.platform != "win32":
            with self.assertRaises(updater.UpdateError):
                updater.install(self.exe)

    def test_settings_come_from_bot_json_and_the_environment(self):
        os.makedirs(os.path.join(self.d, "trader"))
        os.environ.pop("DAYTRADERBOT_UPDATE_URL", None)
        self.assertEqual(updater.settings(self.d), updater.DEFAULTS)
        with open(os.path.join(self.d, "trader", "bot.json"), "w") as fh:
            json.dump({"update": {"url": r"\\nas\share\latest.json", "check_on_start": False}}, fh)
        s = updater.settings(self.d)
        self.assertEqual((s["url"], s["check_on_start"]), (r"\\nas\share\latest.json", False))
        os.environ["DAYTRADERBOT_UPDATE_URL"] = "https://x.test/latest.json"
        try:
            self.assertEqual(updater.settings(self.d)["url"], "https://x.test/latest.json")
        finally:
            del os.environ["DAYTRADERBOT_UPDATE_URL"]
        self.assertEqual(sorted(bot.DEFAULTS["update"]), sorted(updater.DEFAULTS))

    def test_the_cli_checks_downloads_and_stops_short_of_running_on_a_no(self):
        updater.write_manifest(self.exe, "9.9.0", notes="Big news.")
        manifest = os.path.join(self.d, "latest.json")
        home = os.path.join(self.d, "home")
        os.makedirs(os.path.join(home, "trader"))
        code, out, err = run(["update", "--check", "--dir", home, "--url", manifest])
        self.assertEqual(code, 0, err)
        self.assertIn("Day Trader Bot 9.9.0 is available (you have %s)" % bot.VERSION, out)
        self.assertIn("Big news.", out)
        code, out, err = run(["update", "--dir", home, "--url", manifest, "--yes", "--download-only",
                              "--into", os.path.join(self.d, "saved")])
        self.assertEqual(code, 0, err)
        self.assertIn("Verified DayTraderBot-Setup-9.9.0.exe", out)
        self.assertTrue(os.path.exists(os.path.join(self.d, "saved", "DayTraderBot-Setup-9.9.0.exe")))
        a = type("A", (), {"dir": home, "url": manifest, "check": False, "yes": False, "download_only": False,
                           "into": None, "relaunch": False})()
        out = io.StringIO()
        code = bot.cmd_update(a, input_fn=lambda prompt: "no", out=out)
        self.assertEqual(code, 0)
        self.assertIn("Not now. Nothing was downloaded.", out.getvalue())
        updater.write_manifest(self.exe, bot.VERSION)
        code, out, err = run(["update", "--check", "--dir", home, "--url", manifest])
        self.assertIn("is the newest version", out)
        code, out, err = run(["update", "--check", "--dir", home, "--url", os.path.join(self.d, "nope.json")])
        self.assertEqual(code, 1)
        self.assertIn("update check failed", err)
        self.assertIn(["update"], [argv for _, _, argv in bot.MENU])

    def test_the_one_file_updater_cmd(self):
        path = os.path.join(ROOT, "releases", "DayTraderBot-Update.cmd")
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"@echo off"))
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n\n\n\n", raw.replace(b"\r", b""))
        text = raw.decode("utf-8")
        self.assertEqual(text.count("#" + "POWERSHELL-BEGIN"), 1)  # the batch side spells it in two halves on purpose
        self.assertIn("/SILENT", text)
        self.assertIn("Get-FileHash", text)
        with open(os.path.join(ROOT, "installer", "DayTraderBot.iss"), encoding="utf-8") as fh:
            app_id = re.search(r"AppId=\{\{([0-9A-F-]+)\}", fh.read()).group(1)
        self.assertIn("{%s}_is1" % app_id, text)
        self.assertIn(updater.DEFAULT_URL, text)
        pwsh = shutil.which("pwsh") or shutil.which("powershell") or (
            "/home/ubuntu/pwsh/pwsh" if os.path.exists("/home/ubuntu/pwsh/pwsh") else None)
        if not pwsh:
            self.skipTest("no PowerShell here to run the payload")
        import subprocess as sp
        updater.write_manifest(self.exe, "9.9.0", notes="Big news.")
        env = dict(os.environ, DTB_SELF=path, DTB_ARGS='-Url "%s" -Check' % os.path.join(self.d, "latest.json"))
        env.pop("DAYTRADERBOT_UPDATE_URL", None)
        cmd = ("$f = Get-Content -LiteralPath $env:DTB_SELF -Raw; $i = $f.IndexOf('#' + 'POWERSHELL-BEGIN'); "
               "& ([scriptblock]::Create($f.Substring($i))); exit $LASTEXITCODE")
        out = sp.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Day Trader Bot 9.9.0 is available", out.stdout)
        self.assertIn("Big news.", out.stdout)
        env["DTB_ARGS"] = '-Url "%s" -Yes -DownloadOnly' % os.path.join(self.d, "latest.json")
        env["TEMP"] = os.path.join(self.d, "tmp")
        out = sp.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("SHA-256 matches", out.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.d, "tmp", "DayTraderBot-update", "DayTraderBot-Setup-9.9.0.exe")))
        with open(os.path.join(self.d, "latest.json")) as fh:
            m = json.load(fh)
        m["installer"]["sha256"] = "0" * 64
        m["version"] = "9.9.1"
        with open(os.path.join(self.d, "latest.json"), "w") as fh:
            json.dump(m, fh)
        out = sp.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 1)
        self.assertIn("SHA-256 does not match", out.stdout)


if __name__ == "__main__":
    unittest.main()
