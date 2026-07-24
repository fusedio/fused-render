"""Gate for the calls template (SPEC CT-12).

`main(path)` returns True only when the call log actually holds records for
`path`, so "Calls" is not a dead mode on every .html and .py in the filesystem.
A page nobody has run has nothing to show; the moment it has calls, the gate
flips and the mode joins the switcher in the background with no reload (the
verdict is resolved by /api/fs/conditions, not at stat time).

Efficiency is the design constraint — this runs on every .html/.py the user
opens, so it must never scan the whole store:

1. **Zero-I/O fast path for the log files themselves.** A `<name>.calls.jsonl`
   IS the store, so it always has something to show. Decided True with no
   filesystem calls at all.

2. **Tail-first, newest-file-first, early exit.** Records are appended, so a
   page's most recent activity is at the END of the NEWEST file. The gate reads
   a bounded tail (TAIL_BYTES) of each file, newest first, and returns True on
   the first line mentioning the path — which for any page opened recently is
   the very first probe. Only a page with no recent activity pays for more, and
   even then the scan is capped at MAX_FILES files × TAIL_BYTES each.

The substring test is deliberately cheap and slightly loose: it asks whether
this path appears anywhere in the tail, not whether it appears in the `page`
field specifically. A false positive costs one extra mode that renders an empty
state (and would be a genuine near-miss anyway — the path was in some record);
parsing every line as JSON to be exact would cost far more on the hot path than
the mistake it prevents. A missing/unreadable store fails closed (not offered),
matching CT-12's posture.

Stdlib only, and no import of `fused_render` — a user may copy this template
folder to ~/.fused-render/templates/calls/, where it runs as a subprocess.
"""
import json
import os

# Per-file tail budget. A record is ~400-900 bytes, so this covers the last
# ~100+ calls in each file — far more than "was this page active recently".
TAIL_BYTES = 96 * 1024
# Newest files first; a page with nothing in this many files is treated as
# having no history rather than scanning a whole retention window.
MAX_FILES = 3

SUFFIX = ".calls.jsonl"


def _store_dir() -> str:
    """~/.fused-render/calls, honouring the same overrides shell/storage does.

    Duplicated rather than imported so this file works as a standalone copy in
    the user template dir. FUSED_RENDER_BRANCH nesting is mirrored too, so a
    branch checkout gates against its own store and not the baseline one.
    """
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    ref = os.environ.get("FUSED_RENDER_BRANCH")
    if ref:
        base = os.path.join(base, "branches", ref)
    return os.path.join(base, "calls")


def _tail(path: str, limit: int) -> str:
    """The last `limit` bytes of a file as text (errors replaced)."""
    with open(path, "rb") as fh:
        try:
            size = os.fstat(fh.fileno()).st_size
        except OSError:
            size = 0
        if size > limit:
            fh.seek(-limit, os.SEEK_END)
        return fh.read(limit).decode("utf-8", "replace")


def main(path: str) -> bool:
    if not path:
        return False
    if os.path.basename(path).endswith(SUFFIX):
        return True  # the store itself — nothing to check

    store = _store_dir()
    try:
        names = sorted(n for n in os.listdir(store) if n.endswith(SUFFIX))
    except OSError:
        return False  # no store yet / unreadable -> fail closed

    needle = json.dumps(path)[1:-1]  # JSON-escaped, without the quotes
    for name in list(reversed(names))[:MAX_FILES]:
        try:
            text = _tail(os.path.join(store, name), TAIL_BYTES)
        except OSError:
            continue
        if needle in text or path in text:
            return True
    return False
