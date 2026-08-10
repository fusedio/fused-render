"""Tests for template resolution (D73): built-in templates/registry.json,
unified suffix-pattern matcher (multi-dot keys, `*` wildcard segments,
trailing-"/" directory keys), user-registry precedence, and sentinel rules.
"""
import json
import os

import pytest

from _thread_scoped import this_thread_only

from fused_render.server import templates as server


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


def _core_list(key: str) -> list:
    """The core registry's ordered names for `key`. Derived on purpose: the tests
    that use it assert a FALLBACK reaches the built-in list, which is a claim
    about the fallback — spelling the list out again just meant a rebinding
    (D235) broke tests that were not about bindings."""
    with open(os.path.join(server.TEMPLATES_DIR, "registry.json"),
              encoding="utf-8") as f:
        return list(json.load(f)[key])


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
    assert [e["mode"] for e in entries] == [
        "_render", "code", "claude", "versions", "reader", "history"]
    assert entries[0]["path"] is None and entries[0]["icon"] is None
    assert entries[1]["path"].endswith("code/template.html")
    assert entries[2]["path"].endswith("claude/template.html")


def test_builtin_parquet_default_is_duckdb():
    # `history` (HV-2) is bound here too — not `.html`-only.
    entries, error = server._templates_for("/x/data.parquet", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["duckdb", "structure", "h3", "claude", "versions", "history",
            "geometry_editor"]
    assert entries[0]["path"].endswith("duckdb/template.html")


# ------------------------------------------------------------ reader mode (RD)
#
# `reader` (listen/TTS mode, templates/reader/) is the LAST mode on text-bearing
# keys — it used to sit immediately before `annotate`, which is deregistered as of
# D235 (the annotation tools live in the claude pane now, not in a mode of
# their own). It is deliberately absent from binary/visual keys (images, 3D, geo,
# media, archives, parquet data).


def test_reader_is_the_last_mode_on_text_keys():
    # Reader trails every real view on representative text formats, and is never
    # the default (first entry stays the content view).
    cases = {
        "/x/notes.md": ["markdown", "code", "claude", "versions", "reader"],
        "/x/data.csv": ["duckdb", "excel", "code", "claude", "versions",
                        "reader"],
        "/x/paper.pdf": ["pdf", "pdf_studio", "reader"],
        "/x/log.txt": ["code", "claude", "versions", "reader"],
    }
    for path, expected in cases.items():
        got, error = modes(path)
        assert error is None, path
        assert got == expected, path
        assert got[0] != "reader", path       # never the default
        assert got[-1] == "reader", path      # always last


def test_no_builtin_key_binds_text():
    # `text` and `code` render the same bytes; `code` just renders them better
    # (syntax, line numbers, an editor), so no built-in key offers `text` at
    # all — the plain viewer survives only as a template a user registry can
    # bind by hand. Derived from the registry rather than spelled out per key,
    # so a key added later is covered.
    with open(os.path.join(server.TEMPLATES_DIR, "registry.json"),
              encoding="utf-8") as f:
        registry = json.load(f)
    offenders = [
        key for key, value in registry.items()
        if isinstance(value, list) and "text" in value
    ]
    assert offenders == []


def test_former_text_keys_still_offer_code():
    # Dropping `text` must never leave a key without a plain-bytes viewer:
    # every key that used to lean on it opens in `code` instead.
    for path in ["/x/readme.txt", "/x/server.log"]:
        got, error = modes(path)
        assert error is None, path
        assert "code" in got, path


def test_reader_absent_on_binary_visual_keys():
    # Images, 3D models, and other non-text surfaces must not offer reader.
    for path in ["/x/pic.png", "/x/scene.glb", "/x/video.mp4", "/x/data.parquet", "/x/tiles.pmtiles"]:
        got, error = modes(path)
        assert error is None, path
        assert "reader" not in got, path


# --------------------------------------------------------------- git mode (GT)
#
# `git` (SPEC §33) is the condition-gated WORKING TREE view — staging,
# discarding, stashing, committing, branches, push/pull. Every one of those is a
# repository-level act, so it is a FOLDER-ONLY mode: bound to the universal "/"
# directory key and to no file extension, and never a default. It spent a while
# riding along on every key `versions` was on, because a folder then had no mode
# switcher of its own — the preview pane's surface acted on the selected ROW,
# always a file. The pane peeks FOLDER rows now, so the ride is retired. Per-file
# history stays `versions`'. See tests/test_git_scope.py for the rule itself;
# this file pins what the resolver hands back.


def test_git_is_offered_on_directories_and_on_no_file_key():
    # Not one authored-file key answers with it...
    for path in ["/x/mod.py", "/x/app.tsx", "/x/deploy.sh", "/x/site.css",
                 "/x/config.yaml", "/x/pyproject.toml", "/x/tsconfig.json",
                 "/x/main.tf", "/x/notes.md", "/x/paper.tex",
                 "/x/readme.txt", "/x/server.log", "/x/page.html"]:
        got, error = modes(path)
        assert error is None, path
        assert "git" not in got, path
    # ...and the directory key is where it lives instead.
    assert "git" in modes("/x/somedir", is_dir=True)[0]


def test_versions_survives_on_every_key_git_left():
    # De-linking took `git` off the file keys and nothing else with it: the
    # per-file question still has its answer.
    for path in ["/x/mod.py", "/x/app.tsx", "/x/deploy.sh", "/x/site.css",
                 "/x/config.yaml", "/x/pyproject.toml", "/x/tsconfig.json",
                 "/x/main.tf", "/x/notes.md", "/x/paper.tex",
                 "/x/readme.txt", "/x/server.log", "/x/page.html"]:
        got, error = modes(path)
        assert error is None, path
        assert "versions" in got, path


def test_git_follows_versions_immediately_on_the_directory_key():
    # The one list that still holds both: the working tree and its history sit
    # together in switcher order.
    got, _ = modes("/x/somedir", is_dir=True)
    assert got.index("versions") + 1 == got.index("git")


def test_the_file_side_history_and_chat_are_versions_and_claude():
    # The other half of the same split: what `git` and `claude` stopped offering
    # on a file, `versions` and `claude` now do — and in that order, chat
    # then history, on every key that has them.
    for path in ["/x/mod.py", "/x/app.tsx", "/x/deploy.sh", "/x/site.css",
                 "/x/config.yaml", "/x/pyproject.toml", "/x/tsconfig.json",
                 "/x/main.tf", "/x/notes.md", "/x/paper.tex",
                 "/x/readme.txt", "/x/server.log", "/x/page.html",
                 "/x/data.parquet", "/x/data.csv", "/x/book.ipynb"]:
        got, error = modes(path)
        assert error is None, path
        assert "claude" in got, path
        assert "versions" in got, path
        assert got.index("claude") + 1 == got.index("versions"), path
        assert got[0] not in ("claude", "versions"), path  # never default


def test_there_is_exactly_one_chat_mode_on_every_key():
    # D235 folded the two chat templates into one: `claude` is THE chat, for a
    # file and for a directory alike. What this pins is the property the old
    # directories-only binding existed to protect — a target is never offered two
    # Claude modes — now held by there being only one to offer. Counted rather
    # than name-checked, because the guarantee is about how MANY chats a key
    # offers, not about the absence of a name that no longer exists.
    for path in ["/x/notes.md", "/x/page.html", "/x/data.parquet", "/x/mod.py"]:
        got, error = modes(path)
        assert error is None, path
        assert got.count("claude") == 1, path
    assert modes("/x/somedir", is_dir=True)[0].count("claude") == 1


def test_annotate_is_deregistered_everywhere():
    # D235: the annotation tools live in the claude pane, so the standalone
    # `annotate` mode is bound to nothing. The folder still ships (a user may
    # re-bind it themselves, §16) — this is about the CORE registry.
    with open(os.path.join(server.TEMPLATES_DIR, "registry.json"),
              encoding="utf-8") as f:
        registry = json.load(f)
    offenders = [k for k, v in registry.items()
                 if isinstance(v, list) and "annotate" in v]
    assert offenders == []


def test_git_is_never_the_default_mode():
    # A gated mode cannot be the default anyway (PT-8), but the ORDER is also
    # deliberate: the content view always comes first in the list.
    for path in ["/x/mod.py", "/x/notes.md", "/x/readme.txt", "/x/server.log",
                 "/x/page.html", "/x/somedir"]:
        got, _ = modes(path, is_dir=path.endswith("somedir"))
        assert got[0] != "git", path


def test_the_file_side_pair_sits_before_the_trailing_meta_mode():
    # `reader` trails everything (RD); the chat/history pair slots in ahead of it
    # rather than after, so the content views and then the companion views read
    # left to right.
    for path in ["/x/mod.py", "/x/notes.md", "/x/readme.txt"]:
        got, _ = modes(path)
        assert got.index("versions") < got.index("reader"), path


def test_the_file_side_pair_is_absent_from_media_and_binary_keys():
    # Chat and history are offered where a human authors or analyses the bytes.
    # A spreadsheet, a 3D model, a video, an archive or a PDF is none of those,
    # so those lists are left alone rather than churned.
    for path in ["/x/book.xlsx", "/x/scene.glb", "/x/clip.mp4",
                 "/x/bundle.tar.gz", "/x/paper.pdf", "/x/tiles.pmtiles",
                 "/x/warehouse.duckdb"]:
        got, error = modes(path)
        assert error is None, path
        assert "git" not in got, path
        assert "claude" not in got, path
        assert "versions" not in got, path


def test_gated_directory_peers_follow_the_listing():
    # `_listing` stays the default of the universal `/` key (D81); the gated
    # peers follow it in switcher order (left to right). `app` (the app itself,
    # full-bleed — what OPENING an app lands on) leads them, then the split
    # build view, then `versions` ahead of `git` — for an app folder the version
    # timeline is what you want first, and the raw commit log sits one click
    # further (#361).
    got, error = modes("/x/somedir", is_dir=True)
    assert error is None
    assert got == ["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"]


def test_git_ships_a_condition_gate_and_an_icon():
    path, error = server._resolve_name("git")
    assert error is None and path is not None
    folder = os.path.dirname(path)
    assert os.path.isfile(os.path.join(folder, "condition.py"))
    assert os.path.isfile(os.path.join(folder, "icon.svg"))
    assert os.path.isfile(os.path.join(folder, "log.py"))


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
    # a `.zarr`-named dir carries the AOI streamer and the raw member listing as
    # peer modes (the legacy `zarr` template is gone; folder-level detection for
    # non-`.zarr` dirs is handled by the gate on the "/" key instead).
    assert modes("/x/store.zarr", is_dir=True) == (["zarr_aoi", "_listing"], None)
    # a *file* named .zarr does not match the directory key
    assert modes("/x/store.zarr", is_dir=False) == ([], None)


def test_unmapped_file_empty_and_plain_dir_lists():
    # an unmapped, non-existent file resolves to nothing — it can't be sniffed
    # as text (no such path), so it stays on the metadata fallback
    assert modes("/x/a.xyz") == ([], None)
    # every directory resolves the universal `/` key (D81): the built-in
    # listing (default) plus two
    # offered-but-gated candidates — `graph` (the link graph, SPEC §32) and
    # `zarr_aoi` — for a plain folder, a dotted folder and the filesystem root
    # alike. Each gated mode is dropped unless its condition.py says otherwise;
    # see tests/test_graph_condition.py and the zarr_aoi tests below.
    assert modes("/x/somedir", is_dir=True) == (["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"], None)
    assert modes("/x/my.data", is_dir=True) == (["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"], None)
    assert modes("/", is_dir=True) == (["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"], None)


# --------------------------------------------- text sniff for unmapped files

def test_unmapped_text_file_falls_back_to_the_code_viewer(tmp_path):
    # Whole-name dotfiles and extensionless files can't match any suffix key,
    # but they're plain text -> the sniff offers the viewer .txt gets.
    for name, body in [
        (".gitignore", "node_modules\n*.log\n"),
        (".gitconfig", "[user]\n  name = x\n"),
        ("Makefile", "all:\n\tgcc\n"),
        ("LICENSE", "MIT License\n"),
    ]:
        p = tmp_path / name
        p.write_text(body)
        assert modes(str(p)) == (["code"], None), name


def test_unmapped_empty_file_is_text(tmp_path):
    p = tmp_path / ".npmrc"
    p.write_text("")
    assert modes(str(p)) == (["code"], None)


def test_text_sniff_fallback_offers_code_only(tmp_path):
    # `code` renders the same bytes as `text` but better, so the sniff fallback
    # offers it alone — no `text` peer to switch to.
    p = tmp_path / ".gitignore"
    p.write_text("node_modules\n*.log\n")
    assert modes(str(p)) == (["code"], None)


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
    assert modes("/x/a.json")[0] == _core_list(".json")  # builtin still applies


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
    assert m == _core_list(".csv")
    assert "no-such-template" in error


def test_all_dangling_names_fall_back(user_dir):
    # With splice gone, "..." is just an unresolved name; a value of all
    # dangling names resolves to nothing -> built-in fallback, error names one.
    user_dir.registry({".csv": ["...", "..."]})
    m, error = modes("/x/a.csv")
    assert m == _core_list(".csv")
    assert "..." in error


def test_bad_value_type_falls_back(user_dir):
    user_dir.registry({".csv": 42})
    m, error = modes("/x/a.csv")
    assert m == _core_list(".csv")
    assert "must be a list" in error


def test_unreadable_user_registry_reports_and_falls_back(user_dir):
    (user_dir.path / "registry.json").write_text("{not json")
    m, error = modes("/x/a.csv")
    assert m == _core_list(".csv")
    assert "cannot read registry.json" in error


# ------------------------------------------------- conditional templates (PT-8)
#
# Evaluation is deferred (SPEC CT-12): stat only MARKS gated entries
# `conditional: True` (never runs the gate — it may do remote I/O), and
# /api/fs/conditions resolves them in the background. These tests exercise
# both halves through _templates_for and _conditions_payload.


def conditions(path):
    """Resolved gate map for a real file: {mode: bool}, error."""
    payload = server._conditions_payload(path)
    return payload["conditions"], payload.get("error")


@pytest.fixture()
def csv_file(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("x\n1\n")
    return str(p)


def test_condition_marks_entry_without_evaluating(user_dir):
    # The gate would blow up if run — stat must not run it, only mark it.
    user_dir.template("special", condition="def main(path):\n    raise RuntimeError\n")
    user_dir.registry({".csv": ["special", "code"]})
    entries, error = server._templates_for("/x/a.csv", False)
    assert [e["mode"] for e in entries] == ["special", "code"]
    assert entries[0].get("conditional") is True
    assert "conditional" not in entries[1]
    assert error is None


def test_condition_true_allows_template(user_dir, csv_file):
    user_dir.template("special", condition="def main(path):\n    return True\n")
    user_dir.registry({".csv": ["special", "code"]})
    cond, error = conditions(csv_file)
    assert cond == {"special": True}
    assert error is None


def test_condition_false_disallows_template(user_dir, csv_file):
    user_dir.template("special", condition="def main(path):\n    return False\n")
    user_dir.registry({".csv": ["special", "code"]})
    cond, error = conditions(csv_file)
    assert cond == {"special": False}
    assert error is None


def test_condition_receives_file_path(user_dir, tmp_path):
    # Only show the template for files under a "reports" directory.
    user_dir.template(
        "special",
        condition="def main(path):\n    return 'reports' in path\n",
    )
    user_dir.registry({".csv": ["special", "code"]})

    (tmp_path / "reports").mkdir()
    hit = tmp_path / "reports" / "a.csv"
    hit.write_text("x\n")
    miss = tmp_path / "a.csv"
    miss.write_text("x\n")

    assert conditions(str(hit))[0] == {"special": True}
    assert conditions(str(miss))[0] == {"special": False}


def test_condition_missing_is_unconditional(user_dir, csv_file):
    user_dir.template("special")  # no condition.py
    user_dir.registry({".csv": ["special", "code"]})
    entries, error = server._templates_for("/x/a.csv", False)
    assert [e["mode"] for e in entries] == ["special", "code"]
    assert all("conditional" not in e for e in entries)
    assert error is None
    # ... and the conditions payload has nothing to resolve.
    cond, err = conditions(csv_file)
    assert cond == {} and err is None


def test_condition_error_disallows_and_reports(user_dir, csv_file):
    user_dir.template(
        "special", condition="def main(path):\n    raise ValueError('boom')\n"
    )
    user_dir.registry({".csv": ["special", "code"]})
    cond, error = conditions(csv_file)
    assert cond == {"special": False}
    assert "boom" in error


def test_condition_missing_main_disallows_and_reports(user_dir, csv_file):
    user_dir.template("special", condition="x = 1\n")  # no `main`
    user_dir.registry({".csv": ["special", "code"]})
    cond, error = conditions(csv_file)
    assert cond == {"special": False}
    assert "main" in error


def test_condition_missing_target_is_404(user_dir, tmp_path):
    resp = server._conditions_payload(str(tmp_path / "nope.csv"))
    assert resp.status_code == 404


def test_condition_reevaluated_per_call(user_dir, csv_file):
    # Registries + conditions are read fresh per call (no restart): editing
    # condition.py flips the verdict on the next resolution.
    user_dir.template("special", condition="def main(path):\n    return False\n")
    user_dir.registry({".csv": ["special", "code"]})
    assert conditions(csv_file)[0] == {"special": False}

    (user_dir.path / "special" / "condition.py").write_text(
        "def main(path):\n    return True\n"
    )
    assert conditions(csv_file)[0] == {"special": True}


def test_conditions_run_concurrently(user_dir, csv_file):
    # Independent gates are evaluated in parallel, so total time is the slowest
    # single gate, not their sum. Four ~0.3s sleeps would take ~1.2s serially;
    # concurrently they finish in well under that. Generous margin for CI jitter.
    import time

    sleep = "import time\ndef main(path):\n    time.sleep(0.3)\n    return True\n"
    names = [f"cond{i}" for i in range(4)]
    for name in names:
        user_dir.template(name, condition=sleep)
    user_dir.registry({".csv": names})

    t = time.perf_counter()
    cond, error = conditions(csv_file)
    elapsed = time.perf_counter() - t

    assert cond == {name: True for name in names} and error is None
    assert elapsed < 0.9, f"expected concurrent (~0.3s), got {elapsed:.2f}s (serial would be ~1.2s)"


def test_condition_pool_failure_falls_back_to_serial(user_dir, csv_file, tmp_path, monkeypatch):
    # If the thread pool can't be created/run (e.g. the OS refuses a new thread
    # under load), evaluation must NOT propagate and 500 the request — it falls
    # back to serial evaluation, preserving both the fail-closed guarantee and
    # correct results.
    user_dir.template("a", condition="def main(path):\n    return True\n")
    user_dir.template("b", condition="def main(path):\n    return 'keep' in path\n")
    user_dir.registry({".csv": ["a", "b", "code"]})

    import concurrent.futures

    def boom(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", boom)

    keep = tmp_path / "keep.csv"
    keep.write_text("x\n")
    # Serial fallback still evaluates every gate correctly.
    assert conditions(str(keep)) == ({"a": True, "b": True}, None)
    assert conditions(csv_file) == ({"a": True, "b": False}, None)


def test_stat_never_blocks_on_slow_condition(user_dir):
    # The whole point of deferral: a gate sleeping 5s must not delay stat.
    import time

    user_dir.template(
        "slow", condition="import time\ndef main(path):\n    time.sleep(5)\n    return True\n"
    )
    user_dir.registry({".csv": ["slow", "code"]})

    t = time.perf_counter()
    m, error = modes("/x/a.csv")
    elapsed = time.perf_counter() - t

    assert m == ["slow", "code"] and error is None
    assert elapsed < 1.0, f"stat blocked on a condition gate ({elapsed:.2f}s)"


# ---------------------------------- zarr_aoi gate + registry (real templates)
#
# The legacy `zarr` template was deleted; `zarr_aoi` is the `.zarr/` default and
# an offered-but-gated candidate on the universal "/" key. Its condition.py
# proves a directory is a Zarr store via a zero-I/O name fast-path plus bounded
# `isfile` marker probes — never a directory listing (the remote-timeout risk).


def _zarr_condition_main():
    """Load the real zarr_aoi/condition.py standalone and return its `main`."""
    import importlib.util

    cf = os.path.join(server.TEMPLATES_DIR, "zarr_aoi", "condition.py")
    spec = importlib.util.spec_from_file_location("__zarr_aoi_cond__", cf)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


def test_registry_drops_zarr_template_and_sentinel_keys():
    with open(server.BUILTIN_REGISTRY, encoding="utf-8") as f:
        registry = json.load(f)
    # the hidden sentinel-file keys are gone entirely (they only ever pointed at
    # the deleted template, and zarr_aoi can't open a sentinel-file path)
    for k in (".zgroup", ".zattrs", ".zmetadata"):
        assert k not in registry
    # the legacy `zarr` template name resolves nowhere in the registry...
    for key, value in registry.items():
        assert "zarr" not in value, key
    # ...and its folder is deleted, so the name no longer resolves at all
    assert server._resolve_name("zarr")[0] is None
    # zarr_aoi is the .zarr/ default and a gated candidate on every directory
    assert registry[".zarr/"] == ["zarr_aoi", "_listing"]
    assert registry["/"] == ["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"]


def test_zarr_named_dir_gate_true_with_no_markers(tmp_path):
    # A `.zarr`-named dir matches the `.zarr/` key; the gate's name fast-path
    # returns True with ZERO marker files present (and zero filesystem calls).
    store = tmp_path / "store.zarr"
    store.mkdir()
    assert modes(str(store), is_dir=True) == (["zarr_aoi", "_listing"], None)
    assert _zarr_condition_main()(str(store)) is True
    cond, err = conditions(str(store))
    # The `.zarr/` key is its own mode list and never offers `graph`, so
    # zarr_aoi is the only gate to evaluate here.
    assert cond == {"zarr_aoi": True} and err is None


# `claude: True` in the condition dicts below is not incidental to Zarr: it
# is the chat gate answering "yes" for any existing directory, which is what it
# does since it became the only chat template. These assertions are full-dict
# equality on purpose — a mode silently appearing or vanishing from the gated set
# is exactly what they are for — so its verdict is spelled out here rather than
# the dict being loosened to a subset check.
@pytest.mark.parametrize("marker", [".zmetadata", ".zgroup"])
def test_plain_dir_with_store_marker_gates_true(tmp_path, marker):
    # A non-`.zarr` directory containing an inherently GROUP-root marker is
    # detected as a Zarr store by the "/" key gate — consolidated metadata
    # (.zmetadata, always at the group root) and v2 group (.zgroup). The v3
    # `zarr.json` marker is group/array-ambiguous and covered separately below.
    store = tmp_path / "data"
    store.mkdir()
    (store / marker).write_text("{}")
    assert modes(str(store), is_dir=True) == (["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"], None)
    assert _zarr_condition_main()(str(store)) is True
    cond, err = conditions(str(store))
    assert cond == {"app": False, "claude": True, "git": False, "versions": False, "graph": False, "zarr_aoi": True} and err is None


def test_v3_group_dir_offered(tmp_path):
    # A non-`.zarr` directory whose `zarr.json` declares a v3 GROUP root is a
    # loadable store: zarr.open_group() opens it, so the gate offers zarr_aoi.
    store = tmp_path / "grp"
    store.mkdir()
    (store / "zarr.json").write_text('{"zarr_format": 3, "node_type": "group"}')
    assert modes(str(store), is_dir=True) == (["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"], None)
    assert _zarr_condition_main()(str(store)) is True
    cond, err = conditions(str(store))
    assert cond == {"app": False, "claude": True, "git": False, "versions": False, "graph": False, "zarr_aoi": True} and err is None


def test_bare_array_dir_not_offered(tmp_path):
    # A non-`.zarr` directory whose only marker is `.zarray` is a v2 *bare
    # array*, not a group. zarr_aoi opens stores with zarr.open_group(), which
    # raises on an array root — so the gate deliberately does NOT offer it
    # (offering-then-erroring is worse than a clean plain-folder listing).
    store = tmp_path / "arr"
    store.mkdir()
    (store / ".zarray").write_text("{}")
    assert _zarr_condition_main()(str(store)) is False
    cond, err = conditions(str(store))
    assert cond == {"app": False, "claude": True, "git": False, "versions": False, "graph": False, "zarr_aoi": False} and err is None


def test_v3_bare_array_dir_not_offered(tmp_path):
    # The v3 analogue of the `.zarray` case: a `zarr.json` with
    # node_type == "array" is a v3 bare array root. zarr.open_group() raises on
    # it, so the gate must NOT offer zarr_aoi (offered-then-broken > not-offered).
    store = tmp_path / "v3arr"
    store.mkdir()
    (store / "zarr.json").write_text('{"zarr_format": 3, "node_type": "array"}')
    assert _zarr_condition_main()(str(store)) is False
    cond, err = conditions(str(store))
    assert cond == {"app": False, "claude": True, "git": False, "versions": False, "graph": False, "zarr_aoi": False} and err is None


def test_v3_zarr_json_without_node_type_not_offered(tmp_path):
    # A `zarr.json` that can't be confirmed as a group (missing node_type, or
    # unparseable) fails closed — the gate never offers a store it can't prove
    # is group-shaped, so a malformed root stays a plain listing, not an error.
    store = tmp_path / "ambiguous"
    store.mkdir()
    (store / "zarr.json").write_text("{}")
    assert _zarr_condition_main()(str(store)) is False
    bad = tmp_path / "unparseable"
    bad.mkdir()
    (bad / "zarr.json").write_text("not json{")
    assert _zarr_condition_main()(str(bad)) is False


def test_plain_dir_without_markers_gates_false(tmp_path):
    # A plain directory with none of the markers: zarr_aoi is offered but the
    # gate drops it, while _listing stays unconditional and resolves.
    store = tmp_path / "plain"
    store.mkdir()
    (store / "readme.txt").write_text("hi")
    assert modes(str(store), is_dir=True) == (["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"], None)
    assert _zarr_condition_main()(str(store)) is False
    cond, err = conditions(str(store))
    assert cond == {"app": False, "claude": True, "git": False, "versions": False, "graph": False, "zarr_aoi": False} and err is None

    entries, _ = server._templates_for(str(store), True)
    assert entries[0]["mode"] == "_listing" and "conditional" not in entries[0]
    assert entries[1]["mode"] == "app" and entries[1].get("conditional") is True
    assert entries[2]["mode"] == "claude" and entries[2].get("conditional") is True
    assert entries[3]["mode"] == "versions" and entries[3].get("conditional") is True
    assert entries[4]["mode"] == "git" and entries[4].get("conditional") is True
    assert entries[5]["mode"] == "graph" and entries[5].get("conditional") is True
    assert entries[6]["mode"] == "zarr_aoi" and entries[6].get("conditional") is True
    # `claude` used to sit at index 3 as the one UNCONDITIONAL entry after
    # `_listing` — the "no condition.py at all" case this list also covered. It is
    # deleted; `_listing` at index 0 still covers it.


def test_zarr_condition_fail_closed(tmp_path):
    # Fail closed: any bad input returns False and never raises.
    main = _zarr_condition_main()
    assert main("/no/such/directory/anywhere") is False  # nonexistent
    assert main(__file__) is False                        # a file, not .zarr
    assert main("") is False                              # empty
    # a plain existing dir with no markers is False, trailing slash handled
    plain = tmp_path / "nope"
    plain.mkdir()
    assert main(str(plain) + "/") is False


def test_zarr_named_dir_fast_path_ignores_trailing_slash(tmp_path):
    # The name fast-path strips a trailing slash before the `.zarr` check.
    store = tmp_path / "s.zarr"
    store.mkdir()
    assert _zarr_condition_main()(str(store) + "/") is True


def test_zarr_condition_never_lists_directory(tmp_path, monkeypatch):
    # Efficiency lock-in: the gate must use targeted isfile checks only, never a
    # directory listing (which scales with entry count and times out on big
    # remote stores). Make any listing explode and confirm verdicts hold.
    main = _zarr_condition_main()

    def boom(*a, **k):
        raise AssertionError("zarr_aoi condition must not list the directory")

    # Thread-scoped (tests/_thread_scoped.py) — see test_browse_mount.
    monkeypatch.setattr(os, "listdir", this_thread_only(os.listdir, boom))
    monkeypatch.setattr(os, "scandir", this_thread_only(os.scandir, boom))

    named = tmp_path / "s.zarr"
    named.mkdir()
    assert main(str(named)) is True  # name fast-path, no walk

    marked = tmp_path / "m"
    marked.mkdir()
    (marked / ".zgroup").write_text("{}")
    assert main(str(marked)) is True  # targeted isfile hit, no walk

    plain = tmp_path / "p"
    plain.mkdir()
    assert main(str(plain)) is False  # all isfile misses, still no walk
