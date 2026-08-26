"""`~/.fused-render/models.json` — a user-extensible overlay on
`catalog.SUGGESTIONS` (SPEC AI-25, D528).

**Follows `server/templates.py`'s registry idiom exactly** (SPEC CT-5/CT-6,
D73), because that module already answered every question this one would
otherwise have to re-derive: read the file fresh on every call rather than
caching it (a tiny local JSON file, so an edit applies on the next request
with no restart and no cache-invalidation story to get wrong — the same
reasoning `_load_registry`'s own docstring states), a missing file is a
CLEAN no-op rather than an error (`FileNotFoundError` -> `None`, not raised),
and a malformed one degrades SILENTLY to "no overlay" rather than taking the
AI Models page down — a curated catalog with a typo'd `models.json` sitting
beside it must still render the built-in list, the same way a broken
`templates/registry.json` still lets a file open with its built-in binding.

**The merge rule** (SPEC item 15's own words): an overlay entry whose `id`
matches a built-in row's `id` REPLACES it in place (same position in the
list — `catalog.py`'s lists are ordered smallest-first, and overriding a
row's `size_gb` should not also silently reorder the page); an entry whose
`id` names nothing built-in APPENDS. Both are keyed by RUNNER CODE, the same
grain `catalog.SUGGESTIONS` itself uses, so `{"llamacpp-text": [...]}` in
`models.json` only ever touches that one curated list.

**Not machine-scoped, unlike `footprints.py`.** A user's hand-curated model
row is a statement about what THEY want offered, not a fact about the
machine that happened to write the file — carrying it onto a new machine (or
reading it from a synced dotfiles setup) is the whole point of it living in
the home directory at all.

Wired into exactly one call site, `catalog.for_runner` — every other reader
of `SUGGESTIONS` (`for_capability`, `all_suggested_ids`, `runners_offering`,
...) already goes through it, so the overlay is visible everywhere the
built-in curation is without a second wiring point to keep in sync.
"""
from __future__ import annotations

import os

from fused_render.shell import storage


def _path() -> str:
    return os.path.join(storage.home_dir(), "models.json")


def _load() -> dict:
    """The raw overlay file, or `{}` for anything that is not a usable
    `{runner_code: [row, ...]}` shape — missing, malformed JSON, or a body
    that parsed but is not a JSON object. Never raises: this is read on
    every `catalog.for_runner` call, on the same request path that serves
    the AI Models page, and a broken `models.json` must not take that page
    down any more than a broken `templates/registry.json` takes the
    explorer down."""
    data = storage.read_json(_path())
    return data if isinstance(data, dict) else {}


def apply(runner_code: str, builtin: list[dict]) -> list[dict]:
    """`builtin` (a `catalog.SUGGESTIONS[...]`-shaped list, already resolved
    for `runner_code`'s hardware-variant alias) with the overlay merged in:
    an overlay row whose `id` matches a built-in row's REPLACES it at the
    same position; a new `id` is appended, in the overlay file's own order.

    Returns a NEW list — `builtin` itself is never mutated, matching
    `catalog.for_runner`'s existing "callers get a copy, not the curation"
    contract.

    A row missing an `id`, or not a dict at all, is skipped rather than
    raising: `_load`'s degrade-to-`{}` handles a broken FILE, this handles a
    broken ROW inside an otherwise-valid file — one bad entry must not
    discard every other row the user actually got right.
    """
    overlay = _load().get(runner_code)
    if not isinstance(overlay, list) or not overlay:
        return list(builtin)

    result = [dict(row) for row in builtin]
    index_by_id = {row.get("id"): i for i, row in enumerate(result) if isinstance(row, dict)}
    for entry in overlay:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if model_id in index_by_id:
            result[index_by_id[model_id]] = dict(entry)
        else:
            index_by_id[model_id] = len(result)
            result.append(dict(entry))
    return result
