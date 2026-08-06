"""runPython target: update the triage record for a session.

`patch` is a JSON object; keys present are written, empty-string values
clear the key. A record whose fields are all cleared (and status back to
"inbox") is removed, so the session returns to the Inbox pile.
"""
import json
import os

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
