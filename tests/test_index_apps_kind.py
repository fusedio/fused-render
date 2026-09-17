"""The built-in "apps" IndexKind: app-ness read from file CONTENT (a
`<meta name="fused-app">` marker on a direct-child .html), the one thing
plain file metadata (name/size/mtime) can never answer. See
fused_render/index/specs/index-plugins.md.
"""
import os

import pytest

from fused_render.index import apps_kind
from fused_render.index.kinds import get, registered


def _write(path, text='<html><head><meta name="fused-app"></head><body></body></html>'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _stat(path):
    return os.stat(path)


def test_extract_ignores_non_html_files(tmp_path):
    p = tmp_path / "app" / "data.json"
    os.makedirs(p.parent, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    assert apps_kind.extract(str(p), _stat(str(p))) is None


def test_extract_ignores_html_with_no_fused_app_marker(tmp_path):
    p = _write(str(tmp_path / "plain" / "page.html"), "<html><body>hi</body></html>")
    assert apps_kind.extract(p, _stat(p)) is None


def test_extract_ignores_a_non_entry_html_sibling(tmp_path):
    """A folder with a tagged entry can have other .html files beside it —
    only the entry itself (app_listing.app_entry's pick) is a row."""
    folder = str(tmp_path / "app")
    entry = _write(os.path.join(folder, "index.html"))
    sibling = _write(os.path.join(folder, "notes.html"), "<html><body>notes</body></html>")
    assert apps_kind.extract(sibling, _stat(sibling)) is None
    row = apps_kind.extract(entry, _stat(entry))
    assert row is not None
    assert row["entry_html"] == os.path.abspath(entry)


def test_extract_returns_a_row_for_the_canonical_entry(tmp_path):
    folder = str(tmp_path / "myapp")
    entry = _write(
        os.path.join(folder, "index.html"),
        '<html><head><meta name="fused-app">'
        '<meta name="fused-app-id" content="myapp-deadbeef"><title>My App</title>'
        "</head><body></body></html>",
    )
    row = apps_kind.extract(entry, _stat(entry))
    assert row is not None
    assert row["name"] == "myapp"
    assert row["path"] == os.path.realpath(folder)
    assert row["entry"] == os.path.abspath(entry)
    assert row["entry_html"] == os.path.abspath(entry)
    assert row["id"] == "myapp-deadbeef"
    assert row["title"] == "My App"
    assert "tag" not in row


def test_extract_row_has_no_id_when_the_page_declares_none(tmp_path):
    folder = str(tmp_path / "noid")
    entry = _write(os.path.join(folder, "index.html"))
    row = apps_kind.extract(entry, _stat(entry))
    assert row["id"] is None


def test_extract_never_raises_on_an_unreadable_folder(tmp_path):
    """A file whose folder vanished between the walker's visit and the
    extract call (a race) must degrade to None, not raise — the same
    posture as app_listing.app_entry's documented OSError, contained here
    rather than propagated into the walker."""
    ghost = str(tmp_path / "gone" / "index.html")
    st = os.stat_result((0,) * 10)
    assert apps_kind.extract(ghost, st) is None


def test_kind_columns_match_the_row_shape(tmp_path):
    folder = str(tmp_path / "shaped")
    entry = _write(os.path.join(folder, "index.html"))
    row = apps_kind.extract(entry, _stat(entry))
    assert set(row) == {c.name for c in apps_kind.KIND.columns}


def test_kind_registers_under_the_name_apps():
    apps_kind.register_builtin(replace=True)
    assert "apps" in registered()
    assert get("apps") is apps_kind.KIND
