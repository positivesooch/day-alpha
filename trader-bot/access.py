"""The code that opens Day Trader Bot.

Day Trader Code (a separate window) writes a random code into
trader/state/access.json. The bot asks for that code before it shows the
desk. The code is not a broker password and it is not sent anywhere.
It expires after 15 minutes. Standard library only. Python 3.8.
"""

import datetime
import hmac
import json
import os
import secrets
import tempfile

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 6
TTL_SECONDS = 15 * 60


def code_path(home):
    return os.path.join(home, "trader", "state", "access.json")


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


def issue(home, now=None):
    """A new random code, saved under home. Returns the document."""
    when = _now(now)
    doc = {"code": "".join(secrets.choice(ALPHABET) for _ in range(LENGTH)),
           "created": _stamp(when), "ttl_seconds": TTL_SECONDS}
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


def status(home, now=None):
    """(doc or None, seconds left). seconds left is 0 when missing or expired."""
    doc = read(home)
    when = _now(now)
    if not doc:
        return None, 0
    created = _parse(doc.get("created"))
    if created is None:
        return None, 0
    left = TTL_SECONDS - int((when - created).total_seconds())
    if left <= 0:
        return doc, 0
    return doc, left


def check(home, typed, now=None):
    """(True, "") when typed is the current code. Otherwise (False, reason)."""
    doc, left = status(home, now)
    if not doc or left <= 0:
        return False, "No current code. Open Day Trader Code and press Generate."
    got = "".join(ch for ch in (typed or "").upper() if ch.isalnum())
    code = str(doc.get("code") or "")
    if len(got) != len(code) or not hmac.compare_digest(got, code):
        return False, "That code does not match. Use the code Day Trader Code is showing."
    return True, ""
