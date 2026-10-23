#!/usr/bin/env python3
"""Write latest.json next to a built installer, for the app's one-click update.

    python3 installer/make_manifest.py installer/Output/DayTraderBot-Setup-2.3.0.exe
    python3 installer/make_manifest.py SETUP.exe --url https://github.com/<you>/<repo>/releases/download/v2.3.0/DayTraderBot-Setup-2.3.0.exe

The version is read from trader-bot/bot.py, the notes from CHANGELOG.md's
section for that version. Without --url the installer is referenced by file
name, relative to wherever latest.json ends up (a release's assets, a folder,
a network share), which is right when the two files travel together.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "trader-bot"))
import update  # noqa: E402


def version_of_bot():
    with open(os.path.join(ROOT, "trader-bot", "bot.py"), encoding="utf-8") as fh:
        return re.search(r'^VERSION = "([^"]+)"', fh.read(), re.M).group(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("installer", help="the built DayTraderBot-Setup-<version>.exe")
    ap.add_argument("--url", help="where the installer will be downloaded from (default: its file name)")
    ap.add_argument("--version", help="override the version read from bot.py")
    ap.add_argument("--changelog-url", help="a link to the full changelog")
    ap.add_argument("--out", help="where to write (default: latest.json next to the installer)")
    a = ap.parse_args(argv)
    if not os.path.exists(a.installer):
        print("make_manifest: no file %s" % a.installer, file=sys.stderr)
        return 2
    version = a.version or version_of_bot()
    notes = update.notes_from_changelog(os.path.join(ROOT, "CHANGELOG.md"), version)
    doc = update.write_manifest(a.installer, version, url=a.url, notes=notes, changelog_url=a.changelog_url,
                                out=a.out)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.installer)), update.MANIFEST)
    print("%s: version %s, %s bytes, sha256 %s..." % (out, doc["version"], format(doc["installer"]["size"], ",d"),
                                                      doc["installer"]["sha256"][:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
