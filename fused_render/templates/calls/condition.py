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

Stdlib only, and no *required* import of `fused_render` — a user may copy this
template folder to ~/.fused-render/templates/calls/, where it runs as a
subprocess. (The build-time baked branch ref is read through one guarded
import that degrades to "no ref" when the package is not importable.)
"""
import json
import os
import re

# Per-file tail budget. A record is ~400-900 bytes, so this covers the last
# ~100+ calls in each file — far more than "was this page active recently".
TAIL_BYTES = 96 * 1024
# Newest files first; a page with nothing in this many files is treated as
# having no history rather than scanning a whole retention window. "Newest" is
# by mtime, not by name — see _newest_first.
MAX_FILES = 3

SUFFIX = ".calls.jsonl"


# Mirrors fused_render._branch. Duplicated rather than imported so this file
# works as a standalone copy in the user template dir — and pinned to the real
# resolver by a test that compares the two dirs across a table of refs, because
# a duplicate that drifts sends the gate to a directory nothing writes to.
_REF_MAX_LEN = 12
_DEFAULT_REFS = ("main", "master", "head")
_BRANCHES_SUBDIR = "branches"


def _sanitize_ref(ref: str) -> str:
    """Lowercase, collapse non-[a-z0-9] runs to one '-', trim, truncate.

    The raw env value is NOT the directory name, and using it as one missed
    every rule here:

    * a ref naming a **default** branch is the baseline, not a nested dir, so
      ``FUSED_RENDER_BRANCH=main`` writes to ``~/.fused-render/calls`` while the
      gate looked under ``branches/main/``;
    * case and separators are normalised (``Feature_X`` -> ``feature-x``);
    * refs are truncated to 12 chars, and a real branch name is usually longer —
      ``claude/fused-api-…`` is ``claude-fused``, and joining the raw value even
      nests an extra level on the ``/``.

    Each one puts the probe in a directory that never receives records, so the
    gate fails closed and the Calls mode silently never appears.
    """
    if not ref:
        return ""
    lowered = ref.lower()
    if lowered in _DEFAULT_REFS:
        return ""
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    return collapsed.strip("-")[:_REF_MAX_LEN].rstrip("-")


def _branch_ref() -> str:
    """The active ref, by the package's own priority: the env var if present (an
    empty value is a deliberate baseline opt-out and still wins), else the ref
    baked in at build time, else baseline.

    The baked ref is read rather than skipped because the invariant that matters
    is "the gate probes the dir the writer writes to", and the writer resolves it
    this way — not because any particular run is known to reach it (the packaged
    supervisor sets the env var to "" explicitly, which wins). Matching the
    writer's priority is cheap; guessing which arm it takes is how this drifted.
    The module is gitignored and absent from a source checkout, and
    `fused_render/__init__.py` holds nothing but a version string, so the guarded
    import costs an ImportError in dev and ~0.1 ms in a build.
    """
    if "FUSED_RENDER_BRANCH" in os.environ:
        return _sanitize_ref(os.environ["FUSED_RENDER_BRANCH"])
    try:
        from fused_render import _baked_branch
    except ImportError:
        return ""
    return _sanitize_ref(getattr(_baked_branch, "_BAKED_REF", ""))


def _store_dir() -> str:
    """~/.fused-render/calls, resolved exactly as `calls.store_dir()` resolves it.

    Same overrides, same branch nesting: a branch run gates against its own
    store, and a baseline run against the baseline one.
    """
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    ref = _branch_ref()
    if ref:
        base = os.path.join(base, _BRANCHES_SUBDIR, ref)
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


def _newest_first(store: str, names: list[str]) -> list[str]:
    """Store files ordered by last append, newest first.

    NOT reverse name order. A store name orders records only by its DATE
    segment: the pid segment is arbitrary and compared lexically, so
    ``…-8000-000`` sorts after ``…-12345-000``. With two live servers, or a few
    within-day rolls, reverse name order can hand back MAX_FILES stale files and
    never reach the one being written — and the gate then reports "no history"
    for a page with plenty, so the Calls mode silently never appears. Same
    mistake the reader made before it merged same-day files on append time.

    mtime is the file's last append, which for an append-only file IS its newest
    record — the same fact the reader's file-skip relies on.
    """
    stamped = []
    for name in names:
        try:
            stamped.append((os.path.getmtime(os.path.join(store, name)), name))
        except OSError:
            continue  # vanished between listdir and stat
    # Stable, so equal mtimes keep the incoming name order (deterministic).
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _, name in stamped]


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
    for name in _newest_first(store, names)[:MAX_FILES]:
        try:
            text = _tail(os.path.join(store, name), TAIL_BYTES)
        except OSError:
            continue
        if needle in text or path in text:
            return True
    return False
