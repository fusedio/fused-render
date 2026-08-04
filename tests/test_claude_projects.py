"""Extra Fused workspaces discovered from Claude Code's project list (D207).

`~/.claude.json` carries a `projects` object keyed by absolute path — every
directory Claude Code has been run in. Most of those are source checkouts, and
the two-level app rule finds junk in a checkout, so the load-bearing behaviour
here is what gets REFUSED: anything inside a git repository, anything at or
under the workspace already being listed, and anything the config file cannot
be trusted to say.

The fixtures build a real `.claude.json` and real folders rather than mocking
the reads: the failure this guards against is a listing that depends on what is
actually on the developer's disk.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_projects, claude_science
from fused_render.server import create_app


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """Write a `.claude.json` with the given roots and point the module at it."""
    path = tmp_path / "claude.json"
    monkeypatch.setenv(claude_projects.CONFIG_ENV, str(path))

    def write(roots, extra=None):
        data = {"projects": {str(r): {"hasTrustDialogAccepted": True} for r in roots}}
        if extra is not None:
            data = extra
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return write


def _workspace(root, tag="local", name="demo", entry="index.html"):
    """A Fused-shaped folder: <root>/<tag>/<name>/<entry>. No .git at the root —
    that is what makes it a workspace rather than a checkout (init_repo runs per
    app dir, never on the workspace)."""
    app = root / tag / name
    app.mkdir(parents=True)
    if entry:
        (app / entry).write_text("<title>Demo</title>", encoding="utf-8")
    return app


def _names(apps):
    return sorted(a["name"] for a in apps)


# ------------------------------------------------------- locating the config

@pytest.fixture()
def unset_override(tmp_path, monkeypatch):
    """Drop the test override so the DEFAULT resolution runs.

    Everything else in this suite (and conftest, for the whole run) points
    CONFIG_ENV at a file — which means the code path every real machine
    actually takes is the one path a test never exercises unless it says so
    here. `HOME` is redirected too, so `~` resolves inside tmp_path.
    """
    monkeypatch.delenv(claude_projects.CONFIG_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1) if p.startswith("~") else p)
    return tmp_path


def test_the_default_location_is_the_home_dir_sibling(unset_override):
    home = unset_override
    assert claude_projects.config_path() is None, "nothing there yet"

    (home / ".claude.json").write_text('{"projects": {}}', encoding="utf-8")
    assert claude_projects.config_path() == str(home / ".claude.json")


def test_a_relocated_config_dir_is_preferred_when_it_holds_the_file(
        unset_override, monkeypatch):
    """`CLAUDE_CONFIG_DIR` moves the config *directory*, and some installs put
    the JSON inside it rather than beside it — so both are tried, in that
    order. Without this the override would silently read the wrong machine's
    project list."""
    home = unset_override
    relocated = home / "elsewhere"
    relocated.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))

    # Neither exists yet.
    assert claude_projects.config_path() is None

    # The home sibling alone is still found.
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    assert claude_projects.config_path() == str(home / ".claude.json")

    # ...but the relocated one wins once it exists.
    (relocated / ".claude.json").write_text("{}", encoding="utf-8")
    assert claude_projects.config_path() == str(relocated / ".claude.json")


def test_an_override_is_returned_even_when_it_does_not_exist(tmp_path, monkeypatch):
    """The override is an explicit instruction, not a candidate: pointing it at
    a path that does not exist is how a run turns the source OFF, and the read
    handles the absence. (conftest relies on exactly this.)"""
    absent = tmp_path / "nope.json"
    monkeypatch.setenv(claude_projects.CONFIG_ENV, str(absent))
    assert claude_projects.config_path() == str(absent)
    assert claude_projects.project_roots() == []


# --------------------------------------------------------------- reading roots

def test_roots_come_from_the_projects_object_keyed_by_absolute_path(tmp_path, config):
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    config([a, b])
    assert claude_projects.project_roots() == [str(a), str(b)]


def test_a_path_that_no_longer_exists_is_dropped(tmp_path, config):
    """The list accumulates and never prunes, so a moved or deleted checkout is
    normal — not a reason to report anything."""
    live = tmp_path / "live"
    live.mkdir()
    config([live, tmp_path / "deleted", tmp_path / "also-gone"])
    assert claude_projects.project_roots() == [str(live)]


def test_a_file_masquerading_as_a_project_is_dropped(tmp_path, config):
    f = tmp_path / "notadir"
    f.write_text("x", encoding="utf-8")
    config([f])
    assert claude_projects.project_roots() == []


@pytest.mark.parametrize("body", [
    '{"projects": {"',           # torn mid-write — the expected failure
    "",                           # zero-length, caught the same instant
    "null",
    '{"projects": []}',          # right key, wrong type
    '{"projects": "nope"}',
    '["not", "an", "object"]',
])
def test_an_unusable_config_reads_as_no_roots(tmp_path, monkeypatch, body):
    """The file is Claude Code's live config, rewritten as the user works, so a
    read CAN land mid-write. Every shape of unusable degrades to "no extra
    roots" — this feature adds to a listing, it must never break one."""
    path = tmp_path / "claude.json"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv(claude_projects.CONFIG_ENV, str(path))
    assert claude_projects.project_roots() == []


def test_an_absent_config_reads_as_no_roots(tmp_path, monkeypatch):
    monkeypatch.setenv(claude_projects.CONFIG_ENV, str(tmp_path / "nope.json"))
    assert claude_projects.project_roots() == []
    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_the_root_cap_is_logged_not_silent(tmp_path, config, monkeypatch, caplog):
    monkeypatch.setattr(claude_projects, "MAX_ROOTS", 3)
    roots = []
    for i in range(6):
        d = tmp_path / f"r{i}"
        d.mkdir()
        roots.append(d)
    config(roots)
    with caplog.at_level("WARNING", logger="fused_render"):
        assert len(claude_projects.project_roots()) == 3
    assert "capped" in caplog.text


# ------------------------------------------------------------ what is refused

def test_a_git_checkout_is_skipped_entirely(tmp_path, config):
    """The filter this module turns on.

    Measured on fused-render's own checkout, the two-level rule reports 7
    "apps": 3 internal (`app_starter`, `static`, `template_starter`) and 4
    `examples_seed/*` duplicates of what the user already has. A workspace has
    no repo at its root; a checkout does.
    """
    checkout = tmp_path / "repo"
    _workspace(checkout, tag="examples_seed", name="sine", entry="sine.html")
    (checkout / ".git").mkdir()
    config([checkout])

    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_a_subdirectory_of_a_checkout_is_skipped_too(tmp_path, config):
    """Running Claude Code in `~/repo/service` is ordinary, and the project list
    records that subdirectory — so the check has to look UP, not just at the
    root itself."""
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    inner = checkout / "service"
    _workspace(inner)
    config([inner])

    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_a_git_worktree_pointer_file_counts_as_a_repo(tmp_path, config):
    """A worktree or submodule records `.git` as a FILE pointing elsewhere. It
    is still inside a repository, and `isdir` alone would have missed it."""
    checkout = tmp_path / "worktree"
    _workspace(checkout)
    (checkout / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n",
                                   encoding="utf-8")
    config([checkout])

    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_the_workspace_itself_and_anything_under_it_is_left_to_its_own_source(
        tmp_path, config):
    """Otherwise every app in the real workspace would be listed twice — once by
    the workspace walk and once here."""
    ws = tmp_path / "Fused"
    _workspace(ws, name="mine")
    inner = ws / "local"          # a tag dir inside the workspace
    config([ws, inner])

    assert claude_projects.list_apps(str(ws)) == []


def test_a_hidden_directory_is_not_a_workspace(tmp_path, config):
    hidden = tmp_path / ".cache"
    _workspace(hidden)
    config([hidden])
    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_a_root_inside_the_claude_science_store_is_refused(tmp_path, config,
                                                           monkeypatch):
    """Found in review. The store's own dir is hidden and so already refused,
    but a project root INSIDE it is not — `.../orgs/<org>/artifacts` has an
    ordinary basename, and the two-level rule reads it as
    `<project-id>/<artifact-uuid>/`.

    The artifact folder below has exactly one `.html` version, which is what
    makes this bite: `entry_html` gets set, so the card would open via
    claude_split — the version-stacked read-only path D205 special-cases the
    claude-science source to avoid. It is also a duplicate of a card that
    source already produced.
    """
    store = tmp_path / ".claude-science"
    monkeypatch.setenv(claude_science.DIR_ENV, str(store))
    artifacts = store / "orgs" / "26f4" / "artifacts"
    artifact = artifacts / "proj_a1b2" / "cd5e48e0-1111"
    artifact.mkdir(parents=True)
    (artifact / "v6f4b965a_report.html").write_text("<title>R</title>", encoding="utf-8")

    # Every level a user could plausibly have run Claude Code in.
    config([store, store / "orgs", store / "orgs" / "26f4", artifacts])

    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


# ------------------------------------------------------------- what is found

def test_a_second_workspace_is_listed_with_the_workspace_rule(tmp_path, config):
    """The whole point: a Fused-shaped folder somewhere else, reported with the
    same shape as a workspace app so the shell needs no special case."""
    other = tmp_path / "OnADrive"
    _workspace(other, tag="work", name="dashboard", entry="dashboard.html")
    config([other])

    apps = claude_projects.list_apps(str(tmp_path / "Fused"))
    assert len(apps) == 1
    app = apps[0]
    assert app["name"] == "dashboard"
    assert app["tag"] == "work"
    assert app["path"] == str(other / "work" / "dashboard")
    assert app["entry"] == app["entry_html"] == str(
        other / "work" / "dashboard" / "dashboard.html")
    assert app["title"] == "Demo"          # read from the entry, as anywhere else
    assert app["source"] == claude_projects.SOURCE
    assert isinstance(app["updated_at"], float)


def test_an_ambiguous_folder_still_lists_but_opens_as_a_directory(tmp_path, config):
    """Zero or several top-level .html is the workspace rule's "no entry" case,
    and it must behave identically here."""
    other = tmp_path / "Other"
    app = _workspace(other, name="twofiles")
    (app / "second.html").write_text("<p>2</p>", encoding="utf-8")
    _workspace(other, tag="local", name="empty", entry=None)
    config([other])

    apps = {a["name"]: a for a in claude_projects.list_apps(str(tmp_path / "ws"))}
    assert _names(apps.values()) == ["empty", "twofiles"]
    assert apps["twofiles"]["entry_html"] is None
    assert apps["empty"]["entry_html"] is None


def test_the_same_app_reached_by_two_roots_is_one_card(tmp_path, config):
    """Project entries nest — a workspace and a folder inside it are both
    ordinary things to have run Claude Code in."""
    other = tmp_path / "Other"
    _workspace(other, tag="local", name="demo")
    config([other, other / "local"])   # the tag dir is itself a listed project

    apps = claude_projects.list_apps(str(tmp_path / "ws"))
    assert _names(apps) == ["demo"], "a nested root must not duplicate the app"


def test_an_unreadable_root_is_skipped_not_fatal(tmp_path, config):
    """A root that vanishes between the config read and the walk is a race, not
    an error — the listing degrades by one folder."""
    gone = tmp_path / "gone"
    gone.mkdir()
    live = tmp_path / "live"
    _workspace(live, name="survivor")
    config([gone, live])
    gone.rmdir()

    assert _names(claude_projects.list_apps(str(tmp_path / "ws"))) == ["survivor"]


# ------------------------------------------------------------- through the API

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    (fdir / "local" / "wsapp").mkdir(parents=True)
    (fdir / "local" / "wsapp" / "index.html").write_text("<p>ws</p>", encoding="utf-8")
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


def test_api_apps_merges_the_discovered_workspace_with_the_real_one(
        tmp_path, workspace, config):
    other = tmp_path / "Elsewhere"
    _workspace(other, tag="work", name="report", entry="report.html")
    config([other, workspace])   # the real workspace is listed too — and skipped

    client = TestClient(create_app(start_dir=str(tmp_path)))
    apps = client.get("/api/apps", headers={"X-Fused": "1"}).json()["apps"]

    by_name = {a["name"]: a for a in apps}
    assert sorted(by_name) == ["report", "wsapp"]
    assert by_name["wsapp"]["source"] == "workspace"
    assert by_name["report"]["source"] == "claude-code"
    # Sorted as one list, by (tag, name) — the sources are merged, not appended.
    assert [a["tag"] for a in apps] == sorted(a["tag"] for a in apps)


def test_api_apps_survives_a_broken_config(tmp_path, workspace, monkeypatch):
    """A source that blows up must cost its own apps, never the page."""
    monkeypatch.setattr(claude_projects, "list_apps",
                        lambda _root: (_ for _ in ()).throw(RuntimeError("boom")))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.get("/api/apps", headers={"X-Fused": "1"})

    assert r.status_code == 200
    assert _names(r.json()["apps"]) == ["wsapp"]


def test_the_suite_never_reads_the_developers_real_config():
    """conftest points CONFIG_ENV at a path that does not exist. Without it the
    apps tests would list whatever Fused-shaped folders the person running them
    happens to have — green on one laptop, red on another."""
    configured = os.environ.get(claude_projects.CONFIG_ENV)
    assert configured, "conftest must redirect the Claude Code config"
    assert not os.path.exists(configured)
    assert claude_projects.config_path() == os.path.abspath(configured)
