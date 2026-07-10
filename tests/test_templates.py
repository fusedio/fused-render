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
        def template(name, condition=None):
            folder = udir / name
            folder.mkdir()
            (folder / "template.html").write_text("<html></html>")
            if condition is not None:
                (folder / "condition.py").write_text(condition)

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
    assert [e["mode"] for e in entries] == ["_render", "code", "claude", "annotate", "history"]
    assert entries[0]["path"] is None and entries[0]["icon"] is None
    assert entries[1]["path"].endswith("code/template.html")
    assert entries[2]["path"].endswith("claude/template.html")


def test_builtin_parquet_default_is_duckdb():
    # `history` (HV-2) is bound here too — not `.html`-only.
    entries, error = server._templates_for("/x/data.parquet", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["duckdb", "structure", "h3", "claude", "annotate", "history"]
    assert entries[0]["path"].endswith("duckdb/template.html")


def test_compressed_tabular_routes_to_duckdb():
    # A gzip/zstd-compressed CSV/JSON is still tabular data DuckDB reads through
    # its auto-decompressing scan, so the 2-segment compound key (.csv.gz) wins
    # over the generic 1-segment .gz archive binding.
    assert modes("/x/data.csv.gz")[0][0] == "duckdb"
    assert modes("/x/data.tsv.zst")[0][0] == "duckdb"
    assert modes("/x/data.json.gz")[0][0] == "duckdb"
    assert modes("/x/data.ndjson.gz")[0][0] == "duckdb"
    # A real archive (or a bare .gz) still opens in the tar viewer, untouched.
    assert modes("/x/bundle.tar.gz") == (["tar"], None)
    assert modes("/x/blob.gz") == (["tar"], None)


def test_duckdb_database_files_route_to_duckdb():
    # .duckdb/.ddb open in the tabular grid; .db stays with the sqlite viewer.
    assert modes("/x/warehouse.duckdb") == (["duckdb"], None)
    assert modes("/x/warehouse.ddb") == (["duckdb"], None)
    assert modes("/x/legacy.db") == (["sqlite"], None)


def test_builtin_zarr_directory_key():
    # zarr dir carries the map preview plus the raw member listing as a peer
    # mode (D81 — replaces the old `?listing=1` escape hatch)
    assert modes("/x/store.zarr", is_dir=True) == (["zarr", "_listing"], None)
    # a *file* named .zarr does not match the directory key
    assert modes("/x/store.zarr", is_dir=False) == ([], None)


def test_unmapped_file_empty_and_plain_dir_lists():
    # an unmapped, non-existent file resolves to nothing — it can't be sniffed
    # as text (no such path), so it stays on the metadata fallback
    assert modes("/x/a.xyz") == ([], None)
    # every directory resolves the universal `/` key (D81): the built-in
    # listing (default) plus the switchable preview (folder browser) view — a
    # plain folder, a dotted folder, and the filesystem root all list.
    assert modes("/x/somedir", is_dir=True) == (["_listing", "preview"], None)
    assert modes("/x/my.data", is_dir=True) == (["_listing", "preview"], None)
    assert modes("/", is_dir=True) == (["_listing", "preview"], None)


# --------------------------------------------- text sniff for unmapped files

def test_unmapped_text_file_falls_back_to_text_viewers(tmp_path):
    # Whole-name dotfiles and extensionless files can't match any suffix key,
    # but they're plain text -> the sniff offers the same viewers .txt gets.
    for name, body in [
        (".gitignore", "node_modules\n*.log\n"),
        (".gitconfig", "[user]\n  name = x\n"),
        ("Makefile", "all:\n\tgcc\n"),
        ("LICENSE", "MIT License\n"),
    ]:
        p = tmp_path / name
        p.write_text(body)
        assert modes(str(p)) == (["text", "code"], None), name


def test_unmapped_empty_file_is_text(tmp_path):
    p = tmp_path / ".npmrc"
    p.write_text("")
    assert modes(str(p)) == (["text", "code"], None)


def test_unmapped_binary_file_stays_metadata(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x89PNG\r\n\x00\x01\x02\x00garbage")
    assert modes(str(p)) == ([], None)


def test_mapped_file_never_hits_text_sniff(tmp_path):
    # A file with a real binding resolves via the registry, not the fallback,
    # even though its bytes are text.
    p = tmp_path / "s.py"
    p.write_text("x = 1\n")
    got, err = modes(str(p))
    assert err is None and got[0] == "code" and "text" not in got


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
    assert modes("/x/a.json")[0] == ["tree", "code", "duckdb", "annotate"]  # builtin still applies


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
    assert m == ["duckdb", "csv", "code", "annotate"]
    assert "no-such-template" in error


def test_all_dangling_names_fall_back(user_dir):
    # With splice gone, "..." is just an unresolved name; a value of all
    # dangling names resolves to nothing -> built-in fallback, error names one.
    user_dir.registry({".csv": ["...", "..."]})
    m, error = modes("/x/a.csv")
    assert m == ["duckdb", "csv", "code", "annotate"]
    assert "..." in error


def test_bad_value_type_falls_back(user_dir):
    user_dir.registry({".csv": 42})
    m, error = modes("/x/a.csv")
    assert m == ["duckdb", "csv", "code", "annotate"]
    assert "must be a list" in error


def test_unreadable_user_registry_reports_and_falls_back(user_dir):
    (user_dir.path / "registry.json").write_text("{not json")
    m, error = modes("/x/a.csv")
    assert m == ["duckdb", "csv", "code", "annotate"]
    assert "cannot read registry.json" in error


# ------------------------------------------------- conditional templates (PT-8)

def test_condition_true_keeps_template(user_dir):
    user_dir.template("special", condition="def method(path):\n    return True\n")
    user_dir.registry({".csv": ["special", "code"]})
    m, error = modes("/x/a.csv")
    assert m == ["special", "code"]
    assert error is None


def test_condition_false_drops_template(user_dir):
    user_dir.template("special", condition="def method(path):\n    return False\n")
    user_dir.registry({".csv": ["special", "code"]})
    m, error = modes("/x/a.csv")
    assert m == ["code"]
    assert error is None


def test_condition_receives_file_path(user_dir):
    # Only show the template for files under a "reports" directory.
    user_dir.template(
        "special",
        condition="def method(path):\n    return 'reports' in path\n",
    )
    user_dir.registry({".csv": ["special", "code"]})

    m, _ = modes("/x/reports/a.csv")
    assert m == ["special", "code"]

    m, _ = modes("/x/other/a.csv")
    assert m == ["code"]


def test_condition_missing_is_unconditional(user_dir):
    user_dir.template("special")  # no condition.py
    user_dir.registry({".csv": ["special", "code"]})
    m, error = modes("/x/a.csv")
    assert m == ["special", "code"]
    assert error is None


def test_condition_error_drops_and_reports(user_dir):
    user_dir.template(
        "special", condition="def method(path):\n    raise ValueError('boom')\n"
    )
    user_dir.registry({".csv": ["special", "code"]})
    m, error = modes("/x/a.csv")
    assert m == ["code"]
    assert "boom" in error


def test_condition_missing_method_drops_and_reports(user_dir):
    user_dir.template("special", condition="x = 1\n")  # no `method`
    user_dir.registry({".csv": ["special", "code"]})
    m, error = modes("/x/a.csv")
    assert m == ["code"]
    assert "method" in error


def test_condition_reevaluated_per_call(user_dir):
    # Registries + conditions are read fresh per stat (no restart): editing
    # condition.py flips visibility on the next resolution.
    user_dir.template("special", condition="def method(path):\n    return False\n")
    user_dir.registry({".csv": ["special", "code"]})
    assert modes("/x/a.csv")[0] == ["code"]

    (user_dir.path / "special" / "condition.py").write_text(
        "def method(path):\n    return True\n"
    )
    assert modes("/x/a.csv")[0] == ["special", "code"]


def test_conditions_run_concurrently(user_dir):
    # Independent gates are evaluated in parallel, so total time is the slowest
    # single gate, not their sum. Four ~0.3s sleeps would take ~1.2s serially;
    # concurrently they finish in well under that. Generous margin for CI jitter.
    import time

    sleep = "import time\ndef method(path):\n    time.sleep(0.3)\n    return True\n"
    names = [f"cond{i}" for i in range(4)]
    for name in names:
        user_dir.template(name, condition=sleep)
    user_dir.registry({".csv": names})

    t = time.perf_counter()
    m, error = modes("/x/a.csv")
    elapsed = time.perf_counter() - t

    assert m == names and error is None
    assert elapsed < 0.9, f"expected concurrent (~0.3s), got {elapsed:.2f}s (serial would be ~1.2s)"
