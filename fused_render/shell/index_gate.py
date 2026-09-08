"""The one answer to "may a file-index scan start right now?".

Two things can say no, and every trigger of a scan — the startup scan, the
on-demand routes, the freshness check a folder open queues, the rescan a
mutating page queues, and `runner.start` itself as the backstop — asks this
helper instead of `prefs.indexing_enabled()` alone, so the two refusals
cannot drift apart between call sites:

* ``"disabled"`` — the ``indexing_enabled`` preference is off (shell/prefs.py).
* ``"fda"`` — the packaged macOS app does not have Full Disk Access
  (shell/fda.py). The default scan root is the user's home, and a recursive
  walk of it reads under Desktop, Documents, Downloads and the rest of the
  TCC-protected folders. On a fresh install that walk used to start at boot,
  before the onboarding wizard had even painted, and each protected folder
  fired its own prompt — or, since the app was not frontmost yet, had the
  prompt suppressed and a DENY cached. Full Disk Access silences all of them
  at once, and the wizard already asks for it; so indexing simply waits for
  the grant. A grant applies only to the next launch (macOS caches the
  verdict per process, see fda.py), and the next launch runs the startup
  scan, which is how "indexing starts once FDA is given" comes for free.

Precedence: ``disabled`` first — a user who turned indexing off has said
something stronger than "not yet".

Only a CONCLUSIVE "not granted" blocks. `fda.granted()` answers None when no
probe target exists on the machine; blocking on "cannot tell" would strand
indexing forever with nothing the user could do about it, and an install with
neither `~/Library/Safari` nor `~/Library/Mail` is rare enough that the
per-folder prompts are the lesser wrong. Non-mac and dev-server processes are
never blocked: there `fda.offered()` is False because TCC is not in play (or
the identity is the terminal's).
"""
from fused_render.shell import fda, prefs

#: The `reason` / `why` value every surface spells the FDA refusal as.
FDA_REASON = "fda"

#: Text for the 409 / ValueError shape, beside "indexing is disabled in
#: Preferences".
FDA_MESSAGE = "indexing needs Full Disk Access on macOS"


def indexing_blocked() -> str:
    """"" when a scan may start, else "disabled" or "fda"."""
    # Module attribute lookups on purpose (not `from ... import`), so a test
    # that patches `prefs.indexing_enabled` or `fda.granted` is seen here.
    if not prefs.indexing_enabled():
        return "disabled"
    if fda.offered() and fda.granted() is False:
        return FDA_REASON
    return ""


def indexing_allowed() -> bool:
    return indexing_blocked() == ""
