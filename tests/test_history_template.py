"""Tests for the history view template bindings (SPEC §24, D96).

The template itself is browser-side (template.html + icon.svg, no .py — HV-1),
so what the server can guarantee is covered here: the registry bindings and
their resolution through `_templates_for`, plus the shipped files' presence.
Behavioral checks (per-key validation, navigation) are exercised in the app.
"""

import os

from fused_render import server


def modes(path, is_dir=False):
    entries, error = server._templates_for(path, is_dir)
    return [e["mode"] for e in entries], error


def test_sidecar_default_mode_is_history():
    # `.html.json` (2 segments) beats the wildcard `.*.json` (also 2, but a
    # literal beats `*` at equal length — CT-3), which beats bare `.json` (1).
    entries, error = server._templates_for("/x/sine.html.json", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["history", "tree", "code"]
    assert entries[0]["path"].endswith("history/template.html")
    assert entries[0]["icon"] is not None


def test_sidecar_wildcard_matches_any_extension():
    # `.*.json` (HV-2) is generic — any `<name>.<ext>.json` is a sidecar, not
    # just `.html.json`. No `annotate`: annotating the sidecar log itself
    # doesn't make sense (comments belong on the target file, HV-8).
    entries, error = server._templates_for("/x/table.parquet.json", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["history", "tree", "code"]


# .html and .parquet gaining "history" as their last mode is covered by
# test_templates.py::test_builtin_html_default_is_render_sentinel and
# test_builtin_parquet_default_is_duckdb, which already assert the full
# resolved mode list for those keys.


def test_plain_json_unaffected():
    # A bare, non-compound .json (no sidecar target) keeps its tree-first
    # binding — the wildcard `.*.json` needs a stem with its own extension
    # (HV-3), so this doesn't match it.
    assert modes("/x/data.json", False) == (["tree", "code", "duckdb", "annotate"], None)


def test_template_ships_html_and_icon_only():
    d = os.path.join(server.TEMPLATES_DIR, "history")
    files = sorted(os.listdir(d))
    assert files == ["icon.svg", "template.html"]  # no .py — HV-1


def test_template_holds_inline_schema_for_all_owned_keys():
    with open(
        os.path.join(server.TEMPLATES_DIR, "history", "template.html"),
        encoding="utf-8",
    ) as f:
        text = f.read()
    for key in ("claudeSessions", "bookmarkHistory", "lastSession", "comments"):
        assert key in text, key


# A raw-JSON check of registry[".html.json"]/[".html"] would only restate
# what test_sidecar_default_mode_is_history (above) and
# test_templates.py::test_builtin_html_default_is_render_sentinel already
# prove through the real resolution path (_templates_for reads
# BUILTIN_REGISTRY directly, no intermediate transform) — so it isn't kept
# as a separate test.
