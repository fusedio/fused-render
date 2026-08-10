"""Tests for the history view template bindings (SPEC §24, D96).

The template itself is browser-side (template.html + icon.svg, no .py — HV-1),
so what the server can guarantee is covered here: the registry bindings and
their resolution through `_templates_for`, plus the shipped files' presence.
Behavioral checks (per-key validation, navigation) are exercised in the app.
"""
import os
import re

from fused_render.server import templates as _server_templates

# The two file extensions that bind `history` directly, so the view can be
# opened ON them and `targetPath` is the file itself. (`.*.json` also binds
# `history`, but there the view is on the SIDECAR and targetPath resolves back
# to the target file — never to the sidecar, so the sidecar's own mode list is
# not what the outbound links must satisfy.)
HISTORY_TARGET_FILES = ("/x/table.parquet", "/x/sine.html")


def modes(path, is_dir=False):
    entries, error = _server_templates._templates_for(path, is_dir)
    return [e["mode"] for e in entries], error


def history_template_text():
    with open(
        os.path.join(_server_templates.TEMPLATES_DIR, "history", "template.html"),
        encoding="utf-8",
    ) as f:
        return f.read()


def hardcoded_nav_modes():
    """Every `_mode=<name>` the template hardcodes into an outbound URL.

    Matches a `"` immediately before `_mode=` so this sees only real string
    literals handed to navigateShell — prose mentions of dead modes in the
    surrounding comments write them in backticks, and the one READ of the
    param (`.get("_mode")`, the continue banner's label) has no `=`.
    """
    return set(re.findall(r'"_mode=([a-z_]+)', history_template_text()))


def test_sidecar_default_mode_is_history():
    # `.html.json` (2 segments) beats the wildcard `.*.json` (also 2, but a
    # literal beats `*` at equal length — CT-3), which beats bare `.json` (1).
    entries, error = _server_templates._templates_for("/x/sine.html.json", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["history", "tree", "code"]
    assert entries[0]["path"].endswith("history/template.html")
    assert entries[0]["icon"] is not None


def test_sidecar_wildcard_matches_any_extension():
    # `.*.json` (HV-2) is generic — any `<name>.<ext>.json` is a sidecar, not
    # just `.html.json`. No `annotate`: annotating the sidecar log itself
    # doesn't make sense (comments belong on the target file, HV-8).
    entries, error = _server_templates._templates_for("/x/table.parquet.json", False)
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
    assert modes("/x/data.json", False) == (
        ["tree", "code", "duckdb", "claude", "versions", "reader"], None)


def test_every_hardcoded_nav_mode_still_exists_for_its_file_target():
    """Regression: the timeline's rows linked to modes their target no longer had.

    The history view is bound to FILE keys, and every row it wires up navigates
    to `targetPath` — a file. Two rows named modes that had been deregistered
    from file keys, so the click hit SPEC PT-9's forgiving unknown-`_mode`
    fallback: the shell silently opened the file's DEFAULT view and dropped the
    deep-link param, losing exactly what the user asked for. The session row
    asked for `_mode=claude` (D235 left `claude` on `/` only) and the comment
    row asked for `_mode=annotate` (D235 deregistered it from all 66 keys).

    This pins the modes against the registry through the real resolution path
    rather than against a hardcoded expected string, because the whole bug class
    is "a binding moved and this link didn't" — a test naming the modes it
    expects would have kept passing through D235 exactly as the template did.
    """
    found = hardcoded_nav_modes()
    assert found, "no hardcoded `_mode=` links found — did the regex drift?"
    for path in HISTORY_TARGET_FILES:
        offered, error = modes(path, False)
        assert error is None, path
        for mode in sorted(found):
            assert mode in offered, (
                "history/template.html navigates to _mode=%s, but %s only offers %r"
                % (mode, path, offered)
            )


def test_comment_rows_are_inert_not_deep_links():
    """Regression: the comment row pretended to be a link to a nonexistent mode.

    `annotate` is registered for zero keys after D235, and `claude` — the
    substitute the session row could use — has no `comment` param to focus one
    annotation with. There is nowhere honest to send the click, so the row keeps
    showing the comment text but is no longer clickable. Asserting on the
    template source is the only server-side reach we have (HV-1: no .py), and it
    is the assertion that matters: any reintroduced `annotate`/`comment=` deep
    link here is dead again on arrival.

    Both checks look at string LITERALS only (same discipline as
    `hardcoded_nav_modes`) — the comment above the row names both dead spellings
    in prose to explain itself, and that prose must not trip the test.
    """
    assert "annotate" not in hardcoded_nav_modes()
    assert not re.search(r'"[^"\n]*\bcomment=', history_template_text())
    assert not any(
        "annotate" in offered for offered in (modes(p, False)[0] for p in HISTORY_TARGET_FILES)
    )


def test_template_ships_html_and_icon_only():
    d = os.path.join(_server_templates.TEMPLATES_DIR, "history")
    files = sorted(os.listdir(d))
    assert files == ["icon.svg", "template.html"]  # no .py — HV-1


def test_template_holds_inline_schema_for_all_owned_keys():
    with open(
        os.path.join(_server_templates.TEMPLATES_DIR, "history", "template.html"),
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
