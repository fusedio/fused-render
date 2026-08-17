"""runPython target: update the triage record for a session.

`patch` is a JSON object; keys present are written, empty-string values
clear the key. A record whose fields are all cleared (and status back to
"inbox") is removed, so the session returns to the Inbox pile.

A status write is also STAMPED with `at` — see main(). The Tasks board treats an
`in_progress` pin as falsifiable and needs to know when the status was chosen;
the shell's own writer (`/api/claude-sessions/triage`) stamps the same field.
"""
import json
import os
import time

try:
    import fcntl  # POSIX only — Windows falls back to no inter-process lock
except ImportError:
    fcntl = None

# Mutable state lives in the user-data dir, never next to the scripts — the
# shipped copy is mounted read-only (see ensure_builtin_mounts). Mirrors
# shell/storage.home_dir()'s FUSED_RENDER_HOME override.
_STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "claude-sessions")

TRIAGE_FILE = os.path.join(_STATE_DIR, "triage.json")
LOCK_FILE = TRIAGE_FILE + ".lock"

# What a patch may write. `at` is deliberately absent: it is stamped below from
# this process's clock, and a pin whose lifetime the browser could set would be
# a pin the browser could make immortal.
FIELDS = {"status", "note", "tags", "read"}


def main(session: str = "", patch: str = "{}") -> dict:
    session = (session or "").strip()
    if not session:
        return {"ok": False, "error": "missing session id"}
    try:
        changes = json.loads(patch)
        assert isinstance(changes, dict)
    except (ValueError, AssertionError):
        return {"ok": False, "error": "patch must be a JSON object"}

    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)

        try:
            with open(TRIAGE_FILE, "r", encoding="utf-8") as f:
                triage = json.load(f)
                if not isinstance(triage, dict):
                    triage = {}
        except (OSError, ValueError):
            triage = {}

        rec = triage.get(session, {})
        for key, value in changes.items():
            if key not in FIELDS:
                continue
            value = str(value).strip()
            if value:
                rec[key] = value
            else:
                rec.pop(key, None)

        # Stamp WHEN THE STATUS WAS CHOSEN, and only then — restamping on a note
        # edit would let a thought typed today revive a pin from last week's run.
        #
        # `in_progress` is the one triage word that is a claim about the present,
        # so the Tasks board reaps one whose run has demonstrably ended
        # (tasks.py `_pin_holds`); a stamp later than the session's last activity
        # is what marks it as a decision no run has contradicted. Every
        # `in_progress` that reaches this file is now a person pressing the
        # button — inbox.html's autoFlow stopped writing the automatic one — and
        # the one automatic write left (`done` on a finish it watched) is a
        # timeless filing decision nothing reaps, so stamping every status rather
        # than sniffing which one is simpler and costs nothing. Epoch seconds as
        # a string: the same value and shape the shell's writer records, and the
        # same coercion every field above gets.
        #
        # Cleared with the status: an orphan `at` would keep an otherwise empty
        # record alive, and it is emptiness that returns a session to the Inbox.
        if "status" in changes:
            if rec.get("status"):
                rec["at"] = str(time.time())
            else:
                rec.pop("at", None)

        if rec:
            triage[session] = rec
        else:
            triage.pop(session, None)

        with open(TRIAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(triage, f, indent=2, ensure_ascii=False)

    return {"ok": True, "session": session, "record": triage.get(session)}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
