"""Extra Fused workspaces discovered from Claude Code's project list (D208).

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


def _app(parent, name, entry="index.html"):
    """One app folder: <parent>/<name>/<entry>."""
    app = parent / name
    app.mkdir(parents=True, exist_ok=True)
    if entry:
        (app / entry).write_text("<title>Demo</title>", encoding="utf-8")
    return app


def _workspace(root, tag="local", names=("demo", "other"), entry="index.html"):
    """A Fused-shaped folder: <root>/<tag>/<name>/<entry>.

    TWO apps by default, because one is not enough to make a tag folder: the
    rule is a density test (`MIN_TAG_APPS`/`MIN_TAG_SHARE`), so a lone app-shaped
    directory reads as an ordinary folder that happens to hold a page — which is
    what a `docs/` or a `site/` in a source tree is. A workspace with exactly one
    app in it is the accepted cost, pinned by its own test below."""
    for name in names:
        _app(root / tag, name, entry)
    return root / tag / names[0]


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


def test_the_app_cap_stops_the_walk(tmp_path, config, monkeypatch, caplog):
    """The cap bounds the WORK, not just the output — raised in review, and it
    bites harder here than in the artifact store.

    Past the cap the first version kept calling `_tag_apps` for every remaining
    root AND every one of their subdirectories: a listdir plus an entry
    resolution each, across repositories this source knows nothing about, on
    every Home render — purely to count what it was discarding.

    Six roots of two apps each, capped at 3: the walk must stop inside the
    second root, so the later roots are never even considered.
    """
    monkeypatch.setattr(claude_projects, "MAX_APPS", 3)
    roots = []
    for r in range(6):
        root = tmp_path / f"root{r}"
        for name in ("one", "two"):
            _app(root / "work", f"{name}{r}")
        roots.append(root)
    config(roots)

    scanned = []
    real = claude_projects._tag_apps
    monkeypatch.setattr(claude_projects, "_tag_apps",
                        lambda f, t, s: (scanned.append(f), real(f, t, s))[1])

    with caplog.at_level("WARNING", logger="fused_render"):
        apps = claude_projects.list_apps(str(tmp_path / "ws"))

    assert len(apps) == 3
    assert "capped at 3" in caplog.text
    touched = {p for p in scanned if str(tmp_path / "root2") in p}
    assert not touched, "roots past the cap must never be scanned"


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

def test_a_folder_of_apps_inside_a_git_checkout_IS_listed(tmp_path, config):
    """The reversal, and the reason this module stopped asking about git.

    The first version skipped any root inside a repository, on the strength of
    one measurement (this repo's checkout, where the bare two-level rule finds
    7 junk "apps"). Against a real project list that rule said the opposite:
    17 roots, 14 of them checkouts, 0 apps listed. People keep folders of little
    apps inside repositories — the shape of the folder is the signal, not the
    presence of a `.git` above it.
    """
    checkout = tmp_path / "sandbox"
    (checkout / ".git").mkdir(parents=True)
    for name in ("soccer", "spectrogram", "sysdebug"):
        _app(checkout / "render", name, f"{name}.html")
    config([checkout])

    assert _names(claude_projects.list_apps(str(tmp_path / "ws"))) == [
        "soccer", "spectrogram", "sysdebug"]


def test_a_source_tree_is_refused_by_density(tmp_path, config):
    """What the git filter was really reaching for, done as a positive test.

    Modelled on this repo, where the old rule's 7 false positives came from: a
    package directory with a few app-shaped children among many that are not.
    3 of 8 is 38%, under `MIN_TAG_SHARE`, so the whole folder is refused rather
    than contributing its `app_starter`/`static`/`template_starter` lookalikes.
    """
    repo = tmp_path / "checkout"
    pkg = repo / "fused_render"
    for name in ("app_starter", "static", "template_starter"):
        _app(pkg, name)                       # app-shaped, but not apps
    for name in ("server", "shell", "templates", "windows", "app_seed"):
        (pkg / name).mkdir(parents=True)      # ordinary source dirs
    config([repo])

    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_one_app_alone_is_not_a_workspace(tmp_path, config):
    """The accepted cost of the density rule, pinned so it cannot drift.

    A single app-shaped directory is indistinguishable from a `docs/` or a
    `site/` that happens to hold an index.html, which is the commonest false
    positive there is. `MIN_TAG_APPS` is what rules it out — at the price of not
    discovering a folder that genuinely has exactly one app in it.
    """
    lonely = tmp_path / "project"
    _app(lonely / "docs", "site")
    config([lonely])

    assert claude_projects.list_apps(str(tmp_path / "ws")) == []


def test_a_root_that_is_itself_the_tag_folder_is_listed(tmp_path, config):
    """The depth that the shipped version missed entirely.

    A user's real sandbox: they open Claude Code in the folder that HOLDS the
    apps, not in its parent, so the apps are at `<root>/<name>/one.html`. The
    two-level-only rule found none of them (and, before the __pycache__ fix,
    found seven bytecode directories instead). The root's own basename becomes
    the tag.
    """
    root = tmp_path / "render"
    for name in ("local_chat", "soccer", "spectrogram"):
        _app(root, name, f"{name}.html")
    config([root])

    apps = claude_projects.list_apps(str(tmp_path / "ws"))
    assert _names(apps) == ["local_chat", "soccer", "spectrogram"]
    assert {a["tag"] for a in apps} == {"render"}


def test_the_workspace_itself_and_anything_under_it_is_left_to_its_own_source(
        tmp_path, config):
    """Otherwise every app in the real workspace would be listed twice — once by
    the workspace walk and once here."""
    ws = tmp_path / "Fused"
    _workspace(ws)
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
    claude_split — the version-stacked read-only path D206 special-cases the
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
    _workspace(other, tag="work", names=("dashboard", "sidekick"),
               entry="dashboard.html")
    config([other])

    apps = claude_projects.list_apps(str(tmp_path / "Fused"))
    assert _names(apps) == ["dashboard", "sidekick"]
    app = next(a for a in apps if a["name"] == "dashboard")
    assert app["name"] == "dashboard"
    assert app["tag"] == "work"
    assert app["path"] == str(other / "work" / "dashboard")
    assert app["entry"] == app["entry_html"] == str(
        other / "work" / "dashboard" / "dashboard.html")
    assert app["title"] == "Demo"          # read from the entry, as anywhere else
    assert app["source"] == claude_projects.SOURCE
    assert isinstance(app["updated_at"], float)


def test_only_app_shaped_siblings_are_listed_from_a_discovered_folder(
        tmp_path, config):
    """A deliberate asymmetry with the workspace source, worth stating.

    `two_level_apps` lists a folder with zero or several top-level `.html` too —
    it just reports `entry_html: None` and the card opens as a directory. That
    is right for YOUR workspace, where everything in it is yours to see. It is
    wrong for a folder we merely found: `notes/`, `data/` and every other
    ordinary subdirectory would become an entry-less card. So a discovered tag
    folder contributes only the children that are actually apps.
    """
    other = tmp_path / "Other"
    for name in ("alpha", "beta", "gamma"):
        _app(other / "work", name)
    (other / "work" / "notes").mkdir()                      # no page at all
    two = other / "work" / "twofiles"
    two.mkdir()
    (two / "a.html").write_text("<p>1</p>", encoding="utf-8")
    (two / "b.html").write_text("<p>2</p>", encoding="utf-8")  # ambiguous entry
    config([other])

    apps = claude_projects.list_apps(str(tmp_path / "ws"))
    assert _names(apps) == ["alpha", "beta", "gamma"]
    assert all(a["entry_html"] for a in apps), "every discovered app has an entry"


def test_the_same_app_reached_by_two_roots_is_one_card(tmp_path, config):
    """Project entries nest — a workspace and a folder inside it are both
    ordinary things to have run Claude Code in."""
    other = tmp_path / "Other"
    _workspace(other, tag="local")
    config([other, other / "local"])   # the tag dir is itself a listed project

    apps = claude_projects.list_apps(str(tmp_path / "ws"))
    assert _names(apps) == ["demo", "other"], \
        "a nested root must not duplicate the apps"


def test_an_unreadable_root_is_skipped_not_fatal(tmp_path, config):
    """A root that vanishes between the config read and the walk is a race, not
    an error — the listing degrades by one folder."""
    gone = tmp_path / "gone"
    gone.mkdir()
    live = tmp_path / "live"
    _workspace(live, names=("survivor", "second"))
    config([gone, live])
    gone.rmdir()

    assert _names(claude_projects.list_apps(str(tmp_path / "ws"))) == [
        "second", "survivor"]


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
    _workspace(other, tag="work", names=("report", "sidekick"),
               entry="report.html")
    config([other, workspace])   # the real workspace is listed too — and skipped

    client = TestClient(create_app(start_dir=str(tmp_path)))
    apps = client.get("/api/apps", headers={"X-Fused": "1"}).json()["apps"]

    by_name = {a["name"]: a for a in apps}
    assert sorted(by_name) == ["report", "sidekick", "wsapp"]
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


# --------------------------------------------------- the Preferences switch

@pytest.fixture()
def own_prefs(tmp_path, monkeypatch):
    """A private prefs.json for tests that WRITE one.

    conftest points FUSED_RENDER_HOME at one throwaway dir for the whole run,
    not per test — fine for the suites that only read prefs, poison for these:
    a stored `discover_claude_code: false` leaked forward and emptied the
    discovered source in every later test in the file.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


def test_a_source_switched_off_is_not_listed_and_is_not_walked(
        tmp_path, workspace, config, own_prefs, monkeypatch):
    """Preferences → App discovery (D210).

    Two assertions, and the second is the one worth having: an off source must
    cost NOTHING, not merely produce nothing. Turning discovery off is largely
    about not walking other people's folders on every Home render, so the pref
    is read before the call rather than filtering its result.
    """
    other = tmp_path / "Elsewhere"
    _workspace(other, tag="work", names=("report", "sidekick"))
    config([other])

    scanned = []
    real = claude_projects._tag_apps
    monkeypatch.setattr(claude_projects, "_tag_apps",
                        lambda f, t, s: (scanned.append(f), real(f, t, s))[1])

    client = TestClient(create_app(start_dir=str(tmp_path)))
    names = sorted(a["name"] for a in
                   client.get("/api/apps", headers={"X-Fused": "1"}).json()["apps"])
    assert names == ["report", "sidekick", "wsapp"]
    assert scanned, "the source ran while switched on"

    scanned.clear()
    assert client.put("/api/prefs", json={"discover_claude_code": False},
                      headers={"X-Fused": "1"}).status_code == 200

    names = sorted(a["name"] for a in
                   client.get("/api/apps", headers={"X-Fused": "1"}).json()["apps"])
    assert names == ["wsapp"], "the workspace is untouched; the source is gone"
    assert scanned == [], "an off source must not be walked at all"


def test_the_workspace_survives_both_sources_being_off(
        tmp_path, workspace, config, own_prefs):
    """There is deliberately no switch for the workspace itself — a toggle that
    could empty Home of the user's own work is a footgun, not a preference."""
    client = TestClient(create_app(start_dir=str(tmp_path)))
    for key in ("discover_claude_code", "discover_claude_science"):
        client.put("/api/prefs", json={key: False}, headers={"X-Fused": "1"})

    apps = client.get("/api/apps", headers={"X-Fused": "1"}).json()["apps"]
    assert [a["name"] for a in apps] == ["wsapp"]
    assert apps[0]["source"] == "workspace"
