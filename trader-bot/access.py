"""The code that opens Day Trader Bot.

Day Trader Code writes a random 6-digit code into trader/state/access.json.
The bot and the code window are often started from different folders, so the
code is saved in the bot's data folder (%USERPROFILE%\\DayTraderBot on
Windows) and the bot accepts a matching code from any folder it might have
used. The code is not a broker password and it is not sent anywhere. It
expires after 12 hours. Standard library only. Python 3.8.
"""

import datetime
import hmac
import json
import os
import secrets
import sys
import tempfile

ALPHABET = "0123456789"
LENGTH = 6
TTL_SECONDS = 12 * 60 * 60

# Tests append extra folders here so a code saved in one place is accepted
# when the other window looks somewhere else.
_EXTRA = []


def user_data_home():
    """The installed app's data folder. Both windows share the code here."""
    env = os.environ.get("DAYTRADERBOT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    else:
        profile = os.path.expanduser("~")
    return os.path.join(profile, "DayTraderBot")


def code_path(home):
    return os.path.join(home, "trader", "state", "access.json")


def _add(homes, path):
    if not path:
        return
    path = os.path.abspath(os.path.expanduser(path))
    if path not in homes:
        homes.append(path)


def write_homes(preferred=None):
    """Where a new code is saved. The user data folder is always included."""
    homes = []
    _add(homes, preferred)
    _add(homes, user_data_home())
    for path in _EXTRA:
        _add(homes, path)
    return homes


def search_homes(preferred=None):
    """Where the bot looks. Includes older locations from 2.5.0."""
    homes = write_homes(preferred)
    try:
        _add(homes, os.getcwd())
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        _add(homes, os.path.dirname(os.path.abspath(sys.executable)))
    return homes


def normalize(text):
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum())


def _now(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    if now.tzinfo is None:
        return now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).replace(microsecond=0)


def _stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def read(home):
    path = code_path(home)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get("code") or not doc.get("created"):
        return None
    return doc


def _write(home, doc):
    path = code_path(home)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".access-", suffix=".tmp", dir=folder or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return doc


def issue(home, now=None):
    """A new random code, saved under home only. Returns the document."""
    when = _now(now)
    doc = {"code": "".join(secrets.choice(ALPHABET) for _ in range(LENGTH)),
           "created": _stamp(when), "ttl_seconds": TTL_SECONDS}
    return _write(home, doc)


def publish(preferred=None, now=None):
    """One new code, saved in every folder either window might read."""
    when = _now(now)
    doc = {"code": "".join(secrets.choice(ALPHABET) for _ in range(LENGTH)),
           "created": _stamp(when), "ttl_seconds": TTL_SECONDS}
    saved = 0
    last = None
    for home in write_homes(preferred):
        try:
            _write(home, doc)
            saved += 1
        except OSError as exc:
            last = exc
    if not saved:
        raise last or OSError("could not save the code")
    return doc


def status(home, now=None):
    """(doc or None, seconds left). seconds left is 0 when missing or expired."""
    doc = read(home)
    when = _now(now)
    if not doc:
        return None, 0
    created = _parse(doc.get("created"))
    if created is None:
        return None, 0
    ttl = int(doc.get("ttl_seconds") or TTL_SECONDS)
    left = ttl - int((when - created).total_seconds())
    if left <= 0:
        return doc, 0
    return doc, left


def current(preferred=None, now=None):
    """The live code with the most time left, from any folder either window uses."""
    best, best_left = None, 0
    for home in search_homes(preferred):
        doc, left = status(home, now)
        if doc and left > best_left:
            best, best_left = doc, left
    return best, best_left


def check(home, typed, now=None):
    """(True, "") when typed is a current code in any shared folder."""
    got = normalize(typed)
    found = False
    expired = False
    for folder in search_homes(home):
        doc, left = status(folder, now)
        if not doc:
            continue
        found = True
        code = normalize(doc.get("code"))
        if got and code and len(got) == len(code) and hmac.compare_digest(got, code):
            if left > 0:
                return True, ""
            expired = True
    if not found:
        return False, "No current code. Open Day Trader Code and press Generate."
    if expired:
        return False, "That code has expired. Press Generate in Day Trader Code."
    if not got:
        return False, "Type the code Day Trader Code is showing."
    return False, "That code does not match. Use the code Day Trader Code is showing."
