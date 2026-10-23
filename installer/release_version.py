#!/usr/bin/env python3
"""Pick the version a GitHub push publishes, and stage the built installer.

    python3 installer/release_version.py prepare          # bump the patch if this push did not
    python3 installer/release_version.py stage SETUP.exe  # copy it over releases/ and drop the old setup file

A push that does not change VERSION gets the next patch (2.4.3 -> 2.4.4) so
Check for updates can see a newer build. A push that already changes VERSION
keeps that number. Standard library only. Python 3.8.
"""

import datetime
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "trader-bot", "bot.py")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
RELEASES = os.path.join(ROOT, "releases")
README = os.path.join(RELEASES, "README.md")
NOTES = (
    "- This build replaces the installer on the GitHub release. Check for updates\n"
    "  downloads it from the positivesooch/day-alpha release. FLATTEN ALL still\n"
    "  closes everything."
)


def read_version(text):
    m = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("VERSION is missing from trader-bot/bot.py")
    return m.group(1)


def write_version(text, version):
    new, n = re.subn(r'^VERSION = "[^"]+"', 'VERSION = "%s"' % version, text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("could not update VERSION")
    return new


def next_version(current, previous):
    """(version, bumped). An unchanged version becomes the next patch."""
    if not previous or previous != current:
        return current, False
    parts = current.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit("VERSION must look like 2.4.3, not %r" % current)
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts), True


def insert_notes(text, version, date, body):
    marker = "## %s (" % version
    if marker in text:
        return text
    entry = "## %s (%s)\n\n%s\n\n" % (version, date, body.strip())
    lines = text.splitlines(True)
    out = []
    inserted = False
    for line in lines:
        if not inserted and line.startswith("## "):
            out.append(entry)
            inserted = True
        out.append(line)
    if not inserted:
        out.append("\n" + entry)
    return "".join(out)


def _write(path, text):
    # Always LF. A Windows runner would otherwise rewrite the committed files.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def previous_version():
    try:
        text = subprocess.check_output(
            ["git", "show", "HEAD~1:trader-bot/bot.py"],
            cwd=ROOT, stderr=subprocess.DEVNULL, universal_newlines=True)
    except (subprocess.CalledProcessError, OSError):
        return None
    try:
        return read_version(text)
    except SystemExit:
        return None


def prepare(previous=None, today=None):
    with open(BOT, encoding="utf-8") as fh:
        text = fh.read()
    current = read_version(text)
    version, bumped = next_version(current, previous if previous is not None else previous_version())
    if bumped:
        _write(BOT, write_version(text, version))
        with open(CHANGELOG, encoding="utf-8") as fh:
            log = fh.read()
        _write(CHANGELOG, insert_notes(log, version, today or datetime.date.today().isoformat(), NOTES))
    print(version)
    return version


def stage(exe):
    """Copy the built installer and its latest.json over releases/, and remove older setup files."""
    if not os.path.isfile(exe):
        raise SystemExit("no installer at %s" % exe)
    name = os.path.basename(exe)
    os.makedirs(RELEASES, exist_ok=True)
    dest = os.path.join(RELEASES, name)
    if os.path.abspath(exe) != os.path.abspath(dest):
        shutil.copy(exe, dest)
    manifest = os.path.join(os.path.dirname(os.path.abspath(exe)), "latest.json")
    if os.path.isfile(manifest):
        shutil.copy(manifest, os.path.join(RELEASES, "latest.json"))
    for entry in os.listdir(RELEASES):
        if entry.startswith("DayTraderBot-Setup-") and entry.endswith(".exe") and entry != name:
            os.remove(os.path.join(RELEASES, entry))
    if os.path.isfile(README):
        with open(README, encoding="utf-8") as fh:
            readme = fh.read()
        sha = ""
        latest = os.path.join(RELEASES, "latest.json")
        if os.path.isfile(latest):
            import json
            with open(latest, encoding="utf-8") as fh:
                sha = json.load(fh)["installer"]["sha256"]
        version = name[len("DayTraderBot-Setup-"):-len(".exe")]
        row = "| %s | %s | `%s` |" % (name, version, sha)
        readme2, n = re.subn(r"\| DayTraderBot-Setup-[^|]+\|[^|]+\| `[^`]+` \|", row, readme, count=1)
        if n:
            _write(README, readme2)
    print(dest)


def main(argv):
    if len(argv) < 2 or argv[1] not in ("prepare", "stage"):
        print(__doc__.strip().split("\n")[0], file=sys.stderr)
        return 2
    if argv[1] == "prepare":
        previous = None
        today = None
        args = argv[2:]
        while args:
            if args[0] == "--previous" and len(args) > 1:
                previous = args[1]
                args = args[2:]
            elif args[0] == "--today" and len(args) > 1:
                today = args[1]
                args = args[2:]
            else:
                print("unknown %s" % args[0], file=sys.stderr)
                return 2
        prepare(previous, today)
        return 0
    if len(argv) < 3:
        print("stage needs the built setup exe", file=sys.stderr)
        return 2
    stage(argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
