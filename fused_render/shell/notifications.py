"""GET/POST /api/notifications/state — the Notifications panel's memory at
~/.fused-render/notifications.json.

    {"seen": ["<row key>", ...],              # rows the panel was open over (R4)
     "firstSeenAt": {"<row key>": <epoch ms>}, # when a row first existed (R5 sort)
     "repoDismissed": {"<repo root>": "<signature>"},
     "attentionDismissed": {"<key>": "<signature>"}}

All four used to be localStorage (notifications-seen-store.ts and the dock's
dismiss stores). D1386 moved them here: localStorage is per ORIGIN, and the
desktop app walks ports 1777..1787 then an ephemeral one — every relaunch on
another port is a new origin with empty storage, so every row came back
unseen and every dismissed update un-dismissed itself.

POST is a MERGE, not a replace, because several windows write this at once
and each knows only what it has rendered — a whole-doc last-write-wins would
let one window erase another's marks:

* ``seen`` — union.
* ``firstSeenAt`` — the EARLIER stamp per key wins (a stamp records "first
  existed", so a later one from a second window is just a late observer).
* ``repoDismissed`` / ``attentionDismissed`` — the posted value wins per id;
  a posted ``""`` or ``null`` deletes that id (un-dismiss).

Then the store is pruned — the same rule the client's `pruneByAgeAndSize`
applied to its localStorage copy: `firstSeenAt` entries older than 30 days
drop, then only the newest 500 stay; a `seen` key whose stamp was just pruned
goes with it. A `seen` key that was NEVER stamped is kept: the panel marks a
row seen on close and stamps it on its next render, and pruning it in between
would un-see it.

``message:``-prefixed keys are refused (400) wherever they appear: a client
message's id is minted per window and not stable across a reload, so its
seen state is per-window by design and lives in that window's memory only.
"""
import math
import os
import threading
import time

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
MAX_ENTRIES = 500
MAX_KEY = 4096
MAX_SIG = 4096
MESSAGE_PREFIX = "message:"
_MAPS = ("repoDismissed", "attentionDismissed")

# One read-modify-write at a time in this process: two windows posting at once
# would otherwise each merge into the record they read and the slower write
# would drop the faster one's keys — the exact loss the merge exists to stop.
_LOCK = threading.Lock()


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Duplicated deliberately (see bookmarks.py): shell/ must not import server.
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "notifications.json")


def _is_key(k) -> bool:
    return isinstance(k, str) and 0 < len(k) <= MAX_KEY and not k.startswith(MESSAGE_PREFIX)


def _is_stamp(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _empty() -> dict:
    return {"seen": [], "firstSeenAt": {}, "repoDismissed": {}, "attentionDismissed": {}}


def read_state() -> dict:
    """The stored record, shape-checked entry by entry (a bad entry is
    skipped, not fatal); empty shapes when absent or corrupt."""
    data = storage.read_json(_path())
    out = _empty()
    if not isinstance(data, dict):
        return out
    seen = data.get("seen")
    if isinstance(seen, list):
        out["seen"] = list(dict.fromkeys(k for k in seen if _is_key(k)))
    stamps = data.get("firstSeenAt")
    if isinstance(stamps, dict):
        out["firstSeenAt"] = {k: v for k, v in stamps.items() if _is_key(k) and _is_stamp(v)}
    for name in _MAPS:
        m = data.get(name)
        if isinstance(m, dict):
            out[name] = {k: v for k, v in m.items()
                         if _is_key(k) and isinstance(v, str) and v}
    return out


class _Invalid(ValueError):
    pass


def _validate(patch) -> dict:
    """The patch's known fields, validated; raises `_Invalid` with a sentence."""
    if not isinstance(patch, dict):
        raise _Invalid("body must be an object")
    out: dict = {}
    if "seen" in patch:
        seen = patch["seen"]
        if not isinstance(seen, list) or not all(_is_key(k) for k in seen):
            raise _Invalid("'seen' must be a list of non-message row keys")
        out["seen"] = seen
    if "firstSeenAt" in patch:
        stamps = patch["firstSeenAt"]
        if not isinstance(stamps, dict) or not all(
                _is_key(k) and _is_stamp(v) for k, v in stamps.items()):
            raise _Invalid("'firstSeenAt' must map non-message row keys to epoch ms")
        out["firstSeenAt"] = stamps
    for name in _MAPS:
        if name in patch:
            m = patch[name]
            if not isinstance(m, dict) or not all(
                    _is_key(k) and (v is None or (isinstance(v, str) and len(v) <= MAX_SIG))
                    for k, v in m.items()):
                raise _Invalid(f"'{name}' must map non-message ids to a signature string or null")
            out[name] = m
    return out


def _prune(state: dict, now_ms: float) -> None:
    """The client's old `pruneByAgeAndSize`, made safe for never-stamped
    `seen` keys (module docstring). In place."""
    before = state["firstSeenAt"]
    entries = [(k, v) for k, v in before.items() if now_ms - v <= MAX_AGE_MS]
    if len(entries) > MAX_ENTRIES:
        entries.sort(key=lambda kv: kv[1], reverse=True)
        entries = entries[:MAX_ENTRIES]
    after = dict(entries)
    dropped = before.keys() - after.keys()
    state["firstSeenAt"] = after
    if dropped:
        state["seen"] = [k for k in state["seen"] if k not in dropped]


def merge(patch, now_ms: float | None = None) -> dict:
    """Merge a validated-on-entry `patch` into the store and return the
    record as written. Raises `_Invalid` (a ValueError) on a bad patch."""
    clean = _validate(patch)
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    with _LOCK:
        state = read_state()
        if "seen" in clean:
            state["seen"] = list(dict.fromkeys([*state["seen"], *clean["seen"]]))
        for k, v in clean.get("firstSeenAt", {}).items():
            cur = state["firstSeenAt"].get(k)
            if cur is None or v < cur:
                state["firstSeenAt"][k] = v
        for name in _MAPS:
            for k, v in clean.get(name, {}).items():
                if v:
                    state[name][k] = v
                else:
                    state[name].pop(k, None)
        _prune(state, now_ms)
        storage.write_json(_path(), state)
        return state


@router.get("/api/notifications/state")
def get_notifications_state():
    return read_state()


@router.post("/api/notifications/state")
def post_notifications_state(patch=Body(default=None),
                             x_fused: str | None = Header(default=None)):
    # Untyped body: a non-object is our 400, not FastAPI's 422.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return merge(patch)
    except _Invalid as e:
        return JSONResponse({"error": str(e)}, status_code=400)
