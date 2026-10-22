# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Day Trader Bot.

    pyinstaller --noconfirm installer/DayTraderBot.spec

Builds dist/DayTraderBot/ (one folder): DayTraderBot.exe plus the pack's
twelve skill folders and the fixtures the reel replay needs, under pack/.
The exe is the Python that runs the tool scripts (bot.py answers --tool),
so the installed app needs no Python on the machine. One folder, not one
file: the bot starts the tool scripts as subprocesses many times a session,
and a one-file exe would unpack itself on every one of them.
"""

import glob
import os

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(SPEC_DIR)
ENTRY = os.path.join(ROOT, "trader-bot", "bot.py")

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
]

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest", "test", "distutils", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DayTraderBot",
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DayTraderBot",
)
