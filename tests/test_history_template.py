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
    # `.html.json` (2 segments) beats `.json` (1) by specificity — CT-3.
    entries, error = server._templates_for("/x/sine.html.json", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["history", "tree", "code", "annotate"]
    assert entries[0]["path"].endswith("history/template.html")
    assert entries[0]["icon"] is not None


# .html gaining "history" as its last mode is covered by
# test_templates.py::test_builtin_html_default_is_render_sentinel, which
# already asserts the full resolved mode list for .html.


def test_plain_json_unaffected():
    # A non-sidecar .json keeps its tree-first binding.
    assert modes("/x/data.json", False) == (["tree", "code", "annotate"], None)


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
