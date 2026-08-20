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

2. **Is there a TAGGED entry page?** An app's page is the first non-hidden
   top-level `.html` (name order) carrying `<meta name="fused-app">` — the
   marker is THE only signal and a filename declares nothing, `index.html`
   included (D301). This gate must not invent a second answer to "which page is
   the app's", so the marker check is the SHARED one
   (`../shared/app_entry.has_fused_meta`), never a regex of its own; only the
   listing and the cap below are this gate's.

3. **Is there a top-level `def main` in a top-level `.py`?** A page over no
   callable entrypoint has nothing to curate into a tool.

Both halves read the folder's names, so they share ONE single-level
`os.scandir` — and that listing is the one place this gate does something its
peers do not. It is admitted deliberately: there is no constant name to probe
for on either half. The page is whatever the author tagged (D301) and the
entrypoint may be called anything (`mail.py`, `server.py`), which is exactly
why the curation panel exists at all. What keeps it honest:

* it is ONE level and never a walk — a page or a `main` a directory down is not
  this app's, and the runner looks an entrypoint up in the executed module's
  namespace, so a nested or class-scoped `def main` is not one either;
* it is BOUNDED: at most `_MAX_CANDIDATES` files are read per half, at most
  `_READ_LIMIT` bytes each (4 KiB for the marker), and each half stops at its
  first hit. A pathological folder whose only tagged page or only `main` sits
  past the cap answers False — the gate is the UX and `inspect_app.py`, which
  reads the folder properly once on demand (uncapped, through
  `app_entry.entry_html` itself), is the guarantee (MD-11);
* the folder was just listed by the explorer to display it, so this is the same
  listing again from cache, not a new class of I/O;
* the CHEAPER half runs first: no `.html` at all means no reads whatsoever, and
  a folder with no `.py` never has its pages opened.

This replaced an `index.html` `isfile` probe, which was cheaper and wrong: it
gave a folder whose tagged page is `mail.html` no MCP pill at all, and gave one
with an untagged `index.html` beside a tagged `mail.html` a panel whose pin
hints came from the wrong file. A name rule kept for its cost is the guess D301
deleted, sneaking back in.

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

# How many files may be read PER HALF before the gate gives up looking. An app
# has a handful of each; a folder with dozens is something else, and a gate that
# opened all of them would stall the folder's first paint.
_MAX_CANDIDATES = 24

# Bytes read looking for the `<meta name="fused-app">` marker. The same 4 KiB
# budget `app_entry` itself reads, so the two agree about a page whose marker
# sits absurdly far down (neither sees it).
_MARKER_READ_LIMIT = 4096

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


def _top_level_names(path: str):
    """`(html, py)` — the non-hidden top-level file names of each kind, sorted.

    ONE `os.scandir` for both halves. Sorted by name because both caps below
    take a PREFIX of these lists: an unsorted `scandir` would make a capped
    folder's verdict depend on directory order, i.e. flap. Name order is also
    what `app_entry` resolves the entry page by, so the page this gate finds is
    the page every other consumer finds.
    """
    import os

    html, py = [], []
    try:
        with os.scandir(path) as entries:
            for e in entries:
                if e.name.startswith(".") or not e.is_file():
                    continue
                lower = e.name.lower()
                if lower.endswith(".html"):
                    html.append(e.name)
                elif lower.endswith(".py"):
                    py.append(e.name)
    except OSError:
        return [], []
    return sorted(html), sorted(py)


def _has_entry_page(path: str, html_names) -> bool:
    """Whether one of `html_names` carries the app marker (D301).

    The marker check is `app_entry`'s own — the rule has one home and this gate
    is not a second one. Only the listing and the cap are local: `entry_html`
    would list the folder again (this gate already has the names) and would read
    every page in it, which a gate answering on every directory the user opens
    cannot afford.
    """
    import os
    import sys

    shared = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    try:
        from app_entry import has_fused_meta
    except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
        return False

    for name in html_names[:_MAX_CANDIDATES]:
        if has_fused_meta(os.path.join(path, name)):
            return True
    return False


def _has_entrypoint(path: str, names) -> bool:
    """Whether one of `names` defines a top-level `main`. Bounded, first hit wins."""
    import os

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

        # (2) Folder-only. `isdir` rather than `not isfile` so a path that does
        # not exist reads as "refuse".
        if not os.path.isdir(path):
            return False

        # ONE listing serves both halves, and the halves run cheapest-first: the
        # name lists cost nothing to check, so a folder with no `.html` or no
        # `.py` is refused before a single file is opened.
        html_names, py_names = _top_level_names(path)
        if not html_names or not py_names:
            return False
        if not _has_entrypoint(path, py_names):
            return False
        return _has_entry_page(path, html_names)
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
