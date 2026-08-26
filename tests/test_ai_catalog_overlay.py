"""Tests for the `~/.fused-render/models.json` catalog overlay (SPEC AI-25,
D528).

Mirrors `server/templates.py`'s registry idiom (SPEC CT-5/CT-6, D73): read
per call (a tiny local file, edits apply on the next request with no
restart), a missing file is a clean no-op, a malformed one degrades to "no
overlay" rather than taking the page down, and — the one rule that is this
module's whole point — an entry whose `id` matches a built-in row OVERRIDES
it in place, a new `id` APPENDS.
"""
import pytest

from fused_render.ai import catalog, catalog_overlay


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _write(monkeypatch, data):
    import json
    import os

    from fused_render.shell import storage

    path = catalog_overlay._path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_no_file_is_a_clean_no_op():
    builtin = [{"id": "a/b", "size_gb": 1.0}]
    assert catalog_overlay.apply("some-runner", builtin) == builtin


def test_a_matching_id_overrides_the_built_in_row(monkeypatch):
    _write(monkeypatch, {"llamacpp-text": [{"id": "a/b", "size_gb": 9.0}]})
    builtin = [{"id": "a/b", "size_gb": 1.0}, {"id": "c/d", "size_gb": 2.0}]
    result = catalog_overlay.apply("llamacpp-text", builtin)
    assert result[0] == {"id": "a/b", "size_gb": 9.0}
    assert result[1] == {"id": "c/d", "size_gb": 2.0}
    # Position of the overridden row is preserved, not moved to the end.
    assert [r["id"] for r in result] == ["a/b", "c/d"]


def test_a_new_id_appends(monkeypatch):
    _write(monkeypatch, {"llamacpp-text": [{"id": "new/model", "size_gb": 3.0}]})
    builtin = [{"id": "a/b", "size_gb": 1.0}]
    result = catalog_overlay.apply("llamacpp-text", builtin)
    assert [r["id"] for r in result] == ["a/b", "new/model"]


def test_a_runner_with_no_overlay_entries_is_untouched(monkeypatch):
    _write(monkeypatch, {"other-runner": [{"id": "x/y"}]})
    builtin = [{"id": "a/b", "size_gb": 1.0}]
    assert catalog_overlay.apply("llamacpp-text", builtin) == builtin


def test_malformed_json_degrades_to_no_overlay(monkeypatch):
    import os

    path = catalog_overlay._path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    builtin = [{"id": "a/b", "size_gb": 1.0}]
    assert catalog_overlay.apply("llamacpp-text", builtin) == builtin


def test_a_row_with_no_id_is_ignored(monkeypatch):
    _write(monkeypatch, {"llamacpp-text": [{"size_gb": 3.0}, {"id": "ok/row"}]})
    builtin = [{"id": "a/b"}]
    result = catalog_overlay.apply("llamacpp-text", builtin)
    assert [r["id"] for r in result] == ["a/b", "ok/row"]


def test_the_overlay_is_not_a_list_degrades_gracefully(monkeypatch):
    _write(monkeypatch, {"llamacpp-text": "not-a-list"})
    builtin = [{"id": "a/b"}]
    assert catalog_overlay.apply("llamacpp-text", builtin) == builtin


def test_the_whole_file_is_not_a_dict_degrades_gracefully(monkeypatch):
    import os

    path = catalog_overlay._path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")
    builtin = [{"id": "a/b"}]
    assert catalog_overlay.apply("llamacpp-text", builtin) == builtin


def test_the_builtin_list_passed_in_is_not_mutated(monkeypatch):
    _write(monkeypatch, {"llamacpp-text": [{"id": "a/b", "size_gb": 9.0}]})
    builtin = [{"id": "a/b", "size_gb": 1.0}]
    catalog_overlay.apply("llamacpp-text", builtin)
    assert builtin[0]["size_gb"] == 1.0


def test_for_runner_applies_the_overlay_live(monkeypatch):
    """`catalog.for_runner` is the one production call site — the overlay
    must be visible there without any restart or rebuild."""
    code = next(iter(catalog.SUGGESTIONS))
    existing = catalog.SUGGESTIONS[code][0]["id"]
    _write(monkeypatch, {code: [{"id": existing, "note": "overlaid"}]})
    result = catalog.for_runner(code)
    assert result[0]["id"] == existing
    assert result[0]["note"] == "overlaid"
