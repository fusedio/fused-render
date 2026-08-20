"""Gate for the `mcp` template (SPEC CT-12, §44 / MC-1).

`main(path)` decides whether a path is a fused-render **app folder**: a page
plus at least one Python entrypoint the page can call. That is the only shape
this mode has anything to say about — it curates those entrypoints into MCP
tools, writes the curation to `mcp.toml` in the folder, and registers the folder
with `claude mcp add-json` so a Claude session can call them.

`mcp` is a FOLDER-ONLY mode, for the same reason `git` is: the manifest it
authors sits at the folder's root and covers the whole folder, so the folder is
the unit the mode acts on. There is no per-file question left over — a file's own
entrypoint is one `[[tool]]` in its folder's manifest — so the registry keeps
`mcp` on the universal "/" DIRECTORY key alone and this gate refuses anything
that is not a directory. A hand-written `?_mode=mcp` on a file therefore renders
nothing the user asked for, and the runtime modules still tolerate a file target
rather than crash on one (MD-11: the gate is the UX, the module is the
guarantee).

Three questions, in this order, because the order is what makes the gate cheap:

1. **Is the path mount-backed?** Then False, always, and before any read. The
   panel's backend reads every `.py` in the folder and writes a manifest into
   it; over an rclone-NFS mount that is the stat-and-list pattern that wedges a
   flat million-key prefix, and the write half has nowhere sane to land. Same
   refusal as `git`/`graph`, through the same `../shared/appenv.is_mount_backed`
   — not a second copy. If that import fails we cannot tell, and "cannot tell"
   must read as "refuse" (CT-12).

2. **Is there an `index.html`?** One `isfile`, and it is deliberately FIRST
   among the two app halves. This mode rides the universal "/" key, so the gate
   answers for every directory the user opens, and almost none of them are apps.
   A folder with no page therefore costs exactly what a peer gate costs — a
   single stat — and never reaches the listing below. `test_mcp_condition.py`
   pins that ordering, because a refactor that reversed it would make every
   folder in someone's home directory pay for a listing.

3. **Is there a top-level `def main` in a top-level `.py`?** This is the half
   that needs the folder's contents, and it is the one place this gate does
   something its peers do not: ONE single-level `os.scandir`. That is a real
   cost and it is admitted deliberately, because there is no marker file to
   probe for instead — an app's entrypoint may be called anything (`mail.py`,
   `server.py`), which is exactly why the panel exists to curate them. What
   keeps it honest:

   * it happens only for folders that already passed (2), so it is not paid by
     ordinary folders;
   * it is ONE level and never a walk — a `main` a directory down is not the
     app's entrypoint anyway (the runner looks the name up in the executed
     module's namespace, so a nested or class-scoped `def main` is not an
     entrypoint either);
   * it is BOUNDED: at most `_MAX_CANDIDATES` files are read, at most
     `_READ_LIMIT` bytes each, and it stops at the first qualifying file. A
     pathological folder whose only `main` sits past the cap answers False —
     the gate is the UX and `inspect_app.py`, which reads the folder properly
     once on demand, is the guarantee (MD-11);
   * the folder was just listed by the explorer to display it, so this is the
     same listing again from cache, not a new class of I/O.

CRITICAL: never a walk, a glob, or a recursion — the rule
`zarr_aoi/condition.py` documents, and the property the suite makes fatal.

The `def main` test is an AST parse, not a text search: `ast.parse` is what
answers "TOP-LEVEL def" without also matching a `def main` inside a docstring or
a nested function, and `inspect_app.py` derives the same folder's signatures the
same way — one answer to "what is an entrypoint", not two that can disagree. A
cheap substring reject runs first so the parse is only paid for by files that
could plausibly pass, and an unparseable file is simply not a candidate (a
half-written app file must not hide the mode for the folder's other files).

Fails closed: an unreadable path, a listing error, a decode error, any
exception at all → False.

Self-contained apart from `../shared/appenv.py` (itself stdlib-only, env vars
only) — the module is exec'd standalone (not imported as part of a package), so
nothing here imports fused_render (SPEC PY-15).
"""

# The page that makes the Python an app's entrypoints rather than someone's
# library. Also the cheap probe that keeps the listing off ordinary folders.
_PAGE = "index.html"

# How many top-level `.py` files may be read before the gate gives up looking
# for an entrypoint. An app has a handful; a folder with dozens is something
# else, and a gate that parsed all of them would stall the folder's first paint.
_MAX_CANDIDATES = 24

# Bytes read per candidate. An entrypoint's `def main` is a top-level statement,
# so it is within the first pages of the file in every real app; a truncated
# read can only ever cost a candidate, never produce a wrong True, because a
# truncated tail does not parse into a `def main` that is not there.
_READ_LIMIT = 256 * 1024


def _has_top_level_main(source: str) -> bool:
    """Whether `source` defines a module-level `main` (sync or async).

    Top-level only, and by AST: the runner invokes an entrypoint by looking the
    name up in the executed module's namespace, where a method on a class and a
    function nested in another function do not appear. A text search would count
    both, plus a `def main` inside a docstring.
    """
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # A half-written app file is not a candidate — and must not hide the
        # mode for the folder's other, valid files.
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            return True
    return False


def _has_entrypoint(path: str) -> bool:
    """Whether one top-level `.py` in `path` defines a top-level `main`.

    ONE `os.scandir`, bounded reads, first hit wins. Sorted by name so the
    candidate set the cap admits is deterministic — an unsorted `scandir` would
    make a capped folder's verdict depend on directory order, i.e. flap.
    """
    import os

    try:
        with os.scandir(path) as entries:
            names = sorted(
                e.name for e in entries
                if e.name.endswith(".py") and not e.name.startswith(".") and e.is_file()
            )
    except OSError:
        return False

    for name in names[:_MAX_CANDIDATES]:
        try:
            with open(os.path.join(path, name), "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read(_READ_LIMIT)
        except OSError:
            continue
        # Cheap reject before the parse: a file with no `def main` anywhere in
        # its text cannot have one at top level.
        if "def main" not in source:
            continue
        if _has_top_level_main(source):
            return True
    return False


def main(path: str) -> bool:
    import os
    import sys

    try:
        # (1) A mount-backed path is refused before any read of the folder.
        #
        # Through `shared/appenv` (env vars only, stdlib only) rather than by
        # importing fused_render, so the mount rule has ONE home for every
        # template (SPEC PY-15).
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on EVERY stat, so
        # an unconditional insert would grow sys.path without bound.
        if shared not in sys.path:
            sys.path.insert(0, shared)
        try:
            from appenv import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        if not path:
            return False

        # (2) Folder-only, and the page is the cheap half. `isdir` rather than
        # `not isfile` so a path that does not exist reads as "refuse"; then one
        # `isfile` for the page, which is what keeps the listing below off the
        # ordinary folders this gate is asked about all day.
        if not os.path.isdir(path):
            return False
        if not os.path.isfile(os.path.join(path, _PAGE)):
            return False

        # (3) The half that costs a listing — bounded, one level, first hit wins.
        return _has_entrypoint(path)
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
