"""The connect credential store: one record per provider (SPEC-connect-oauth §5).

The one place this repo's "no credential store" rule (D402) bends, and only
for vendors that keep no store of their own to delegate to. A vendor CLI that
does (`gh`, `hf`) is used instead; its kind stores no token here.

**Backends.** macOS: the login Keychain through `security`, the way
`bots/browser.py` keeps the profile key; service `fused-render.connect.<key>`,
account `<key>`, the record's compact JSON as the password. Everywhere else: an
owner-only (`0600`) file at `<home_dir()>/connect/<key>.json`, in a `0700`
folder, written to a temp file in the same folder and `os.replace`d so a crash
mid-write leaves the old record, never half of one.
`FUSED_RENDER_CONNECT_STORE=file|keychain` forces a backend (tests and CI use
`file`).

`keyring` on Linux/Windows was considered and left out of the first wave: it
is not a dependency, its backends vary by desktop session (a headless box has
none and falls back to plaintext anyway), and the card says which backend holds
the token, so the file is the honest default rather than a silent one.

**The Keychain password travels on `security`'s argv**, as the precedent's
does: `security -i` reading commands from stdin would hide it from `ps`, but its
line parser and limits are undocumented and a record with a DCR registration
runs to kilobytes. Same-user processes can read argv for the instant the call
runs; they cannot read the Keychain item afterwards without a prompt.

**Record** (`get`/`put`): `{"kind", "resource"?, "client_id"?,
"client_registration"?, "access", "refresh"?, "expires_at"? (epoch seconds),
"token_type"?, "scope"?, "account"?, "extra"?}`. This module does not inspect
it beyond "is a dict"; the manager owns its meaning.

**Never log a record**, nor anything read from one: log lines name the key,
the backend and the outcome only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

STORE_ENV = "FUSED_RENDER_CONNECT_STORE"
SERVICE_PREFIX = "fused-render.connect."
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
#: `security`'s exit status for "the specified item could not be found".
_SEC_NOT_FOUND = 44


def _check_key(key: str) -> str:
    # The key becomes a file name and a Keychain service; nothing else gets in.
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(f"not a provider key: {key!r}")
    return key


def backend() -> str:
    """"keychain" | "file" — which one holds records on this machine."""
    forced = (os.environ.get(STORE_ENV) or "").strip().lower()
    if forced in ("file", "keychain"):
        return forced
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    return "file"


# ------------------------------------------------------------------ file ---

def _dir() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "connect")


def _path(key: str) -> str:
    return os.path.join(_dir(), key + ".json")


def _file_get(key: str) -> dict | None:
    try:
        with open(_path(key), encoding="utf-8") as f:
            rec = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("connect store: record for %s is unreadable; treating as absent", key)
        return None
    return rec if isinstance(rec, dict) else None


def _file_put(key: str, record: dict) -> None:
    d = _dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    # mkstemp creates the file 0600 before a byte is written.
    fd, tmp = tempfile.mkstemp(prefix="." + key + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _path(key))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _file_delete(key: str) -> None:
    try:
        os.unlink(_path(key))
    except FileNotFoundError:
        pass


# -------------------------------------------------------------- keychain ---

def _security(*args: str) -> subprocess.CompletedProcess:
    """Run `security` (the seam tests replace). Never logs its arguments."""
    return subprocess.run(["security", *args], capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=30)


def _kc_get(key: str) -> dict | None:
    r = _security("find-generic-password", "-s", SERVICE_PREFIX + key, "-a", key, "-w")
    if r.returncode == _SEC_NOT_FOUND:
        return None
    if r.returncode != 0:
        logger.warning("connect store: keychain read for %s failed (exit %s)", key, r.returncode)
        return None
    try:
        rec = json.loads(r.stdout.strip())
    except ValueError:
        logger.warning("connect store: keychain item for %s is not a record; treating as absent", key)
        return None
    return rec if isinstance(rec, dict) else None


def _kc_put(key: str, record: dict) -> None:
    # ensure_ascii: `find-generic-password -w` prints hex for a non-printable
    # password, so the stored text must be plain printable ASCII.
    value = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
    r = _security("add-generic-password", "-U", "-s", SERVICE_PREFIX + key, "-a", key,
                  "-l", f"fused-render: {key} connection",
                  "-j", "A fused-render Connections sign-in. Deleting it disconnects the service.",
                  "-w", value)
    if r.returncode != 0:
        raise RuntimeError(f"could not store the {key} connection in the Keychain "
                           f"(security exit {r.returncode})")


def _kc_delete(key: str) -> None:
    r = _security("delete-generic-password", "-s", SERVICE_PREFIX + key, "-a", key)
    if r.returncode not in (0, _SEC_NOT_FOUND):
        raise RuntimeError(f"could not delete the {key} connection from the Keychain "
                           f"(security exit {r.returncode})")


# ------------------------------------------------------------------- API ---

def get(key) -> dict | None:
    _check_key(key)
    return _kc_get(key) if backend() == "keychain" else _file_get(key)


def put(key, record: dict) -> None:
    _check_key(key)
    if not isinstance(record, dict):
        raise TypeError("a connect record is a dict")
    if backend() == "keychain":
        _kc_put(key, record)
    else:
        _file_put(key, record)
    logger.debug("connect store: saved %s (%s)", key, backend())


def delete(key) -> None:
    _check_key(key)
    if backend() == "keychain":
        _kc_delete(key)
    else:
        _file_delete(key)
    logger.debug("connect store: deleted %s (%s)", key, backend())
