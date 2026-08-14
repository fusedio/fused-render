"""runPython target: fire-and-forget — launch a new detached Claude Code
session that analyzes the given session's transcript. The new session shows
up in the inbox like any other, so its progress/result is tracked there."""
import os
import shutil
import subprocess
import sys

_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))
sys.path.insert(0, os.path.join(_HERE, "sessions"))

from sessions import _find_session_path  # noqa: E402

PROMPT = (
    "Analyze the Claude Code session transcript at {path} (session {sid}). "
    "Review how the session went: was it fast or slow, where was time or "
    "tokens wasted (wrong turns, retries, over-exploration), and what could "
    "be improved — in the prompts, the workflow, or the code. "
    "Keep it short and concise: a few bullets."
)


# the app's Python runs with a minimal PATH — find claude in the usual spots
def _claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for p in (os.path.join(home, ".local", "bin", "claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if os.access(p, os.X_OK):
            return p
    return ""


def main(session: str = "", cwd: str = "") -> dict:
    session = (session or "").strip()
    if not session:
        return {"ok": False, "error": "missing session id"}
    path, _ = _find_session_path(session)
    if not path:
        return {"ok": False, "error": "transcript not found"}
    if not (cwd and os.path.isdir(cwd)):
        # Not _HERE: the shipped copy lives on a read-only mount that can
        # detach under a long-lived child process.
        cwd = os.path.expanduser("~")

    claude = _claude_bin()
    if not claude:
        return {"ok": False, "error": "claude CLI not found on PATH"}
    try:
        subprocess.Popen(
            [claude, "-p", PROMPT.format(path=path, sid=session)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found on PATH"}
    return {"ok": True}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
