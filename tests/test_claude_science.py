"""Claude Science artifacts in the Home/apps listing (claude_science.py, D212).

The fixtures mirror the real store byte-for-byte, because every rule in the
module is a rule about *that* layout:

    <root>/orgs/<org-uuid>/artifacts/<project-id>/<artifact-uuid>/v<hex>_<name>.<ext>
    <root>/orgs/<org-uuid>/operon-cli.db          projects(id, name, …)

plus the store's own bookkeeping sitting right beside the project dirs
(``.thumbnails/``, ``.example_*_seeded``), which the listing has to walk past.

`operon-cli.db` here is a real SQLite file with the two columns the module
introspects for — the point is to exercise the actual sqlite path, not a stub
of it, since "the DB moved/locked/changed shape" is precisely the failure mode
the fallback exists for.
"""
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_science
from fused_render.server import create_app

ORG = "26f4899b-5698-4e38-8d24-7f2a496d74aa"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An empty Claude Science store at <tmp>/.claude-science, env-selected."""
    root = tmp_path / ".claude-science"
    (root / "orgs" / ORG / "artifacts").mkdir(parents=True)
    monkeypatch.setenv(claude_science.DIR_ENV, str(root))
    return root


def _artifacts_dir(store, org=ORG):
    return store / "orgs" / org / "artifacts"


def _artifact(store, project, uuid, versions, *, org=ORG, body=b"x"):
    """One artifact dir holding `versions` (filenames), oldest first by mtime."""
    d = _artifacts_dir(store, org) / project / uuid
    d.mkdir(parents=True, exist_ok=True)
    for i, filename in enumerate(versions):
        path = d / filename
        path.write_bytes(body)
        # Distinct, ordered mtimes: "newest version wins" must be tested against
        # real timestamps, not filesystem write order.
        os.utime(path, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
    return d


def _projects_db(store, rows, *, org=ORG, table="projects",
                 columns=("id", "name")):
    db = store / "orgs" / org / "operon-cli.db"
    conn = sqlite3.connect(str(db))
    try:
        cols = ", ".join(f"{c} TEXT" for c in columns)
        conn.execute(f"CREATE TABLE {table} ({cols})")
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        conn.commit()
    finally:
        conn.close()
    return db


def _by_name(apps):
    return {a["name"]: a for a in apps}


# ------------------------------------------------------------------- discovery

def test_lists_one_app_per_artifact_named_after_the_saved_file(store):
    _artifact(store, "proj_f0c0cfbcfb8f", "cd5e48e0-64e7-4ebc-b7d5-2f7c191359b7",
              ["v6f4b965a_building_h3_compare.png"])
    _artifact(store, "proj_f0c0cfbcfb8f", "f3e59373-f3d5-46d7-93c4-f689b82d1fe1",
              ["ve2edc51e_overture_coverage_matrix.csv"])

    apps = _by_name(claude_science.list_apps())
    assert set(apps) == {"building_h3_compare.png", "overture_coverage_matrix.csv"}
    app = apps["building_h3_compare.png"]
    assert app["path"] == str(
        _artifacts_dir(store) / "proj_f0c0cfbcfb8f"
        / "cd5e48e0-64e7-4ebc-b7d5-2f7c191359b7")
    assert app["entry"] == str(
        _artifacts_dir(store) / "proj_f0c0cfbcfb8f"
        / "cd5e48e0-64e7-4ebc-b7d5-2f7c191359b7"
        / "v6f4b965a_building_h3_compare.png")
    assert app["source"] == "claude-science"


def test_a_figure_or_table_is_an_entry_but_not_an_entry_html(store):
    """The distinction the whole card design rests on: a PNG/CSV artifact has a
    real entry to open and preview, but must never reach the HTML-only
    /render iframe."""
    _artifact(store, "proj_a", "u1", ["v0a4dc96c_overture_feature_counts.csv"])
    _artifact(store, "proj_a", "u2", ["veb05d344_building_h3_global.png"])

    for app in claude_science.list_apps():
        assert app["entry"] is not None
        assert app["entry_html"] is None
        assert app["title"] is None


def test_an_html_artifact_gets_entry_html_and_its_title(store):
    d = _artifact(store, "proj_a", "u1", ["v1234abcd_binding_report.html"])
    (d / "v1234abcd_binding_report.html").write_text(
        "<html><head><title>Binding  report</title></head></html>")

    app = claude_science.list_apps()[0]
    assert app["name"] == "binding_report.html"
    assert app["entry_html"] == app["entry"]
    assert app["title"] == "Binding report"  # whitespace collapsed


def test_newest_version_wins_and_names_the_app(store):
    _artifact(store, "proj_a", "u1", [
        "vaaaaaaaa_growth_curve.csv",
        "vbbbbbbbb_growth_curve.csv",
        "vcccccccc_growth_curve.csv",
    ])
    app = claude_science.list_apps()[0]
    assert os.path.basename(app["entry"]) == "vcccccccc_growth_curve.csv"
    assert app["name"] == "growth_curve.csv"
    assert app["updated_at"] == 1_700_000_120


def test_versions_are_not_separate_apps(store):
    _artifact(store, "proj_a", "u1", ["v1_a.csv", "v2_a.csv", "v3_a.csv"])
    assert len(claude_science.list_apps()) == 1


# ------------------------------------------------------------------------ tags

def test_tag_is_the_project_display_name_from_the_db(store):
    _artifact(store, "proj_f0c0cfbcfb8f", "u1", ["vaaaaaaaa_fig.png"])
    _projects_db(store, [("proj_f0c0cfbcfb8f", "Overture buildings")])

    assert claude_science.list_apps()[0]["tag"] == "Overture buildings"


def test_tag_falls_back_to_the_project_id_without_a_db(store):
    _artifact(store, "proj_f0c0cfbcfb8f", "u1", ["vaaaaaaaa_fig.png"])
    assert claude_science.list_apps()[0]["tag"] == "proj_f0c0cfbcfb8f"


def test_tag_falls_back_when_the_project_is_absent_from_the_db(store):
    _artifact(store, "proj_unlisted", "u1", ["vaaaaaaaa_fig.png"])
    _projects_db(store, [("proj_other", "Something else")])
    assert claude_science.list_apps()[0]["tag"] == "proj_unlisted"


def test_tag_falls_back_on_an_unrecognised_schema(store):
    """The DB is private to another application: a reshaped `projects` table
    costs prettier tags, never the listing."""
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])
    _projects_db(store, [("proj_a", "Renamed")], columns=("pk", "label_text"))
    assert claude_science.list_apps()[0]["tag"] == "proj_a"


def test_tag_falls_back_on_a_corrupt_db(store):
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])
    (store / "orgs" / ORG / "operon-cli.db").write_bytes(b"not a database")
    assert claude_science.list_apps()[0]["tag"] == "proj_a"


def test_db_is_opened_read_only(store):
    """A store another application owns is never written to — not even a
    journal file, and not by a query that would take a write lock."""
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])
    db = _projects_db(store, [("proj_a", "Project A")])
    before = {p.name: p.stat().st_mtime_ns for p in db.parent.iterdir()}

    assert claude_science.list_apps()[0]["tag"] == "Project A"

    after = {p.name: p.stat().st_mtime_ns for p in db.parent.iterdir()}
    assert after == before


def test_an_alternative_name_column_is_recognised(store):
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])
    _projects_db(store, [("proj_a", "Titled")], columns=("project_id", "title"))
    assert claude_science.list_apps()[0]["tag"] == "Titled"


# ------------------------------------------------------- the store's own files

def test_hidden_store_bookkeeping_is_skipped(store):
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])
    # The thumbnail cache is shaped just like a project dir: shard dirs holding
    # files. Only the leading dot tells it apart.
    thumbs = _artifacts_dir(store) / ".thumbnails" / "66"
    thumbs.mkdir(parents=True)
    (thumbs / "deadbeef.png").write_bytes(b"x")
    (_artifacts_dir(store) / ".example_crispr_screen_seeded").write_text("")

    apps = claude_science.list_apps()
    assert [a["name"] for a in apps] == ["fig.png"]


def test_hidden_artifact_dirs_and_files_are_skipped(store):
    _artifact(store, "proj_a", ".hidden-artifact", ["vaaaaaaaa_fig.png"])
    _artifact(store, "proj_a", "u1", [".DS_Store", "vbbbbbbbb_real.png"])

    apps = claude_science.list_apps()
    assert [a["name"] for a in apps] == ["real.png"]


def test_an_empty_artifact_dir_is_skipped_not_listed_entryless(store):
    (_artifacts_dir(store) / "proj_a" / "u1").mkdir(parents=True)
    _artifact(store, "proj_a", "u2", ["vaaaaaaaa_fig.png"])
    assert [a["name"] for a in claude_science.list_apps()] == ["fig.png"]


def test_a_file_without_a_version_prefix_still_lists(store):
    """The version prefix is another application's private convention; losing
    it should cost the pretty name, not the artifact."""
    _artifact(store, "proj_a", "u1", ["raw_export.csv"])
    app = claude_science.list_apps()[0]
    assert app["name"] == "raw_export.csv"
    assert os.path.basename(app["entry"]) == "raw_export.csv"


def test_prefixed_files_win_over_unprefixed_siblings(store):
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png", "notes.txt"])
    app = claude_science.list_apps()[0]
    assert os.path.basename(app["entry"]) == "vaaaaaaaa_fig.png"


def test_a_figure_and_its_table_are_two_distinguishable_cards(store):
    """The real store saves both under one base name — keeping the extension is
    what stops them rendering as two identical cards."""
    _artifact(store, "proj_a", "u1", ["v668620ec_overture_coverage_matrix.png"])
    _artifact(store, "proj_a", "u2", ["ve2edc51e_overture_coverage_matrix.csv"])

    names = {a["name"] for a in claude_science.list_apps()}
    assert names == {"overture_coverage_matrix.png", "overture_coverage_matrix.csv"}


@pytest.mark.parametrize("filename, expected", [
    ("v6f4b965a_building_h3_compare.png", "building_h3_compare.png"),
    ("ve2edc51e_overture_coverage_matrix.csv", "overture_coverage_matrix.csv"),
    ("vAAAAAAAA_upper.png", "upper.png"),       # digest case is not meaningful
    ("v6f4b965a_two.parts.here.csv", "two.parts.here.csv"),
    ("plain.csv", "plain.csv"),                 # no prefix: kept whole
    ("v6f4b965a_dataset", "dataset"),           # no extension
    ("v6f4b965a_.png", ".png"),
])
def test_artifact_name_parsing(filename, expected):
    assert claude_science.artifact_name(filename) == expected


# ------------------------------------------------------- the bundled sample

def test_the_sample_project_is_skipped_by_default(store):
    """83 of the 97 artifacts on the store this was built against live in the
    demo project. Listing it would bury the user's own work in their own Home."""
    _artifact(store, "proj_example", "u1", ["vaaaaaaaa_seeded_demo.png"])
    _artifact(store, "proj_f0c0cfbcfb8f", "u2", ["vbbbbbbbb_mine.png"])

    assert [a["name"] for a in claude_science.list_apps()] == ["mine.png"]


def test_the_sample_project_lists_when_asked_for(store, monkeypatch):
    monkeypatch.setenv(claude_science.EXAMPLES_ENV, "1")
    _artifact(store, "proj_example", "u1", ["vaaaaaaaa_seeded_demo.png"])
    _artifact(store, "proj_f0c0cfbcfb8f", "u2", ["vbbbbbbbb_mine.png"])

    names = {a["name"] for a in claude_science.list_apps()}
    assert names == {"seeded_demo.png", "mine.png"}


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
def test_the_off_words_keep_the_sample_hidden(store, monkeypatch, value):
    """Same off-words as FUSED_RENDER_CALLS — the two switches must not disagree
    about what "0" means."""
    monkeypatch.setenv(claude_science.EXAMPLES_ENV, value)
    _artifact(store, "proj_example", "u1", ["vaaaaaaaa_seeded_demo.png"])
    assert claude_science.list_apps() == []


def test_only_the_exact_sample_id_is_skipped(store):
    """A user's own project must never be caught by the sample's name."""
    _artifact(store, "proj_examples", "u1", ["vaaaaaaaa_mine.png"])
    _artifact(store, "proj_example_2", "u2", ["vbbbbbbbb_also_mine.png"])
    _artifact(store, "proj_example", "u3", ["vcccccccc_demo.png"])

    names = {a["name"] for a in claude_science.list_apps()}
    assert names == {"mine.png", "also_mine.png"}


def test_a_store_that_is_only_the_sample_lists_empty(store):
    _artifact(store, "proj_example", "u1", ["vaaaaaaaa_demo.png"])
    assert claude_science.list_apps() == []


# --------------------------------------------------------------------- absence

def test_no_store_lists_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(claude_science.DIR_ENV, str(tmp_path / "nope"))
    assert claude_science.list_apps() == []


def test_a_store_without_orgs_lists_empty(tmp_path, monkeypatch):
    root = tmp_path / ".claude-science"
    (root / "conda").mkdir(parents=True)
    monkeypatch.setenv(claude_science.DIR_ENV, str(root))
    assert claude_science.list_apps() == []


def test_the_env_override_expands_a_tilde(monkeypatch):
    monkeypatch.setenv(claude_science.DIR_ENV, "~/somewhere")
    resolved = claude_science.claude_science_dir()
    assert os.path.isabs(resolved) and "~" not in resolved


def test_default_location_when_unset(monkeypatch):
    monkeypatch.delenv(claude_science.DIR_ENV, raising=False)
    assert claude_science.claude_science_dir() == os.path.abspath(
        os.path.expanduser("~/.claude-science"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable dirs")
def test_an_unreadable_project_dir_is_skipped_not_fatal(store):
    _artifact(store, "proj_ok", "u1", ["vaaaaaaaa_fig.png"])
    locked = _artifacts_dir(store) / "proj_locked"
    locked.mkdir()
    (locked / "u2").mkdir()
    locked.chmod(0o000)
    try:
        assert [a["name"] for a in claude_science.list_apps()] == ["fig.png"]
    finally:
        locked.chmod(0o755)


def test_multiple_orgs_are_all_walked(store):
    other = "11111111-2222-3333-4444-555555555555"
    (store / "orgs" / other / "artifacts").mkdir(parents=True)
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_one.png"])
    _artifact(store, "proj_b", "u2", ["vbbbbbbbb_two.png"], org=other)

    assert {a["name"] for a in claude_science.list_apps()} == {"one.png", "two.png"}


def test_the_cap_stops_the_walk_and_is_logged(store, monkeypatch, caplog):
    """The cap bounds the WORK, not just the output — raised in review.

    The first version capped only what it kept: it went on iterating, and on
    calling `_child_dirs` for every remaining project, purely to count what it
    was discarding. A large store paid for a full walk on every Home render to
    produce a list it had already finished.

    The call count is the assertion that matters. Ten artifacts, a cap of 3:
    exactly 4 may be visited — the 3 that are listed plus the single lookahead
    that detects there was more. The exact remainder is deliberately no longer
    reported, because counting it is the work being avoided.
    """
    monkeypatch.setattr(claude_science, "MAX_ARTIFACTS", 3)
    for i in range(10):
        _artifact(store, "proj_a", f"u{i}", [f"vaaaaaaa{i}_fig{i}.png"])

    visited = []
    real = claude_science._artifact_app
    monkeypatch.setattr(claude_science, "_artifact_app",
                        lambda d, tag: (visited.append(d), real(d, tag))[1])

    with caplog.at_level("WARNING", logger="fused_render"):
        apps = claude_science.list_apps()

    assert len(apps) == 3
    assert len(visited) == 4, "the walk must stop at the cap, not run to the end"
    assert "capped at 3" in caplog.text


# ----------------------------------------------------------------- GET /api/apps

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace, store):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _workspace_app(workspace, name, tag="local"):
    d = workspace / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html><body>hi</body></html>")
    return d


def test_api_apps_merges_both_sources(client, workspace, store):
    _workspace_app(workspace, "my-app")
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])
    _projects_db(store, [("proj_a", "Overture buildings")])

    apps = _by_name(client.get("/api/apps").json()["apps"])
    assert set(apps) == {"my-app", "fig.png"}
    assert apps["my-app"]["source"] == "workspace"
    assert apps["my-app"]["entry"] == apps["my-app"]["entry_html"]
    assert apps["fig.png"]["source"] == "claude-science"
    assert apps["fig.png"]["tag"] == "Overture buildings"


def test_api_apps_sorts_the_merged_list_by_tag_then_name(client, workspace, store):
    _workspace_app(workspace, "zulu", tag="local")
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_alpha.png"])
    _projects_db(store, [("proj_a", "Assay")])

    apps = client.get("/api/apps").json()["apps"]
    assert [(a["tag"], a["name"]) for a in apps] == [("Assay", "alpha.png"), ("local", "zulu")]


def test_artifacts_list_even_with_no_workspace_at_all(tmp_path, monkeypatch, store):
    """The workspace's absence used to short-circuit the whole listing."""
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "no-such-workspace"))
    _artifact(store, "proj_a", "u1", ["vaaaaaaaa_fig.png"])

    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert [a["name"] for a in client.get("/api/apps").json()["apps"]] == ["fig.png"]


def test_a_broken_store_does_not_break_the_listing(client, workspace, monkeypatch):
    _workspace_app(workspace, "my-app")

    def _boom():
        raise RuntimeError("store went sideways")

    monkeypatch.setattr(claude_science, "list_apps", _boom)
    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["my-app"]
