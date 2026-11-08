"""Where a detached session host's queue events go once its server is gone.

A session host (`session_host.py` and its Copilot / Codex / Gemini twins) is a
setsid'd process that deliberately OUTLIVES the server that spawned it, and it
reports `turn_started` / `turn_ended` / `exited` / card events to
`FUSED_RENDER_ORIGIN` — the origin captured at spawn. Quit the app mid-run and
relaunch it, and the new server may well be on a different port (1777 was
still held, so 1778): every event the still-running host sends then goes to a
dead port, the queue owner is never released, and the task never leaves
In Progress (2026-11-08, from a user's diagnostics bundle).

So when a post to that origin fails, the host asks THIS module for the current
one: `<FUSED_RENDER_HOME_DIR>/server.json`, the file `server/app.py`
`write_server_json` publishes at every boot. `FUSED_RENDER_HOME_DIR` is the
server's own `shell.storage.home_dir()` — already branch- and flavor-resolved,
exported by `export_app_env` and inherited by every host through
`_spawn_env` — so it names the SAME install's data folder the dead server
wrote to, never another branch's, flavor's or dev server's. With no
`FUSED_RENDER_HOME_DIR` there is no such guarantee, and nothing is re-resolved
(the old behaviour: the event is dropped).

A server.json origin is only taken when it DIFFERS from the one that just
failed and its recorded `pid` is alive — a stale file from the crashed server
itself names the same dead origin, or a dead pid. The answer is cached per
process, so every later event goes straight to the live server; if that one
dies too, the next failure re-resolves again.

`write_server_json` runs BEFORE uvicorn listens (the lifespan startup sits
between the two), so an event that fires while the relaunched server boots
finds a live pid and a port that still refuses. A host emits each edge once,
so that event is retried against the new origin every `retry_seconds` for at
most `FALLBACK_SECONDS` while the file still names it — bounded, never a
loop. `send_budget` is that whole worst case, for a host about to exit that
joins its last post.

Stdlib only and never imports `fused_render`, like every file in this
directory; each host loads it by file path, a fresh module per load.
"""
import json
import os
import threading
import time
import urllib.request

SERVER_JSON_NAME = "server.json"
# How long a post to a re-resolved origin keeps trying: the relaunched server
# publishes server.json before it listens, and its lifespan startup (queue
# manager, tasks watch, warmups) sits in between.
FALLBACK_SECONDS = 15.0

_lock = threading.Lock()
# The origin a re-resolve switched this process to, or "" while the spawn-time
# `FUSED_RENDER_ORIGIN` is still the one to use.
_resolved = ""


def env_origin() -> str:
    """The spawn-time origin, without a trailing slash, or ""."""
    return (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")


def current() -> str:
    """The origin to post to now: the re-resolved one once a switch happened,
    else the spawn-time `FUSED_RENDER_ORIGIN`. "" means nobody to tell."""
    env = env_origin()
    if not env:
        return ""
    with _lock:
        return _resolved or env


def _server_json_path() -> str:
    home = os.environ.get("FUSED_RENDER_HOME_DIR") or ""
    return os.path.join(home, SERVER_JSON_NAME) if home else ""


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, just not ours to signal
    except (OSError, OverflowError):
        return False
    return True


def reresolve(failed_origin: str) -> str:
    """After a post to `failed_origin` failed: the live server's origin from
    this install's server.json, cached for every later event — or "" when
    there is nothing better to try (no home dir exported, no file, a stale
    file naming the same origin, or a server pid that is gone)."""
    global _resolved
    try:
        failed = (failed_origin or "").rstrip("/")
        path = _server_json_path()
        if not path:
            return ""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return ""
        origin = data.get("origin")
        if not isinstance(origin, str) or not origin.startswith(("http://", "https://")):
            return ""
        origin = origin.rstrip("/")
        if origin == failed or not _pid_alive(data.get("pid")):
            return ""
        with _lock:
            _resolved = origin
        return origin
    except Exception:  # unreadable, half-written, absent: nothing to switch to
        return ""


def _post_once(url: str, data: bytes, timeout: float) -> bool:
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "X-Fused": "1"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception:
        return False


def send_budget(timeout: float, retry_seconds: float) -> float:
    """The longest `send` can take: two tries at the original origin, the
    pause between them, then `FALLBACK_SECONDS` of tries at the re-resolved
    one (the last of which may start just before the deadline) — what a
    host joins its final `exited` post for before it lets itself exit."""
    return 2 * timeout + retry_seconds + FALLBACK_SECONDS + timeout + 1.0


def _post_to_live(failed_origin: str, path: str, data: bytes, timeout: float,
                  retry_seconds: float) -> None:
    """Post to the live origin `reresolve` names, retrying every
    `retry_seconds` for at most `FALLBACK_SECONDS` — the relaunched server
    publishes server.json before it listens. Each round re-reads the file, so
    the tries stop the moment it no longer names a live, different server."""
    deadline = time.monotonic() + FALLBACK_SECONDS
    while True:
        live = reresolve(failed_origin)
        if not live or _post_once(live + path, data, timeout):
            return
        if time.monotonic() + retry_seconds > deadline:
            return
        try:
            time.sleep(retry_seconds)
        except Exception:
            return


def send(url: str, body: dict, path: str, timeout: float,
         retry_seconds: float) -> None:
    """Every host's `_send_event`: POST `body` to `url` (`<origin><path>`), on
    a failure once more `retry_seconds` later, and if that fails too, to the
    live server this install's server.json names (`reresolve`) — retried for
    at most `FALLBACK_SECONDS` while that server finishes booting.

    Fire-and-forget: swallows everything. The retry is for the server bouncing
    or a full listen backlog under a spawn storm; the re-resolve is for a
    server that is not coming back on that port at all. Bounded by
    `send_budget`, never a loop."""
    try:
        data = json.dumps(body).encode("utf-8")
    except Exception:
        return
    for attempt in (0, 1):
        if _post_once(url, data, timeout):
            return
        if attempt:
            break
        try:
            time.sleep(retry_seconds)
        except Exception:
            return
    if not url.endswith(path):
        return
    _post_to_live(url[:-len(path)], path, data, timeout, retry_seconds)
