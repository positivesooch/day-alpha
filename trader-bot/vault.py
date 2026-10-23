"""Broker credentials for the installed app, kept out of the repo and off the screen.

Environment variables still win (ALPACA_API_KEY, ALPACA_SECRET_KEY,
MT5_PASSWORD): a value set there is used as is. Otherwise the window's
Broker tab saves what you type into trader/state/vault.json in your data
folder, encrypted with Windows DPAPI (CryptProtectData, your user account is
the key; another user or another machine cannot read it). On other systems
the file is written readable by you only. Values are never printed, never
logged and never written anywhere else.

    v = Vault(home)
    v.set("alpaca.key", "...")
    v.get("alpaca.secret")      # env first, then the file
    v.names()                   # what is stored, never the values
"""

import base64
import ctypes
import json
import os
import sys

NAMES = {"alpaca.key": "ALPACA_API_KEY", "alpaca.secret": "ALPACA_SECRET_KEY", "mt5.password": "MT5_PASSWORD"}
ALT_ENV = {"alpaca.key": ("APCA_API_KEY_ID",), "alpaca.secret": ("APCA_API_SECRET_KEY",)}


class VaultError(Exception):
    pass


# ---------------------------------------------------------------- DPAPI

if sys.platform == "win32":
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi(data, protect):
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(data, len(data))
        inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(inp), "Day Trader Bot" if protect else None, None, None, None, 0, ctypes.byref(out))
        if not ok:
            raise VaultError("Windows would not %s the credential (error %d)" % (
                "protect" if protect else "unprotect", kernel32.GetLastError()))
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    def protect(text):
        return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")

    def unprotect(blob):
        kind, _, body = blob.partition(":")
        raw = base64.b64decode(body)
        if kind == "dpapi":
            return _dpapi(raw, False).decode("utf-8")
        if kind == "plain":
            return raw.decode("utf-8")
        raise VaultError("unknown credential encoding %r" % kind)
else:
    def protect(text):
        return "plain:" + base64.b64encode(text.encode("utf-8")).decode("ascii")

    def unprotect(blob):
        kind, _, body = blob.partition(":")
        if kind == "plain":
            return base64.b64decode(body).decode("utf-8")
        if kind == "dpapi":
            raise VaultError("this credential was saved on Windows and can only be read there")
        raise VaultError("unknown credential encoding %r" % kind)


# ---------------------------------------------------------------- the file

class Vault(object):
    def __init__(self, home):
        self.home = os.path.abspath(home)
        self.path = os.path.join(self.home, "trader", "state", "vault.json")

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, doc):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"note": "Broker credentials for Day Trader Bot, encrypted for this Windows user (DPAPI) "
                               "or readable by you only. Delete this file to forget them.",
                       "items": doc}, fh, indent=2)
        os.replace(tmp, self.path)
        if sys.platform != "win32":
            os.chmod(self.path, 0o600)

    def from_env(self, name):
        for var in (NAMES.get(name),) + ALT_ENV.get(name, ()):
            if var and os.environ.get(var):
                return os.environ[var]
        return None

    def get(self, name):
        """The value: the environment first, then the file. None when neither has it."""
        env = self.from_env(name)
        if env:
            return env
        blob = (self._load().get("items") or {}).get(name)
        return unprotect(blob) if blob else None

    def source(self, name):
        if self.from_env(name):
            return "environment"
        if name in (self._load().get("items") or {}):
            return "saved"
        return None

    def set(self, name, value):
        if name not in NAMES:
            raise VaultError("unknown credential %s (one of %s)" % (name, ", ".join(sorted(NAMES))))
        value = (value or "").strip()
        if not value:
            return self.delete(name)
        doc = self._load()
        items = dict(doc.get("items") or {})
        items[name] = protect(value)
        self._save(items)

    def delete(self, name):
        doc = self._load()
        items = dict(doc.get("items") or {})
        items.pop(name, None)
        if items:
            self._save(items)
        elif os.path.exists(self.path):
            os.remove(self.path)

    def clear(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def names(self):
        return sorted((self._load().get("items") or {}).keys())

    def status(self):
        """{name: 'environment' | 'saved' | None} for every credential the bot knows."""
        return {n: self.source(n) for n in sorted(NAMES)}
