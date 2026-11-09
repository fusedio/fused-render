"""Bearer keys for the `/v1` local model gateway (docs/LOCAL_MODEL_GATEWAY.md §1).

Two kinds, one `is_valid` check:

* **The machine key** — `machine_key()` — one urlsafe 32-byte secret persisted
  at `<home>/ai/v1_key` with mode 0600, for tools the user configures by hand
  (an OpenAI-compatible client pointed at `http://127.0.0.1:<port>/v1`). Minted
  on first read, re-read from disk after; the file is the source of truth so
  a key the user pasted into another tool survives a restart.
* **Session keys** — `mint_session_key()` — in-memory only, registered for the
  life of a child process the OpenCode host spawns and `release_key`d when it
  exits. Never written anywhere: a restart invalidates them by construction.

Pure stdlib. `home_dir` is the same helper the worker files use
(`fused_render.shell.storage.home_dir`), so the key lands beside
`<home>/ai/workers`.
"""

from __future__ import annotations

import os
import secrets
import threading

_KEY_BYTES = 32
_lock = threading.Lock()
#: Live session keys. A set rather than a dict: nothing is attached to a key
#: beyond "it is valid right now".
_session_keys: set[str] = set()


def key_path() -> str:
    """Where the machine key lives: `<home>/ai/v1_key`."""
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), "ai", "v1_key")


def machine_key() -> str:
    """The persisted per-machine key, minted on first call.

    Written with `O_CREAT | O_EXCL` at 0600 so a concurrent first call cannot
    hand two callers two different keys — the loser re-reads the winner's.
    """
    path = key_path()
    with _lock:
        existing = _read(path)
        if existing:
            return existing
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fresh = secrets.token_urlsafe(_KEY_BYTES)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _read(path) or fresh
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(fresh)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return fresh


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value or None


def mint_session_key() -> str:
    """A fresh in-memory key, valid until `release_key`."""
    key = secrets.token_urlsafe(_KEY_BYTES)
    with _lock:
        _session_keys.add(key)
    return key


def release_key(key: str) -> None:
    """Forget a session key. Unknown keys (and the machine key) are a no-op."""
    with _lock:
        _session_keys.discard(key)


def is_valid(key: str | None) -> bool:
    """Is `key` the machine key or a live session key? Constant-time compares."""
    if not key or not isinstance(key, str):
        return False
    with _lock:
        if any(secrets.compare_digest(key, live) for live in _session_keys):
            return True
    try:
        machine = machine_key()
    except OSError:
        return False
    return secrets.compare_digest(key, machine)


def _reset_for_tests() -> None:
    with _lock:
        _session_keys.clear()
