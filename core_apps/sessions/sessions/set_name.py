"""runPython target: save a user-chosen name for a session.
Names are stored in session_names.json in the user-data dir; an empty
name removes the entry so the row falls back to its first prompt."""
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

NAMES_FILE = os.path.join(_STATE_DIR, "session_names.json")
LOCK_FILE = NAMES_FILE + ".lock"


def main(session: str = "", name: str = "") -> dict:
    session = (session or "").strip()
    name = (name or "").strip()
    if not session:
        return {"ok": False, "error": "missing session id"}

    # Bulk rename runs several of these processes in parallel — serialize the
    # read-modify-write so concurrent saves don't clobber each other.
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)

        try:
            with open(NAMES_FILE, "r", encoding="utf-8") as f:
                names = json.load(f)
                if not isinstance(names, dict):
                    names = {}
        except (OSError, ValueError):
            names = {}

        if name:
            names[session] = name
        else:
            names.pop(session, None)

        with open(NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(names, f, indent=2, ensure_ascii=False)

    return {"ok": True, "session": session, "name": name}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
