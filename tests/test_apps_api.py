"""The apps backend (server/routers/apps.py): GET /api/apps lists the
workspace's app folders (entry = the single direct-child .html), GET
/api/apps/home hydrates recents before falling back to that listing, and POST
/api/apps/new scaffolds a folder from the app starter kit and optionally starts
a detached Claude session on its index.html.

Apps live one to three levels under the workspace (app_listing.workspace_apps),
and a tag is the first path segment — there is no registry, so these tests cover
arbitrary tag names alongside "local" (where POST /api/apps/new always lands).
Every workspace staged here puts its apps at depth 2, where the rule is the
original one: any non-hidden folder, page or no page. The tag folder itself does
not list — a page-less folder at depth 1 is a shelf of apps, not an app — so
these assertions are unchanged by the walk becoming recursive. The depth rules
are exercised directly in tests/test_app_listing.py.

The spawn is stubbed at the module seam (_start_app_session) — no test here
launches a real claude.
"""
import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import app_listing
from fused_render.server import create_app
from fused_render.server.routers import apps as apps_mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _app_dir(workspace, name, htmls=("index.html",), title=None, tag="local"):
    d = workspace / tag / name
    d.mkdir(parents=True)
    for i, h in enumerate(htmls):
        body = '<html><head><meta name="fused-app" /></head><body>hi</body></html>'
        if title is not None and i == 0:
            body = f"<html><head><meta name=\"fused-app\" /><title>{title}</title></head></html>"
        (d / h).write_text(body)
    return d


# -------------------------------------------------------------------- listing

def test_lists_only_top_level_dirs_with_entry_resolution(client, workspace):
    """Entry resolution as the cards see it, on the D269 rule: a folder with a
    top-level page HAS an entry, and only a folder with none opens as a folder.

    `many` and `indexed` are the two halves of what D269 changed. Both used to
    report `entry_html: null` as "ambiguous", which made a card of a multi-page
    app open a file listing — the outcome the owner's rule forbids.
    """
    _app_dir(workspace, "one")                                 # exactly one html
    _app_dir(workspace, "none", htmls=())                      # zero htmls
    _app_dir(workspace, "many", htmls=("b.html", "a.html"))    # first in NAME order
    _app_dir(workspace, "indexed", htmls=("a.html", "index.html"))  # first tagged wins
    (workspace / "loose.html").write_text('<html><head><meta name="fused-app" /></head></html>')     # a file, not a tag dir

    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    # `none` is absent: a page is what makes a folder an app, so a folder with no
    # html is a shelf, not an entry-less card (app_listing.workspace_apps).
    assert set(apps) == {"one", "many", "indexed"}
    assert apps["one"]["entry_html"] == str(workspace / "local" / "one" / "index.html")
    assert apps["many"]["entry_html"] == str(workspace / "local" / "many" / "a.html")
    # `index.html` has no special status (D301): among tagged pages, first in
    # name order wins.
    assert apps["indexed"]["entry_html"] == str(
        workspace / "local" / "indexed" / "a.html"
    )
    # `entry` — the file a card OPENS — is the same file, so the card and the
    # /render iframe cannot disagree about which page the folder is.
    assert apps["many"]["entry"] == apps["many"]["entry_html"]
    assert apps["one"]["path"] == str(workspace / "local" / "one")
    assert apps["one"]["tag"] == "local"


def test_listing_surfaces_a_root_preview_png_per_app(client, workspace):
    """`preview_image` reaches the cards through GET /api/apps, absolute like
    every other path in the payload — the shell serves it through /api/fs/raw,
    which takes an fs path, not a name relative to the app."""
    shot = _app_dir(workspace, "shot")
    (shot / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _app_dir(workspace, "plain")

    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert apps["shot"]["preview_image"] == str(shot / "preview.png")
    # Absent as null, never as a path that would render a broken image.
    assert apps["plain"]["preview_image"] is None


def test_tag_is_the_parent_folder_name(client, workspace):
    _app_dir(workspace, "widget", tag="examples")
    _app_dir(workspace, "widget2", tag="local")
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert apps["widget"]["tag"] == "examples"
    assert apps["widget2"]["tag"] == "local"


def test_any_top_level_folder_is_a_tag_no_registry(client, workspace):
    _app_dir(workspace, "proj", tag="whatever-i-want")
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["tag"] == "whatever-i-want"


def test_sorted_by_tag_then_name(client, workspace):
    _app_dir(workspace, "b", tag="zzz")
    _app_dir(workspace, "a", tag="aaa")
    apps = client.get("/api/apps").json()["apps"]
    assert [(a["tag"], a["name"]) for a in apps] == [("aaa", "a"), ("zzz", "b")]


def test_hidden_dirs_and_hidden_htmls_are_skipped(client, workspace):
    hidden_tag_app = workspace / ".hidden-tag" / "app"
    hidden_tag_app.mkdir(parents=True)
    (hidden_tag_app / "index.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    _app_dir(workspace, ".hidden-app")  # hidden project dir inside a real tag
    v = _app_dir(workspace, "app", htmls=("view.html",))
    (v / ".draft.html").write_text('<html><head><meta name="fused-app" /></head></html>')  # hidden: doesn't make it ambiguous

    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["app"]
    assert apps[0]["entry_html"] == str(v / "view.html")


def test_entry_match_is_non_recursive(client, workspace):
    """`app_entry` looks at DIRECT children only, so a page one level down does
    not become the parent's entry. The parent has no page and so is not a card at
    all; the nested page surfaces as its own app instead (the depth-3 rule)."""
    d = _app_dir(workspace, "app", htmls=())
    (d / "sub").mkdir()
    (d / "sub" / "index.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["sub"]
    assert apps[0]["entry_html"] == str(d / "sub" / "index.html")


def test_sorted_case_insensitively(client, workspace):
    for name in ("beta", "Alpha", "gamma"):
        _app_dir(workspace, name)
    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["Alpha", "beta", "gamma"]


def test_title_parsed_from_entry_head(client, workspace):
    _app_dir(workspace, "titled", title="My  Fancy\n App")
    _app_dir(workspace, "untitled")
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert apps["titled"]["title"] == "My Fancy App"  # whitespace collapsed
    assert apps["untitled"]["title"] is None


def test_title_beyond_first_4kb_is_null_not_an_error(client, workspace):
    d = workspace / "local" / "big"
    d.mkdir(parents=True)
    (d / "index.html").write_text(
        '<html><meta name="fused-app" /><!--' + "x" * 5000 + "--><title>late</title>")
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["title"] is None


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="chmod-based unreadable dir needs POSIX non-root")
def test_unreadable_tag_dir_is_skipped_not_fatal(client, workspace):
    _app_dir(workspace, "ok")
    locked = workspace / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        apps = client.get("/api/apps").json()["apps"]
    finally:
        os.chmod(locked, stat.S_IRWXU)
    assert [a["name"] for a in apps] == ["ok"]  # unreadable tag dir skipped, no 500


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="chmod-based unreadable dir needs POSIX non-root")
def test_unreadable_project_dir_is_skipped_not_fatal(client, workspace):
    _app_dir(workspace, "ok")
    locked = workspace / "local" / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        apps = client.get("/api/apps").json()["apps"]
    finally:
        os.chmod(locked, stat.S_IRWXU)
    assert [a["name"] for a in apps] == ["ok"]  # unreadable project dir skipped, no 500


def test_an_unreadable_project_dir_is_skipped_at_any_uid(client, workspace,
                                                         monkeypatch):
    """The same contract as above, without depending on file permissions.

    The chmod test is VACUOUS FOR ROOT — mode 0 does not stop uid 0 reading the
    directory, so it is skipped there and a developer (or a container) running
    as root gets no coverage of this path at all. That is how a regression
    shipped once: `app_entry` was called from inside `app_dict`, which swallowed
    the `OSError` and turned "skip this directory" into "list it with no entry".

    Raising from `app_entry` itself reproduces the condition deterministically,
    for every uid and on Windows too, and is what the listing actually has to
    survive.
    """
    _app_dir(workspace, "ok")
    (workspace / "local" / "locked").mkdir()

    real = app_listing.app_entry

    def refuse(path):
        if os.path.basename(path) == "locked":
            raise PermissionError(13, "Permission denied", path)
        return real(path)

    monkeypatch.setattr(app_listing, "app_entry", refuse)
    apps = client.get("/api/apps").json()["apps"]

    assert [a["name"] for a in apps] == ["ok"]


def test_missing_workspace_lists_empty(client, workspace):
    os.rmdir(workspace)
    assert client.get("/api/apps").json() == {"apps": []}


def test_entry_is_reported_alongside_entry_html(client, workspace):
    """Both keys, same file — the shell reads `entry` and needs it to be there.

    `entry` is "the file a card opens"; `entry_html` is the narrower "this entry
    is a renderable page". They coincide for a workspace app.

    A page-less folder is no longer listed at all, so the walk has no null-entry
    card to assert here; the null-under-both-keys shape remains `app_dict`'s
    contract (it accepts `entry=None`) with no listing caller today.
    """
    _app_dir(workspace, "withentry")
    (workspace / "local" / "bare").mkdir()

    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}

    assert apps["withentry"]["entry"] == apps["withentry"]["entry_html"]
    assert apps["withentry"]["entry"].endswith("index.html")
    assert "bare" not in apps


# ------------------------------------------------------------------- creation

HDRS = {"X-Fused": "1"}


def test_new_app_requires_the_fused_header(client):
    r = client.post("/api/apps/new", json={"name": "x", "prompt": ""})
    assert r.status_code == 403


def test_new_app_happy_path_no_prompt(client, workspace, monkeypatch):
    called = []
    monkeypatch.setattr(apps_mod, "_start_app_session",
                        lambda entry, prompt: called.append((entry, prompt)) or ("r-1", None))
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    dest = workspace / "local" / "demo"
    assert body["path"] == str(dest)
    assert body["entry_html"] == str(dest / "index.html")
    assert body["session_started"] is False
    assert called == []  # empty prompt: no spawn attempt at all
    assert (dest / "index.html").is_file()
    assert (dest / "CLAUDE.md").is_file()
    # the starter kit's entry is a valid single-entry app: it lists back
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["entry_html"] == body["entry_html"]
    assert apps[0]["tag"] == "local"


def test_new_app_has_no_dot_claude_and_syncs_user_skills(
    client, workspace, tmp_path, monkeypatch
):
    """D185: the app folder itself carries no .claude/; creating an app
    installs the canonical skills at Claude Code's user level instead."""
    from fused_render import user_skills

    claude_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setattr(apps_mod, "_start_app_session", lambda e, p: (None, "x"))
    client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)

    assert not (workspace / "local" / "demo" / ".claude").exists()
    for name in user_skills.SKILLS:
        skill = claude_dir / "skills" / name
        assert (skill / "SKILL.md").is_file()
        assert (skill / user_skills._MARKER).is_file()


def test_new_app_with_prompt_starts_a_session(client, workspace, monkeypatch):
    seen = {}

    def fake_start(app_dir, prompt):
        seen["target"] = app_dir
        seen["prompt"] = prompt
        return "run-42", None

    monkeypatch.setattr(apps_mod, "_start_app_session", fake_start)
    r = client.post("/api/apps/new",
                    json={"name": "demo", "prompt": "build a todo app"},
                    headers=HDRS)
    assert r.json()["session_started"] is True
    assert r.json()["session_error"] is None
    assert r.json()["run_id"] == "run-42"   # the UI can attach to the live run
    # The scaffolding session starts on the app FOLDER (claude agent),
    # so its sidecar lands where the split view lists sessions from.
    assert seen["target"] == str(workspace / "local" / "demo")
    assert seen["prompt"] == "build a todo app"


def test_spawn_failure_does_not_fail_creation_and_says_why(client, workspace, monkeypatch):
    monkeypatch.setattr(apps_mod, "_start_app_session",
                        lambda e, p: (None, "claude CLI not found"))
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": "hi"}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["session_started"] is False
    assert r.json()["run_id"] is None
    assert r.json()["session_error"] == "claude CLI not found"
    assert (workspace / "local" / "demo" / "index.html").is_file()


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "a/b", "a\\b", ".hidden", " .hidden", " a/b ", None, 7],
)
def test_bad_names_are_rejected(client, workspace, bad):
    r = client.post("/api/apps/new", json={"name": bad, "prompt": ""}, headers=HDRS)
    assert r.status_code == 400
    assert not any((workspace).iterdir())


def test_collision_is_409_for_dirs_and_files(client, workspace):
    _app_dir(workspace, "taken")
    (workspace / "local" / "afile").write_text("x")
    for name in ("taken", "afile"):
        r = client.post("/api/apps/new", json={"name": name, "prompt": ""}, headers=HDRS)
        assert r.status_code == 409, name


def test_partial_copy_is_cleaned_up(client, workspace, monkeypatch):
    def boom(src, dst, **kw):
        os.makedirs(dst)
        (workspace / "local" / "demo" / "index.html").write_text("partial")
        raise OSError("disk full")

    monkeypatch.setattr(apps_mod.shutil, "copytree", boom)
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    assert r.status_code == 400
    assert not (workspace / "local" / "demo").exists()


# ------------------------------------------------------------------ updated_at

def test_updated_at_tracks_direct_children_not_just_the_dir(client, workspace):
    """Editing a file in place doesn't move the dir's own mtime — updated_at
    must still reflect it (max over dir + direct children)."""
    d = _app_dir(workspace, "app")
    dir_mtime = os.stat(d).st_mtime
    future = dir_mtime + 1000
    os.utime(d / "index.html", (future, future))
    apps = client.get("/api/apps").json()["apps"]
    # abs=, not the default rel=1e-6: on an epoch value (~1.8e9) the relative
    # tolerance is nearly half an hour, so `approx(future)` would happily
    # accept the dir's own untouched mtime and assert nothing at all.
    assert apps[0]["updated_at"] == pytest.approx(future, abs=0.01)


def test_updated_at_is_a_float(client, workspace):
    """A float in the JSON, not a string or an int — the shell sorts on it.

    This used to stage a page-less folder, to pin that `updated_at` is filled in
    even with no entry. That folder is no longer an app (a page is what makes one,
    at any depth), so the entry-less half of the claim has no listable app left
    to carry it and is dropped.
    """
    _app_dir(workspace, "stamped")
    apps = client.get("/api/apps").json()["apps"]
    assert isinstance(apps[0]["updated_at"], float)


# ------------------------------------------------- opened_at (recency of open)

@pytest.fixture()
def recents_home(tmp_path, monkeypatch):
    """Per-test recents store — the session-wide FUSED_RENDER_HOME is shared,
    and app_recents.json written by one test must not rank apps in another."""
    home = tmp_path / "frhome"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


def test_opening_an_app_stamps_opened_at_in_the_listing(
        client, workspace, recents_home):
    """The recency the shell sorts /home and /apps by: POST recents/open
    records the open (keyed on the app's path), and GET /api/apps reports it
    as epoch seconds (updated_at's unit). An app never opened carries null."""
    _app_dir(workspace, "opened")
    _app_dir(workspace, "untouched")
    r = client.post("/api/apps/recents/open",
                    json={"path": str(workspace / "local" / "opened")},
                    headers={"X-Fused": "1"})
    assert r.json() == {"recorded": True}
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert isinstance(apps["opened"]["opened_at"], float)
    assert abs(apps["opened"]["opened_at"] - time.time()) < 60
    assert apps["untouched"]["opened_at"] is None


def test_open_records_at_every_depth_and_shelves_do_not_collide(
        client, workspace, recents_home):
    """The path key exists because (tag, name) was ambiguous: two depth-3 apps
    under different shelves of one tag share both. Each depth the walk lists
    (1-3) must record, and opening one same-named app must not stamp the other."""
    # depth 1: the folder is its own tag; depth 3: index.html under tag/shelf/app.
    (workspace / "solo").mkdir()
    (workspace / "solo" / "page.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    for shelf in ("a", "b"):
        d = workspace / "deep" / shelf / "twin"
        d.mkdir(parents=True)
        (d / "index.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    for p in ("solo", "deep/a/twin"):
        r = client.post("/api/apps/recents/open",
                        json={"path": str(workspace / p)},
                        headers={"X-Fused": "1"})
        assert r.json() == {"recorded": True}, p
    apps = {a["path"]: a for a in client.get("/api/apps").json()["apps"]}
    assert isinstance(apps[str(workspace / "solo")]["opened_at"], float)
    assert isinstance(apps[str(workspace / "deep" / "a" / "twin")]["opened_at"], float)
    assert apps[str(workspace / "deep" / "b" / "twin")]["opened_at"] is None


def test_open_outside_the_workspace_is_a_no_op(client, workspace, recents_home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = client.post("/api/apps/recents/open",
                    json={"path": str(outside)}, headers={"X-Fused": "1"})
    assert r.json() == {"recorded": False}


def test_a_malformed_recents_timestamp_never_fails_the_listing(
        client, workspace, recents_home):
    """app_recents.json is user-writable: a garbage openedAt drops that
    entry's opened_at, it must not 500 GET /api/apps."""
    _app_dir(workspace, "victim")
    recents_home.mkdir(parents=True, exist_ok=True)
    (recents_home / "app_recents.json").write_text(json.dumps({
        "entries": [
            {"path": "local/victim", "openedAt": "not-a-date"},
            {"path": "local/victim2", "openedAt": 12345},
        ]
    }))
    r = client.get("/api/apps")
    assert r.status_code == 200
    apps = {a["name"]: a for a in r.json()["apps"]}
    assert apps["victim"]["opened_at"] is None


def test_reopening_updates_opened_at_not_duplicates(
        client, workspace, recents_home):
    _app_dir(workspace, "again")
    for _ in range(2):
        client.post("/api/apps/recents/open",
                    json={"path": str(workspace / "local" / "again")},
                    headers={"X-Fused": "1"})
    entries = client.get("/api/apps/recents").json()["entries"]
    assert [e["path"] for e in entries] == ["local/again"]
    apps = client.get("/api/apps").json()["apps"]
    assert isinstance(apps[0]["opened_at"], float)


# ----------------------------------------- Home's recent-first bounded listing

def test_home_hydrates_recents_without_scanning_the_workspace(
        client, workspace, recents_home, tmp_path, monkeypatch):
    """A returning Home visit pays for explicit recent paths only. Workspace
    and registered recents share one newest-first row, and filling that row is
    the gate that keeps the exhaustive discovery walk unreachable."""
    local = _app_dir(workspace, "local-recent")
    external = tmp_path / "outside" / "linked-recent"
    external.mkdir(parents=True)
    (external / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head></html>'
    )
    assert client.post(
        "/api/apps/recents/open", json={"path": str(local)},
        headers={"X-Fused": "1"},
    ).json() == {"recorded": True}
    time.sleep(0.002)  # make the cross-store order observable on all filesystems
    assert client.post(
        "/api/apps/recents/open", json={"path": str(external)},
        headers={"X-Fused": "1"},
    ).json() == {"recorded": True}

    def unexpected_scan():
        raise AssertionError("warm Home must not scan the workspace")

    def unexpected_mtime_sweep(_path):
        raise AssertionError("opened recents do not need fallback mtimes")

    monkeypatch.setattr(apps_mod, "_workspace_apps", unexpected_scan)
    monkeypatch.setattr(app_listing, "dir_updated_at", unexpected_mtime_sweep)
    r = client.get("/api/apps/home", params={"limit": 2})
    assert r.status_code == 200
    assert [a["path"] for a in r.json()["apps"]] == [str(external), str(local)]


def test_home_registry_limit_counts_only_valid_open_timestamps(
        client, workspace, recents_home, tmp_path, monkeypatch):
    """A corrupt linked-app timestamp is not one of Home's recent slots.

    The registry is user-writable. A valid app after the corrupt row must still
    fill the requested row, keeping the workspace discovery fallback cold.
    """
    from fused_render import registered_apps

    corrupt = tmp_path / "outside" / "corrupt-open"
    valid = tmp_path / "outside" / "valid-open"
    for folder in (corrupt, valid):
        folder.mkdir(parents=True)
        (folder / "index.html").write_text(
            '<html><head><meta name="fused-app" /></head></html>'
        )
    registered_apps.write_entries([
        {"path": str(corrupt), "openedAt": "not-a-date"},
        {"path": str(valid), "openedAt": "2026-08-18T12:00:00+00:00"},
    ])

    def unexpected_scan():
        raise AssertionError("the later valid linked recent fills Home's row")

    monkeypatch.setattr(apps_mod, "_workspace_apps", unexpected_scan)
    apps = client.get("/api/apps/home", params={"limit": 1}).json()["apps"]
    assert [app["path"] for app in apps] == [str(valid)]


def test_home_falls_back_to_discovery_and_keeps_showcase(
        client, workspace, recents_home, monkeypatch):
    """An incomplete recents row triggers the existing walk. Showcase is an
    ordinary workspace tag, so an unopened showcase app remains a fallback
    card rather than disappearing behind the optimization."""
    recent = _app_dir(workspace, "recent")
    showcase = _app_dir(workspace, "starter", tag="showcase")
    assert client.post(
        "/api/apps/recents/open", json={"path": str(recent)},
        headers={"X-Fused": "1"},
    ).json() == {"recorded": True}

    real_scan = apps_mod._workspace_apps
    calls = 0

    def counted_scan():
        nonlocal calls
        calls += 1
        return real_scan()

    monkeypatch.setattr(apps_mod, "_workspace_apps", counted_scan)
    apps = client.get("/api/apps/home", params={"limit": 2}).json()["apps"]
    assert calls == 1
    assert [a["path"] for a in apps] == [str(recent), str(showcase)]


def test_home_falls_back_when_stale_recents_do_not_fill_the_row(
        client, workspace, recents_home):
    """Deleted recent folders are skipped, then discovery repairs the row."""
    discovered = _app_dir(workspace, "discovered")
    recents_home.mkdir(parents=True, exist_ok=True)
    (recents_home / "app_recents.json").write_text(json.dumps({
        "entries": [{
            "path": "local/deleted",
            "openedAt": "2026-08-18T12:00:00+00:00",
        }]
    }))
    apps = client.get("/api/apps/home", params={"limit": 1}).json()["apps"]
    assert [a["path"] for a in apps] == [str(discovered)]


# ---------------------------------------------------- the fork-safe spawn seam

def test_spawn_runs_agent_start_in_a_helper_subprocess_not_in_process(
        tmp_path, workspace, monkeypatch):
    """The live-bug regression: calling agent._start inside the server process
    fork()s with libproj resident and SIGSEGVs the child before exec (PROJ's
    pthread_atfork handler; same crash test_worker_forksafe.py pins for the
    executor). The spawn must therefore happen via a helper subprocess — and
    that helper's own Popen must stay on the posix_spawn path (close_fds=False,
    no cwd, no start_new_session) with the prompt on stdin, not argv.

    The spawn itself now lives in fused_render/claude_spawn.py — shared with
    scheduled messages, which need the identical discipline — so the subprocess
    stub goes there. What stays this module's own is the policy asserted below:
    permission mode "auto", and a fresh session."""
    entry = workspace / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text('<html><head><meta name="fused-app" /></head></html>')
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return type("R", (), {"returncode": 0,
                              "stdout": '{"run_id": "r-1"}', "stderr": ""})()

    monkeypatch.setattr(apps_mod.claude_spawn.subprocess, "run", fake_run)
    monkeypatch.setattr(apps_mod, "_claude_agent", lambda: None)
    started_threads = []
    monkeypatch.setattr(apps_mod.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started_threads.append(kw)})())

    run_id, err = apps_mod._start_app_session(str(entry), "secret prompt $(boom)")
    assert (run_id, err) == ("r-1", None)

    # a real python -c helper, not claude itself, and prompt over stdin only
    assert seen["cmd"][0] == apps_mod.claude_spawn.sys.executable
    assert "secret prompt" not in " ".join(seen["cmd"])
    import json as jsonlib
    req = jsonlib.loads(seen["kwargs"]["input"])
    assert req["message"] == "secret prompt $(boom)"
    assert req["file"] == str(entry)
    # unattended: nobody polls `decide` for a session started from a POST, so
    # the strict default mode would park the first tool call until the
    # permission timeout denied it — boilerplate, silently.
    assert req["permission_mode"] == "auto"
    # an app is being scaffolded: there is no prior conversation to resume
    assert req["session_id"] == ""
    # posix_spawn preconditions on the helper spawn (the crash was fork+exec)
    assert seen["kwargs"]["close_fds"] is False
    assert "cwd" not in seen["kwargs"]
    assert "start_new_session" not in seen["kwargs"]
    # text=True alone decodes stdout/stderr with locale.getpreferredencoding —
    # ASCII on a GUI-launched server with no LANG/LC_ALL — so the first em dash
    # or curly quote in the helper's JSON result (echoed prompt, app name/title,
    # model output) raised UnicodeDecodeError instead of returning a run_id.
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["errors"] == "replace"
    assert started_threads  # the sidecar-recording poll thread was kicked off


def test_spawn_helper_failure_reports_why(tmp_path, workspace, monkeypatch):
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "",
                              "stderr": "boom\nFileNotFoundError: claude"})()

    monkeypatch.setattr(apps_mod.claude_spawn.subprocess, "run", fake_run)
    run_id, err = apps_mod._start_app_session("/x/index.html", "hi")
    assert run_id is None
    assert "FileNotFoundError: claude" in err


def test_a_missing_claude_cli_reports_the_fix_not_a_traceback_tail(
        tmp_path, workspace, monkeypatch):
    """_claude_bin's FileNotFoundError is a multi-line message, so the helper's
    last-stderr-line report used to surface its "Also looked in: ..." tail —
    accurate, but with the actual instruction cut off. The mapped message says
    what to do and where the guide is."""
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr":
                              "Traceback (most recent call last):\n"
                              "FileNotFoundError: claude CLI not found — "
                              "install Claude Code, put `claude` on the PATH\n"
                              "of the environment that launched fused-render. "
                              "Also looked in: /opt/foo"})()

    monkeypatch.setattr(apps_mod.claude_spawn.subprocess, "run", fake_run)
    run_id, err = apps_mod._start_app_session("/x/index.html", "hi")
    assert run_id is None
    assert "Claude Code isn't installed" in err
    assert "render.fused.io/#troubleshooting-notfound" in err


@pytest.mark.skipif(os.name == "nt", reason="/bin/sh stub claude is POSIX-only")
def test_spawn_really_delivers_the_prompt_to_the_claude_process(
        tmp_path, workspace, monkeypatch):
    """The regression the mocked tests could never catch.

    Everything below _start_app_session is real here — the helper subprocess,
    agent._start, the detached spawn — with only `claude` itself replaced by a
    shell stub that records the argv and stdin it was handed. The live bug was
    precisely that this whole path produced nothing: the helper's absence meant
    the fork() SIGSEGV'd before exec, so claude never ran at all and the app
    stayed boilerplate. Asserting the stub RAN and SAW the prompt is what pins
    that; a stub that is never executed writes no files and fails here."""
    entry = workspace / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text('<html><head><meta name="fused-app" /></head></html>')
    argv_log, stdin_log = tmp_path / "argv.txt", tmp_path / "stdin.txt"
    stub = tmp_path / "claude"
    stub.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{argv_log}"\ncat > "{stdin_log}"\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(stub))

    run_id, err = apps_mod._start_app_session(str(entry), "hello from the test")
    assert err is None and run_id, err

    # the spawn is detached, so wait for the stub to finish writing
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not stdin_log.exists():
        time.sleep(0.1)
    assert stdin_log.exists(), "claude was never executed (the live bug)"

    argv = argv_log.read_text().splitlines()
    # the prompt reached the process, over stdin as one stream-json user line
    assert "hello from the test" not in "\n".join(argv)   # never in argv
    row = json.loads(stdin_log.read_text())
    assert row["message"]["content"][0]["text"] == "hello from the test"
    # ...and it can act on it unattended: prompt-tool wired AND a mode that
    # doesn't park every tool call on a card nobody is watching for.
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--permission-prompt-tool") + 1].startswith("mcp__")
    assert argv[argv.index("--input-format") + 1] == "stream-json"


# --------------------------------------------- the stdin path through agent.py

def test_agent_start_stdin_mode_keeps_message_out_of_argv(tmp_path, monkeypatch):
    """The spawn the apps API relies on: message_via_stdin writes the prompt as
    a stream-json user line in the run dir and wires it as the process stdin —
    the prompt string must appear nowhere in argv."""
    import importlib.util
    import json as jsonlib

    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_stdin", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    target = tmp_path / "index.html"
    target.write_text('<html><head><meta name="fused-app" /></head></html>')
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stdin"] = kwargs["stdin"]
        return type("P", (), {"pid": 4242})()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    secret = "build me a $(rm -rf /) app"
    run_id = agent._start(str(target), secret, "", "", "",
                          message_via_stdin=True)["run_id"]

    assert secret not in " ".join(seen["cmd"])
    assert seen["cmd"][seen["cmd"].index("--input-format") + 1] == "stream-json"
    # stdin is the run-dir file holding exactly one stream-json user message
    stdin_file = os.path.join(agent.RUNS, run_id, "stdin.jsonl")
    assert seen["stdin"].name == stdin_file
    row = jsonlib.loads(open(stdin_file, encoding="utf-8").read())
    assert row["message"]["content"][0]["text"] == secret


def test_agent_start_default_still_passes_message_in_argv(tmp_path, monkeypatch):
    """The template path is unchanged: no stdin file, message after -p."""
    import importlib.util

    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_argv", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    target = tmp_path / "index.html"
    target.write_text('<html><head><meta name="fused-app" /></head></html>')
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stdin"] = kwargs["stdin"]
        return type("P", (), {"pid": 4242})()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    run_id = agent._start(str(target), "hello", "", "", "")["run_id"]
    assert seen["cmd"][seen["cmd"].index("-p") + 1] == "hello"
    assert seen["stdin"] == agent.subprocess.DEVNULL
    assert not os.path.exists(os.path.join(agent.RUNS, run_id, "stdin.jsonl"))


# --------------------------- landing the creator in the running claude session

# Creating an app with a prompt starts a session the user never sees unless the
# post-create navigation opens the app FOLDER's chat attached to that run. Three
# sources have to agree for that to work, and none of them can see the other two:
# HomeHero.tsx builds the URL, registry.json makes "claude" a selectable
# mode for the `/` key, and the chat template's boot re-attaches from the `run`
# param. These tests pin the three ends of that contract.

def _repo_text(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_home_navigates_into_the_claude_chat_for_the_started_run():
    home = _repo_text("frontend", "src", "apps", "builder", "HomeHero.tsx")
    # Folder-first: the scaffolding session runs via the claude agent on
    # the app FOLDER, so the re-attach must land there (same runs dir, same
    # .claude-split.json sidecar) rather than on the entry html.
    assert '_mode: "claude"' in home, "post-create nav must select the claude mode"
    assert "claudeChatUrl(res.path" in home, "…on the app folder, not entry_html"
    assert "run: runId" in home, "…and attach to the run the POST just started"
    # the run_id is what gates it: no session (no prompt) -> the default view
    assert "if (res.run_id) navigateUrl(claudeChatUrl(" in home


def test_claude_is_the_selectable_chat_mode_for_html():
    """D235 moved the file-side chat to the split view and deleted the plain chat
    template outright, so `claude` is the only chat a page — or anything else —
    can offer. A target must offer exactly one, or the switcher shows two chats
    that differ only in features."""
    registry = json.loads(_repo_text("fused_render", "templates", "registry.json"))
    assert registry[".html"].count("claude") == 1
    assert registry["/"].count("claude") == 1


def test_claude_template_boots_into_chat_from_a_bare_run_param():
    """The page must resume a run it did not start itself: its boot reads the
    `run` param, enters chat, and polls — no session_id needed (the sidecar
    entry lands seconds later, once claude reports its id).

    Retargeted from the deleted plain chat template to the split view (which now
    carries the `claude` name): the POST always spawned through the chat agent on
    the FOLDER, so this was already pinning the wrong page's boot."""
    page = _repo_text("fused_render", "templates", "claude", "template.html")
    assert 'fused.params.get("run")' in page
    # The CALL, not its argument list: `resumeRun` grew an options object
    # (`{ retryUnknown: true }`, #610) and this test is about the boot resuming
    # the run at all, so it must not break every time an opt is added.
    assert "await resumeRun(run_id" in page


def test_run_param_survives_the_shell_runtime():
    """`run` must be an ordinary view param: the runtime hides every
    `_`-prefixed name from templates (isReserved), so a reserved-looking name
    would read back as undefined and the chat would boot to its home card."""
    assert not "run".startswith("_")
    runtime = _repo_text("fused_render", "static", "runtime.js")
    assert 'if (key.startsWith("_")) return true;' in runtime


def test_poll_serves_a_run_started_by_the_server(tmp_path, workspace, monkeypatch):
    """The crux: agent._poll is the page's re-attach path, and it must answer
    for a run the SERVER spawned (the POST) exactly as for one the page did —
    same runs dir, same meta, so the page replays the user's prompt and streams
    the reply. Pinned against a real spawn (stub claude) rather than a mock."""
    if os.name == "nt":
        pytest.skip("/bin/sh stub claude is POSIX-only")
    entry = workspace / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text('<html><head><meta name="fused-app" /></head></html>')
    stub = tmp_path / "claude"
    # the stream-json rows poll parses: an init row carrying the session id, a
    # streamed text delta, and the terminating result row
    stub.write_text(
        '#!/bin/sh\ncat > /dev/null\n'
        'printf \'%s\\n\' \'{"type":"system","subtype":"init","session_id":"sid-live"}\'\n'
        'printf \'%s\\n\' \'{"type":"stream_event","event":{"type":'
        '"content_block_delta","delta":{"type":"text_delta","text":"on it"}}}\'\n'
        'printf \'%s\\n\' \'{"type":"result","subtype":"success",'
        '"session_id":"sid-live","result":"on it"}\'\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(stub))

    run_id, err = apps_mod._start_app_session(str(entry), "make it red")
    assert err is None and run_id, err

    agent = apps_mod._claude_agent()
    deadline = time.monotonic() + 30
    data = {}
    while time.monotonic() < deadline:
        data = agent._poll(run_id)
        if data.get("done"):
            break
        time.sleep(0.2)
    assert data.get("done"), data
    # the page renders `message` as the user turn and `text` as the reply
    assert data["message"] == "make it red"
    assert "on it" in (data.get("text") or "")
    assert data.get("session_id") == "sid-live"
    # ...and the session lists in the entry file's sidecar, so a later visit
    # without a `run` param still finds the conversation
    assert agent._sessions(str(entry))["sessions"][0]["id"] == "sid-live"


# --------------------------------------------- opens recorded by GET /render
#
# D301: rendering a page that carries the fused-app marker IS the open — the
# server records recency (workspace apps) or registers the folder (/apps hub,
# external folders) right in GET /render. No client post is involved; the shell
# stopped sending one.


def _opened_at(client, name):
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    return apps.get(name, {}).get("opened_at")


def test_rendering_a_workspace_app_records_its_open(client, workspace, recents_home):
    d = _app_dir(workspace, "sine")
    assert _opened_at(client, "sine") is None
    r = client.get("/render", params={"path": str(d / "index.html")})
    assert r.status_code == 200
    assert _opened_at(client, "sine") is not None


def test_a_preview_render_records_nothing(client, workspace, recents_home):
    # `_preview=1` is the card/preview iframes saying "thumbnail, not an open" —
    # without it every visit to the /apps grid would mark every app just-opened.
    d = _app_dir(workspace, "sine")
    r = client.get("/render",
                   params={"path": str(d / "index.html"), "_preview": "1"})
    assert r.status_code == 200
    assert _opened_at(client, "sine") is None


def test_a_render_referred_by_a_preview_records_nothing(client, workspace, recents_home):
    # A previewed page may itself iframe another app's /render URL directly —
    # its author never wrote _preview=1, but the same-origin Referer carries
    # the parent's stamp, so the nested render is a thumbnail too. Without
    # this, previewing a page that embeds other apps re-records THOSE apps.
    d = _app_dir(workspace, "sine")
    r = client.get(
        "/render",
        params={"path": str(d / "index.html")},
        headers={"Referer": "http://x/render?path=%2Fw%2Ftutorial.html&_preview=1"},
    )
    assert r.status_code == 200
    assert _opened_at(client, "sine") is None
    # An ordinary referer (a real open navigated from anywhere) still records.
    r = client.get(
        "/render",
        params={"path": str(d / "index.html")},
        headers={"Referer": "http://x/explorer/view/w/sine"},
    )
    assert r.status_code == 200
    assert _opened_at(client, "sine") is not None


def test_rendering_an_unmarked_page_records_nothing(client, workspace, tmp_path, recents_home):
    # Templates and plain html render through /render too; no marker, no record.
    p = tmp_path / "plain.html"
    p.write_text("<html><body>hi</body></html>")
    r = client.get("/render", params={"path": str(p)})
    assert r.status_code == 200
    assert client.get("/api/apps").json()["apps"] == []
    from fused_render import registered_apps
    assert registered_apps.read_entries() == []


def test_app_entry_endpoint_answers_by_the_marker_rule(client, workspace, tmp_path):
    # The explorer's "Open app" button asks the server instead of re-deriving
    # the rule from filenames (D301) — any folder may be asked, workspace or not.
    d = _app_dir(workspace, "sine", htmls=("main.html",))
    r = client.get("/api/apps/entry", params={"path": str(d)})
    assert r.json() == {"entry": str(d / "main.html")}
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "index.html").write_text("<html></html>")  # untagged: no entry
    assert client.get("/api/apps/entry",
                      params={"path": str(plain)}).json() == {"entry": None}
    assert client.get("/api/apps/entry",
                      params={"path": "relative/nope"}).json() == {"entry": None}
    assert client.get("/api/apps/entry",
                      params={"path": str(tmp_path / "gone")}).json() == {"entry": None}


def test_rendering_an_external_marked_page_registers_the_folder(
        client, tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    ext = tmp_path / "elsewhere" / "myapp"
    ext.mkdir(parents=True)
    (ext / "main.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>x</body></html>')
    r = client.get("/render", params={"path": str(ext / "main.html")})
    assert r.status_code == 200
    from fused_render import registered_apps
    (entry,) = registered_apps.read_entries()
    assert entry["path"] == str(ext)
    # ...and the folder now lists on the hub under the reserved tag.
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert apps["myapp"]["tag"] == registered_apps.REGISTERED_TAG
