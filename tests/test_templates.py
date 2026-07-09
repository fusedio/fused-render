"""Tests for template resolution (D73): built-in templates/registry.json,
unified suffix-pattern matcher (multi-dot keys, `*` wildcard segments,
trailing-"/" directory keys), user-registry precedence, and sentinel rules.
"""
import json

import pytest

from fused_render import server


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    """Point the user template dir + registry at a tmp dir; returns helpers
    to write the registry and create user template folders."""
    udir = tmp_path / "user-templates"
    udir.mkdir()
    monkeypatch.setattr(server, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server, "USER_REGISTRY", str(udir / "registry.json"))

    class Helper:
        path = udir

        @staticmethod
        def registry(mapping):
            (udir / "registry.json").write_text(json.dumps(mapping))

        @staticmethod
        def template(name):
            folder = udir / name
            folder.mkdir()
            (folder / "template.html").write_text("<html></html>")

    return Helper


def modes(path, is_dir=False):
    entries, error = server._templates_for(path, is_dir)
    return [e["mode"] for e in entries], error


# ------------------------------------------------- built-in registry sanity

def test_builtin_registry_parses_and_all_names_resolve():
    with open(server.BUILTIN_REGISTRY, encoding="utf-8") as f:
        registry = json.load(f)
    assert isinstance(registry, dict) and registry
    for key, value in registry.items():
        is_dir = key.endswith("/")
        # every key is a well-formed pattern for its population
        assert server._key_segments(key, is_dir) is not None, key
        assert isinstance(value, list) and value, key
        for name in value:
            if name in server.KNOWN_SENTINELS:
                continue
            path, err = server._resolve_name(name)
            assert path is not None, f"{key}: {err}"


def test_builtin_html_default_is_render_sentinel():
    entries, error = server._templates_for("/x/page.html", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["_render", "code", "claude", "annotate"]
    assert entries[0]["path"] is None and entries[0]["icon"] is None
    assert entries[1]["path"].endswith("code/template.html")
    assert entries[2]["path"].endswith("claude/template.html")


def test_builtin_zarr_directory_key():
    # zarr dir carries the map preview plus the raw member listing as a peer
    # mode (D81 — replaces the old `?listing=1` escape hatch)
    assert modes("/x/store.zarr", is_dir=True) == (["zarr", "_listing"], None)
    # a *file* named .zarr does not match the directory key
    assert modes("/x/store.zarr", is_dir=False) == ([], None)


def test_unmapped_file_empty_and_plain_dir_lists():
    # an unmapped file extension resolves to nothing
    assert modes("/x/a.xyz") == ([], None)
    # every directory resolves at least the universal `/` key's ["_listing"]
    # (D81) — a plain folder, a dotted folder, and the filesystem root all list
    assert modes("/x/somedir", is_dir=True) == (["_listing"], None)
    assert modes("/x/my.data", is_dir=True) == (["_listing"], None)
    assert modes("/", is_dir=True) == (["_listing"], None)


# ------------------------------------------------------------------ matcher

def test_specificity_literal_beats_wildcard_beats_shorter():
    reg = {".json": "a", ".*.json": "b", ".xyz.json": "c"}
    assert server._match_registry(reg, "f.xyz.json", False)[1] == "c"
    assert server._match_registry(reg, "f.abc.json", False)[1] == "b"
    assert server._match_registry(reg, "f.json", False)[1] == "a"


def test_rightmost_segment_dominates_tie():
    reg = {".a.*": "left", ".*.json": "right"}
    assert server._match_registry(reg, "x.a.json", False)[1] == "right"


def test_case_insensitive():
    reg = {".tar.gz": "archive"}
    assert server._match_registry(reg, "BACKUP.TAR.GZ", False)[1] == "archive"


def test_dotfile_named_like_key_does_not_match():
    reg = {".json": "a"}
    assert server._match_registry(reg, ".json", False) is None
    # but a hidden file with a real extension does ('.h' is the stem)
    assert server._match_registry(reg, ".h.json", False)[1] == "a"


def test_dir_and_file_keys_are_disjoint():
    reg = {".zarr/": "d", ".zarr": "f"}
    assert server._match_registry(reg, "s.zarr", True)[1] == "d"
    assert server._match_registry(reg, "s.zarr", False)[1] == "f"


def test_wildcard_matches_whole_nonempty_segment_only():
    reg = {".*.json": "b"}
    # `*` never matches an empty segment
    assert server._match_registry(reg, "a..json", False) is None
    # partial wildcards are invalid keys — never match
    assert server._key_segments(".geo*.json", False) is None
    # malformed keys never match
    assert server._key_segments("json", False) is None
    assert server._key_segments("..json", False) is None
    assert server._key_segments(".", False) is None


def test_universal_dir_key_segments():
    # the bare "/" is the universal directory key (D81): zero segments, matches
    # any directory, never a file
    assert server._key_segments("/", True) == []
    assert server._key_segments("/", False) is None


def test_universal_dir_key_lowest_specificity():
    reg = {"/": "any", ".zarr/": "zarr"}
    # a dot-anchored directory key beats the universal key
    assert server._match_registry(reg, "s.zarr", True)[1] == "zarr"
    # a plain folder falls to the universal key
    assert server._match_registry(reg, "plain", True)[1] == "any"
    # files never match the universal (or any) directory key
    assert server._match_registry(reg, "plain", False) is None


# ------------------------------------------------------------ user registry

def test_user_override_beats_builtin(user_dir):
    user_dir.template("geo")
    user_dir.registry({".csv": "geo"})
    assert modes("/x/a.csv") == (["geo"], None)


def test_user_null_disables(user_dir):
    user_dir.registry({".png": None})
    m, error = modes("/x/a.png")
    assert m == [] and error is None


def test_user_any_match_beats_more_specific_builtin(user_dir):
    # user .json wins over builtin even for a compound filename
    user_dir.template("geo")
    user_dir.registry({".json": "geo"})
    assert modes("/x/a.xyz.json") == (["geo"], None)


def test_user_wildcard_key(user_dir):
    user_dir.template("geo")
    user_dir.registry({".*.json": "geo"})
    assert modes("/x/a.tiles.json") == (["geo"], None)
    assert modes("/x/a.json")[0] == ["tree", "code", "annotate"]  # builtin still applies


def test_user_directory_binding(user_dir):
    user_dir.template("bundle")
    user_dir.registry({".obt/": "bundle"})
    assert modes("/x/data.obt", is_dir=True) == (["bundle"], None)
    assert modes("/x/data.obt", is_dir=False) == ([], None)


def test_user_universal_splice_token_is_dangling(user_dir):
    # Splice removed (owner 2026-07-09): "..." resolves to no folder, so it is
    # dropped from the rendered list (and flagged via error), never expanded to
    # the built-in modes. Only the real template survives; a user "/" match
    # still beats the built-in at any specificity.
    user_dir.template("gallery")
    user_dir.registry({"/": ["...", "gallery"]})
    plain_modes, plain_err = modes("/x/plain", is_dir=True)
    assert plain_modes == ["gallery"]
    assert plain_err is not None  # names the dropped "..."
    zarr_modes, _ = modes("/x/s.zarr", is_dir=True)
    assert zarr_modes == ["gallery"]


def test_user_empty_list_disables_dir(user_dir):
    # An empty list disables previews for the type, identical to null — no
    # modes and no built-in fallback.
    user_dir.registry({"/": []})
    assert modes("/x/plain", is_dir=True) == ([], None)
    assert modes("/x/s.zarr", is_dir=True) == ([], None)


def test_user_universal_replace_beats_builtin(user_dir):
    # a user match at ANY specificity beats the built-in (CT-3), so a universal
    # "/" replace clobbers even the built-in zarr preview — the documented
    # "user can shoot themselves" posture; the splice form above is the safe one
    user_dir.template("gallery")
    user_dir.registry({"/": ["gallery"]})
    assert modes("/x/plain", is_dir=True) == (["gallery"], None)
    assert modes("/x/s.zarr", is_dir=True) == (["gallery"], None)


def test_user_can_rebind_html(user_dir):
    user_dir.registry({".html": ["code"]})
    assert modes("/x/page.html") == (["code"], None)


def test_user_html_splice_token_dropped(user_dir):
    # Splice removed: "..." is dangling, dropped from the rendered list (error
    # names it) — it no longer re-adds the built-in _render/claude/annotate.
    user_dir.registry({".html": ["code", "..."]})
    m, error = modes("/x/page.html")
    assert m == ["code"]
    assert "..." in error


def test_user_zarr_dir_rebind_and_disable(user_dir):
    user_dir.registry({".zarr/": None})
    assert modes("/x/s.zarr", is_dir=True) == ([], None)


def test_unknown_sentinel_dropped_with_error(user_dir):
    user_dir.registry({".csv": ["_bogus", "code"]})
    m, error = modes("/x/a.csv")
    assert m == ["code"]
    assert "_bogus" in error


def test_unresolvable_user_value_falls_back_to_builtin(user_dir):
    user_dir.registry({".csv": "no-such-template"})
    m, error = modes("/x/a.csv")
    assert m == ["csv", "code", "annotate"]
    assert "no-such-template" in error


def test_all_dangling_names_fall_back(user_dir):
    # With splice gone, "..." is just an unresolved name; a value of all
    # dangling names resolves to nothing -> built-in fallback, error names one.
    user_dir.registry({".csv": ["...", "..."]})
    m, error = modes("/x/a.csv")
    assert m == ["csv", "code", "annotate"]
    assert "..." in error


def test_bad_value_type_falls_back(user_dir):
    user_dir.registry({".csv": 42})
    m, error = modes("/x/a.csv")
    assert m == ["csv", "code", "annotate"]
    assert "must be a list" in error


def test_unreadable_user_registry_reports_and_falls_back(user_dir):
    (user_dir.path / "registry.json").write_text("{not json")
    m, error = modes("/x/a.csv")
    assert m == ["csv", "code", "annotate"]
    assert "cannot read registry.json" in error
