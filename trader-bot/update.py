"""One-click updates for the installed Day Trader Bot.

A release publishes two files next to each other: the installer
(DayTraderBot-Setup-<version>.exe) and a small manifest, latest.json:

    {
      "name": "Day Trader Bot",
      "version": "2.3.0",
      "published": "2026-10-23",
      "installer": {"url": "DayTraderBot-Setup-2.3.0.exe", "sha256": "...", "size": 17897207},
      "notes": "What changed, in a few lines.",
      "changelog_url": "https://.../CHANGELOG.md"
    }

The app reads the manifest from `update.url` in trader/bot.json (an https
URL, a file path, or a network share; the installer's url may be relative to
it), compares the version with its own, and on one click downloads the
installer, checks its SHA-256 and size, and runs it silently in place. Inno
Setup upgrades the same AppId without asking anything, then the window comes
back on the new version. Nothing is downloaded or run without that click.

    python3 bot.py update --check            # is there a newer version?
    python3 bot.py update                    # download, verify, install (asks first)
    python3 bot.py update --yes              # the same, no question
    python3 installer/make_manifest.py ...   # the build writes latest.json

Standard library only.
"""

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MANIFEST = "latest.json"
DEFAULT_URL = "https://github.com/yoyossm/day-alpha/releases/latest/download/" + MANIFEST
DEFAULTS = {"url": DEFAULT_URL, "check_on_start": True}
UA = "day-trader-bot-updater/1.0 (python-urllib)"
TIMEOUT = 20


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------- versions

def parse_version(text):
    """'2.10.1' -> (2, 10, 1); a trailing tag ('-beta') sorts under the plain release."""
    m = re.match(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+.]?([A-Za-z].*))?\s*$", str(text or ""))
    if not m:
        raise UpdateError("not a version: %r" % (text,))
    nums = tuple(int(x or 0) for x in m.groups()[:3])
    return nums + ((0,) if not m.group(4) else (-1,))


def newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


# ---------------------------------------------------------------- the manifest

def settings(home):
    """update.url and update.check_on_start from trader/bot.json, over the defaults."""
    out = dict(DEFAULTS)
    path = os.path.join(home, "trader", "bot.json")
    try:
        with open(path, encoding="utf-8") as fh:
            user = (json.load(fh) or {}).get("update") or {}
    except (OSError, ValueError):
        user = {}
    env = os.environ.get("DAYTRADERBOT_UPDATE_URL")
    if env:
        user = dict(user, url=env)
    out.update({k: v for k, v in user.items() if v is not None})
    return out


def is_url(text):
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text or ""))


def read_bytes(location, limit=None, progress=None):
    """The bytes at an http(s) URL, a file:// URL, a path or a UNC share."""
    if is_url(location) and not location.lower().startswith("file:"):
        req = urllib.request.Request(location, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                total = int(r.headers.get("Content-Length") or 0)
                chunks, got = [], 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
                    if limit and got > limit:
                        raise UpdateError("%s is larger than %d bytes" % (location, limit))
                    if progress:
                        progress(got, total)
                return b"".join(chunks)
        except urllib.error.HTTPError as e:
            raise UpdateError("HTTP %s from %s" % (e.code, location))
        except (urllib.error.URLError, OSError) as e:
            raise UpdateError("could not reach %s: %s" % (location, getattr(e, "reason", e)))
    path = location
    if location.lower().startswith("file:"):
        path = urllib.request.url2pathname(urllib.parse.urlparse(location).path)
    try:
        size = os.path.getsize(path)
        if limit and size > limit:
            raise UpdateError("%s is larger than %d bytes" % (path, limit))
        with open(path, "rb") as fh:
            data = fh.read()
        if progress:
            progress(len(data), len(data))
        return data
    except OSError as e:
        raise UpdateError("could not read %s: %s" % (path, e))


def resolve(base, ref):
    """The installer's location, relative to the manifest's when it is not absolute."""
    if is_url(ref) or os.path.isabs(ref) or ref.startswith("\\\\"):
        return ref
    if is_url(base):
        return urllib.parse.urljoin(base, ref)
    return os.path.join(os.path.dirname(os.path.abspath(base)), ref)


def fetch_manifest(url):
    try:
        doc = json.loads(read_bytes(url, limit=1024 * 1024).decode("utf-8"))
    except ValueError as e:
        raise UpdateError("the manifest at %s is not JSON: %s" % (url, e))
    inst = doc.get("installer") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or not doc.get("version") or not isinstance(inst, dict) or not inst.get("url"):
        raise UpdateError("the manifest at %s has no version and installer url" % url)
    parse_version(doc["version"])
    doc["_source"] = url
    doc["installer"]["_location"] = resolve(url, inst["url"])
    return doc


def check(url, current):
    """{'current', 'latest', 'available', 'manifest', 'notes'} for the manifest at url."""
    m = fetch_manifest(url)
    return {"current": current, "latest": m["version"], "available": newer(m["version"], current),
            "manifest": m, "notes": (m.get("notes") or "").strip(), "published": m.get("published"),
            "source": url}


# ---------------------------------------------------------------- download and install

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(manifest, dest_dir=None, progress=None):
    """The installer, saved under dest_dir (default: the temp folder) and verified
    against the manifest's sha256 and size. Returns its path."""
    inst = manifest["installer"]
    location = inst.get("_location") or inst["url"]
    name = inst.get("file") or os.path.basename(urllib.parse.urlparse(location).path if is_url(location)
                                                else location) or "DayTraderBot-Setup.exe"
    if not re.match(r"^[\w.-]+\.exe$", name, re.I):
        raise UpdateError("the manifest names an installer that is not an .exe: %r" % name)
    folder = dest_dir or os.path.join(tempfile.gettempdir(), "DayTraderBot-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    if os.path.exists(path) and inst.get("sha256") and sha256_of(path) == inst["sha256"].lower():
        return path  # the same file from an earlier attempt
    data = read_bytes(location, limit=500 * 1024 * 1024, progress=progress)
    if inst.get("size") and int(inst["size"]) != len(data):
        raise UpdateError("the download is %d bytes, the manifest says %s" % (len(data), inst["size"]))
    if not inst.get("sha256"):
        raise UpdateError("the manifest has no sha256 for the installer; refusing to run an unverified file")
    digest = hashlib.sha256(data).hexdigest()
    if digest != inst["sha256"].lower():
        raise UpdateError("the download's SHA-256 does not match the manifest (got %s, want %s)"
                          % (digest[:16], inst["sha256"][:16]))
    with open(path + ".part", "wb") as fh:
        fh.write(data)
    os.replace(path + ".part", path)
    return path


def installer_args(path, relaunch=None):
    """The command that upgrades in place: Inno Setup's silent switches, then the
    app again. The chain runs in its own console so it outlives the caller."""
    setup = [path, "/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS"]
    if not relaunch:
        return setup, False
    line = 'start "" /wait "%s" %s && start "" "%s"' % (path, " ".join(setup[1:]), relaunch)
    return ["cmd", "/c", line], True


def install(path, relaunch=None):
    """Start the installer and return at once; the caller exits so files can be replaced."""
    if sys.platform != "win32":
        raise UpdateError("the installer is a Windows program; on this system run it yourself: %s" % path)
    args, via_cmd = installer_args(path, relaunch)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, creationflags=flags, close_fds=True)
    return args


def app_exe():
    """The window's exe next to this frozen program, for the relaunch; None from source."""
    if not getattr(sys, "frozen", False):
        return None
    folder = os.path.dirname(sys.executable)
    cand = os.path.join(folder, "DayTraderBot.exe")
    return cand if os.path.exists(cand) else None


# ---------------------------------------------------------------- the build side

def write_manifest(exe_path, version, url=None, notes="", changelog_url=None, out=None, published=None):
    """latest.json for a built installer. url is where the installer will be
    served from (default: its file name, relative to the manifest)."""
    doc = {"name": "Day Trader Bot", "version": str(version),
           "published": published or datetime.date.today().isoformat(),
           "installer": {"url": url or os.path.basename(exe_path), "file": os.path.basename(exe_path),
                         "sha256": sha256_of(exe_path), "size": os.path.getsize(exe_path)},
           "notes": (notes or "").strip()}
    if changelog_url:
        doc["changelog_url"] = changelog_url
    parse_version(doc["version"])
    path = out or os.path.join(os.path.dirname(os.path.abspath(exe_path)), MANIFEST)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    return doc


def notes_from_changelog(path, version):
    """The section for one version in a Keep-a-Changelog style file ('## 2.3.0 ...')."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    m = re.search(r"^##\s+\[?v?%s\]?[^\n]*\n(.*?)(?=^##\s|\Z)" % re.escape(str(version)), text, re.S | re.M)
    return m.group(1).strip() if m else ""
