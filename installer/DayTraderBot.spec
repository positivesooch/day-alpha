# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Day Trader Bot.

    pyinstaller --noconfirm installer/DayTraderBot.spec

Builds dist/DayTraderBot/ (one folder) with two programs that share one
_internal/: DayTraderBot.exe, the window (no console), and
DayTraderBotCLI.exe, the console program the window drives and the one you
type commands at. The console exe is also the Python that runs the pack's
tool scripts (bot.py answers --tool), so the installed app needs no Python
on the machine. The pack's twelve skill folders and the fixtures the reel
replay needs sit under _internal/pack/. One folder, not one file: the bot
starts the tool scripts as subprocesses many times a session, and a one-file
exe would unpack itself on every one of them.
"""

import glob
import os

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(SPEC_DIR)
ENTRY = os.path.join(ROOT, "trader-bot", "bot.py")
GUI_ENTRY = os.path.join(ROOT, "trader-bot", "gui.py")

SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-pelosi", "trader-bot"]

datas = []
for skill in SKILLS:
    for pattern in ("*.py", "*.json", "SKILL.md"):
        for path in glob.glob(os.path.join(ROOT, skill, pattern)):
            datas.append((path, os.path.join("pack", skill)))

# the reel's day, so `replay --fixtures` works out of the box
FIXTURES = {
    os.path.join("trader-entry", "TSLA-2026-10-19-5m.json"): os.path.join("trader-entry"),
    os.path.join("trader-timemachine", "TSLA-60d5m.json.gz"): os.path.join("trader-timemachine"),
    os.path.join("trader-scanner", "wall5m-2026-10-19.json.gz"): os.path.join("trader-scanner"),
}
for rel, dest in FIXTURES.items():
    datas.append((os.path.join(ROOT, "tests", "fixtures", rel), os.path.join("pack", "tests", "fixtures", dest)))
for path in glob.glob(os.path.join(ROOT, "tests", "fixtures", "trader-news", "*")):
    datas.append((path, os.path.join("pack", "tests", "fixtures", "trader-news")))

datas.append((os.path.join(ROOT, "README.md"), "."))
datas.append((os.path.join(ROOT, "LICENSE"), "."))

# The tool scripts are data, not imports, so PyInstaller cannot see what they
# need. Everything they import from the standard library goes here.
hiddenimports = [
    "argparse", "csv", "datetime", "decimal", "email", "email.utils", "gzip", "hashlib", "html",
    "html.parser", "io", "json", "math", "random", "re", "runpy", "shutil", "statistics", "subprocess",
    "time", "urllib", "urllib.error", "urllib.parse", "urllib.request", "xml", "xml.etree",
    "xml.etree.ElementTree", "zipfile", "zlib", "ssl", "http.client", "http.cookiejar",
    "mt5link",
]

# MetaTrader 5 support (prop-firm accounts) rides along when the official
# package is installed in the building Python; it exists for Windows only, so a
# build elsewhere simply ships without it and the bot says so when asked for mt5.
try:
    import MetaTrader5  # noqa: F401
    import numpy  # noqa: F401  (MetaTrader5 returns its rates as numpy arrays)
    hiddenimports += ["MetaTrader5", "numpy"]
    print("DayTraderBot.spec: bundling MetaTrader5 %s" % getattr(MetaTrader5, "__version__", ""))
except ImportError:
    print("DayTraderBot.spec: MetaTrader5 not installed in this Python; building without it")

EXCLUDES = ["unittest", "pydoc", "doctest", "test", "distutils", "setuptools", "pip"]

cli_a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES + ["tkinter"],
    noarchive=False,
)
gui_a = Analysis(
    [GUI_ENTRY],
    pathex=[ROOT, os.path.join(ROOT, "trader-bot")],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports + ["tkinter", "tkinter.ttk", "tkinter.scrolledtext", "tkinter.messagebox"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
MERGE((cli_a, "DayTraderBotCLI", "DayTraderBotCLI"), (gui_a, "DayTraderBot", "DayTraderBot"))

cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name="DayTraderBotCLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="DayTraderBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    cli_exe,
    cli_a.binaries,
    cli_a.datas,
    gui_exe,
    gui_a.binaries,
    gui_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DayTraderBot",
)
