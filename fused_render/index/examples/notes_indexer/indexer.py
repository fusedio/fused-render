"""Reference `IndexKind`: a "notes" index over markdown files.

This is the ONE app-authored example indexer SPEC-index-plugins.md asks
for — a small, fully worked reference a third party can read start to
finish, not a kind end users are meant to discover or enable (decision #9:
documented and tested, never pitched). Its `pyproject.toml` sitting beside
this file is the actual manifest format a third party's own app folder
would carry:

    [tool.fused-render.index]
    module = "indexer.py"
    kind = "notes"

`fused_render.index.manifest.load_manifest` parses that table into an
`IndexManifest(folder, module, kind)`; this folder is a fixture proving
that parse round-trips (see `tests/test_index_examples_notes.py`), not a
piece of the server's own startup path — nothing here is imported at
server boot. A real third party would follow the same two-file shape:
one `pyproject.toml` announcing itself, one module exposing a
`register_example`-shaped call the (confirmed-folder-only) caller invokes
after the user has confirmed the proposal (`manifest.confirm_index`).

Row shape is deliberately trivial — a title and a word count — chosen to
be checkable by eye in a small test fixture, not because a real notes
index would want exactly these two fields.
"""
from __future__ import annotations

import os

from fused_render.index.ignore import norm
from fused_render.index.kinds import Column, IndexKind, register

NAME = "notes"

COLUMNS = (
    Column("title", "string"),
    Column("path", "string"),
    Column("word_count", "int64"),
)


def _first_heading(text: str) -> str | None:
    """The text after the first Markdown `# ` heading, or None when the
    file has none — the simplest possible "read the content, not just the
    name" rule, echoing why `apps_kind.py` reads a page's `<meta>` tag
    rather than trusting a filename."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
    return None


def extract(path: str, st) -> dict | None:
    """One row per `.md` file: `title` (its first `# ` heading, falling
    back to the filename without its extension) and a whitespace word
    count. None for anything that isn't a markdown file, or one that can't
    be read as text — never raises, the same degrade-safe contract every
    `IndexKind.extract` must honor."""
    if not path.lower().endswith(".md"):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return None
    title = _first_heading(text) or os.path.splitext(os.path.basename(path))[0]
    return {
        "title": title,
        # `path` is this kind's identity_column, so it must be canonical
        # (forward-slash) form — see apps_kind.py's extract() and
        # specs/index-plugins.md.
        "path": norm(os.path.abspath(path)),
        "word_count": len(text.split()),
    }


KIND = IndexKind(
    name=NAME, columns=COLUMNS, extract=extract, text_column="title",
    # `path` is the row's identity (one row per markdown file). There is no
    # natural recency column here — a note carries no timestamp of its own
    # — so compaction falls back to ordering by `path` itself when resolving
    # a duplicate (see `IndexKind.recency_column`'s docstring in kinds.py).
    identity_column="path",
)


def register_example(*, replace: bool = False) -> None:
    """Add the "notes" kind to the registry. A real third party's own
    equivalent function is what a caller invokes AFTER
    `fused_render.index.manifest.confirmed_folders()` lists its folder —
    never automatically at import, and never before the user has
    confirmed the proposal (SPEC-index-plugins.md decision #8)."""
    register(KIND, replace=replace)
