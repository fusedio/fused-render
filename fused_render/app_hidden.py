"""Apps the user uninstalled that cannot be deleted: a subfolder of a
showcase clone left from before the Community replaced it (2026-10-29;
deleting it dirties that repo). ``~/.fused-render/hidden_apps.json`` holds
their paths; every hub listing drops them (routers/apps.py). Nothing
un-hides one yet — the showcase checkout is the owner's to tidy."""
from __future__ import annotations

import os

from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import storage


def _path() -> str:
    return os.path.join(storage.home_dir(), "hidden_apps.json")


def _canon(path: str) -> str:
    return os.path.normcase(canonical_fs_path(os.path.realpath(path)).rstrip("/"))


def hidden_paths() -> set[str]:
    data = storage.read_json(_path())
    raw = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return set()
    return {_canon(p) for p in raw if isinstance(p, str) and os.path.isabs(p)}


def hide(path: str) -> None:
    paths = hidden_paths()
    paths.add(_canon(path))
    storage.write_json(_path(), {"paths": sorted(paths)})


def drop_hidden(rows: list[dict]) -> list[dict]:
    """`rows` without the hidden ones. One store read; no I/O per row."""
    hidden = hidden_paths()
    if not hidden:
        return rows
    return [r for r in rows if not (isinstance(r.get("path"), str) and _canon(r["path"]) in hidden)]
