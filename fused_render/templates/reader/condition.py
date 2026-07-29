"""Condition gate for the `reader` template (SPEC CT-12, §28).

Unlike the `canvas` gate beside it, this is NOT a per-file content sniff: it is
a GLOBAL feature switch. `reader` is an accessibility affordance (a mode that
reads text files and PDFs aloud), off by default and turned on from the
Preferences page — so the gate ignores `target_path` entirely and answers the
one question that decides whether the mode ever appears: is the persisted
preference `reader_enabled` True?

Runs in the SERVER process when `/api/fs/conditions` resolves a file's gated
modes in the background (stat only marks the entry `conditional`, CT-12 deferred
evaluation). It must be cheap and must NEVER raise — a broken gate is meant to
*deny* the mode, and returning False is how we do that (`server._run_condition`
also catches, but we fail closed here explicitly, SPEC CT-12/§28).

`main(target_path)` is True only when `reader_enabled` reads True. The pref is
resolved the way the app itself resolves it (`fused_render.shell.prefs` — an
in-server-only concern, not a mount fact, so it stays a direct import); if that
import path is unavailable for any reason we fall back to a stdlib read of
prefs.json under the home dir `shared/appenv.home_dir()` reports. Any failure — missing file,
malformed JSON, a value that isn't literally True — → False, so the accessibility
mode stays off until the user opts in.
"""
import json
import os


def _reader_enabled_fallback() -> bool:
    # Stdlib-only read of the persisted pref, used only if importing the app's
    # own prefs helper fails. The home dir comes from `shared/appenv`, which reads
    # the ALREADY branch-resolved FUSED_RENDER_HOME_DIR the server exports (SPEC
    # PY-14) — so a branch-isolated dev server reads its own prefs.json, not the
    # baseline's, without this file carrying a second copy of the nesting rule.
    # If appenv itself is unreachable, the baseline home is the honest guess.
    try:
        import sys

        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        if shared not in sys.path:      # re-exec'd per stat; don't grow sys.path
            sys.path.insert(0, shared)
        from appenv import home_dir

        home = home_dir()
    except Exception:
        home = (os.environ.get("FUSED_RENDER_HOME")
                or os.path.expanduser("~/.fused-render"))
    with open(os.path.join(home, "prefs.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    return isinstance(data, dict) and data.get("reader_enabled") is True


def main(target_path) -> bool:
    # target_path is ignored on purpose (see module docstring): this is a global
    # opt-in, not a per-file test.
    try:
        from fused_render.shell import prefs

        return prefs.reader_enabled()
    except Exception:
        # The app's helper wasn't importable — try a bare read of the same file,
        # and if even that can't decide, fail closed (SPEC CT-12).
        try:
            return _reader_enabled_fallback()
        except Exception:
            return False
