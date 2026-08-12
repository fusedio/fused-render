"""Linked apps (fused_render/linked_apps.py + its routes in
server/routers/apps.py): folders anywhere on disk registered as apps under the
virtual "linked" tag, via ~/.fused-render/linked_apps.json — never a symlink
in the workspace.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import linked_apps
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The registry lives in the shell home; the conftest default is one shared
    # dir for the whole session, which would leak links across tests. The env
    # export (write_entries) mutates os.environ — seed it through monkeypatch
    # so the mutation is rolled back per test.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_LINKED_APPS", "")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    # What the template gates read (appenv.workspace_dir prefers it): another
    # test's create_app may have exported a stale value into this process.
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _folder(tmp_path, name, htmls=("index.html",), title=None):
    d = tmp_path / "elsewhere" / name
    d.mkdir(parents=True)
    for i, h in enumerate(htmls):
        body = "<html><body>hi</body></html>"
        if title is not None and i == 0:
            body = f"<html><head><title>{title}</title></head></html>"
        (d / h).write_text(body)
    return d


HDRS = {"X-Fused": "1"}


# ------------------------------------------------------------------- linking


def test_link_requires_the_fused_header(client, tmp_path):
    r = client.post("/api/apps/link", json={"path": str(tmp_path)})
    assert r.status_code == 403


def test_link_then_listed_under_the_linked_tag(client, tmp_path):
    d = _folder(tmp_path, "notes", title="My Notes")
    r = client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert r.status_code == 200
    app = r.json()["app"]
    assert app["tag"] == linked_apps.LINKED_TAG
    assert app["name"] == "notes"
    assert app["entry"] == app["entry_html"] == str(d / "index.html")
    assert app["title"] == "My Notes"

    listed = client.get("/api/apps").json()["apps"]
    assert [(a["tag"], a["name"]) for a in listed] == [("linked", "notes")]


def test_link_with_explicit_name(client, tmp_path):
    d = _folder(tmp_path, "notes")
    r = client.post(
        "/api/apps/link", json={"path": str(d), "name": "renamed"}, headers=HDRS
    )
    assert r.status_code == 200
    assert r.json()["app"]["name"] == "renamed"


def test_link_is_idempotent_for_the_same_mapping(client, tmp_path):
    d = _folder(tmp_path, "notes")
    for _ in range(2):
        r = client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
        assert r.status_code == 200
    assert len(linked_apps.read_entries()) == 1


def test_link_name_collision_is_409(client, tmp_path):
    a = _folder(tmp_path, "same")
    b = tmp_path / "elsewhere2" / "same"
    b.mkdir(parents=True)
    assert client.post("/api/apps/link", json={"path": str(a)}, headers=HDRS).status_code == 200
    r = client.post("/api/apps/link", json={"path": str(b)}, headers=HDRS)
    assert r.status_code == 409


def test_link_same_folder_under_two_names_is_409(client, tmp_path):
    d = _folder(tmp_path, "notes")
    assert client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS).status_code == 200
    r = client.post(
        "/api/apps/link", json={"path": str(d), "name": "other"}, headers=HDRS
    )
    assert r.status_code == 409


def test_link_rejects_non_folders_and_workspace_paths(client, tmp_path, workspace):
    r = client.post(
        "/api/apps/link", json={"path": str(tmp_path / "nope")}, headers=HDRS
    )
    assert r.status_code == 400

    inside = workspace / "local" / "real-app"
    inside.mkdir(parents=True)
    r = client.post("/api/apps/link", json={"path": str(inside)}, headers=HDRS)
    assert r.status_code == 400
    assert "workspace" in r.json()["error"]


def test_workspace_linked_tag_collision_registry_wins(client, tmp_path, workspace):
    """A real <workspace>/linked/<name> folder colliding with a registry
    entry: the listing drops the workspace twin (both would carry the same
    ("linked", name) identity, which is the key the recents store and the
    link-status probe speak), and link-status withholds the workspace folder's
    identity so the explorer falls back to the folder's own page. Non-colliding
    names are untouched."""
    ws_twin = workspace / "linked" / "notes"
    ws_twin.mkdir(parents=True)
    (ws_twin / "index.html").write_text("<html></html>")
    ws_free = workspace / "linked" / "solo"
    ws_free.mkdir(parents=True)

    d = _folder(tmp_path, "notes")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)

    listed = [(a["tag"], a["name"], a["path"]) for a in
              client.get("/api/apps").json()["apps"]]
    assert ("linked", "notes", str(d)) in listed
    assert ("linked", "notes", str(ws_twin)) not in listed
    assert ("linked", "solo", str(ws_free)) in listed

    # colliding workspace folder: no route identity
    r = client.get("/api/apps/link-status", params={"path": str(ws_twin)}).json()
    assert r == {"status": "workspace", "name": None, "tag": None}
    # non-colliding one keeps it (route falls back to the codec path)
    r = client.get("/api/apps/link-status", params={"path": str(ws_free)}).json()
    assert r == {"status": "workspace", "name": "solo", "tag": "linked"}


def test_link_rejects_ancestors_of_the_workspace(client, tmp_path, workspace):
    """Linking the workspace's parent would make linked_app_dir_for claim
    every workspace path, shadowing real apps in the template gates."""
    r = client.post("/api/apps/link", json={"path": str(tmp_path)}, headers=HDRS)
    assert r.status_code == 400
    assert "contains the Fused workspace" in r.json()["error"]
    r = client.post("/api/apps/link", json={"path": str(workspace)}, headers=HDRS)
    assert r.status_code == 400


def test_registry_entries_containing_the_workspace_are_filtered_on_read(
    client, tmp_path, workspace, monkeypatch
):
    """A pre-fix or hand-edited registry entry that is the workspace or an
    ancestor of it never reaches consumers (listing, gates, env export)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    ok = _folder(tmp_path, "fine")
    (home / "linked_apps.json").write_text(json.dumps({"entries": [
        {"name": "shadow", "path": str(tmp_path)},
        {"name": "ws", "path": str(workspace)},
        {"name": "fine", "path": str(ok)},
    ]}))
    assert [e["name"] for e in linked_apps.read_entries()] == ["fine"]
    linked_apps.export_linked_apps_env()
    assert os.environ["FUSED_RENDER_LINKED_APPS"] == str(ok)


@pytest.mark.parametrize("bad", ["", "  ", "a/b", "a\\b", ".hidden"])
def test_link_rejects_bad_explicit_names(client, tmp_path, bad):
    d = _folder(tmp_path, "notes")
    r = client.post("/api/apps/link", json={"path": str(d), "name": bad}, headers=HDRS)
    # blank names fall back to the basename; malformed ones are rejected
    if not bad.strip():
        assert r.status_code == 200
        assert r.json()["app"]["name"] == "notes"
    else:
        assert r.status_code == 400


# ------------------------------------------------------------------ listing


def test_linked_apps_merge_with_workspace_apps_sorted(client, tmp_path, workspace):
    d = _folder(tmp_path, "zeta")
    (workspace / "local" / "alpha").mkdir(parents=True)
    (workspace / "local" / "alpha" / "index.html").write_text("<html></html>")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)

    listed = client.get("/api/apps").json()["apps"]
    assert [(a["tag"], a["name"]) for a in listed] == [
        ("linked", "zeta"),
        ("local", "alpha"),
    ]


def test_a_linked_folder_reports_its_preview_png_too(client, tmp_path):
    """The registry reuses app_listing.app_dict, so the thumbnail rule reaches
    linked apps without linked_apps.py knowing about it — the reason
    `preview_image` is resolved inside that shared shape."""
    d = _folder(tmp_path, "shot")
    (d / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)

    (app,) = client.get("/api/apps").json()["apps"]
    assert app["preview_image"] == str(d / "preview.png")


def test_missing_linked_folder_drops_out_but_stays_registered(client, tmp_path):
    d = _folder(tmp_path, "gone")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    (d / "index.html").unlink()
    d.rmdir()
    assert client.get("/api/apps").json()["apps"] == []
    # read-only filtering: the entry survives for when the folder comes back
    assert len(linked_apps.read_entries()) == 1


def test_zero_or_many_htmls_lists_as_entryless_card(client, tmp_path):
    d = _folder(tmp_path, "multi", htmls=("a.html", "b.html"))
    r = client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["app"]["entry_html"] is None


def test_corrupt_registry_reads_as_empty(client, tmp_path, monkeypatch):
    path = linked_apps._registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")
    assert linked_apps.read_entries() == []
    assert client.get("/api/apps").json()["apps"] == []


# ---------------------------------------------------------------- unlinking


def test_unlink_removes_from_listing_but_never_touches_the_folder(client, tmp_path):
    d = _folder(tmp_path, "notes")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    r = client.post("/api/apps/unlink", json={"name": "notes"}, headers=HDRS)
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get("/api/apps").json()["apps"] == []
    assert (d / "index.html").exists()  # target untouched


def test_unlink_unknown_name_reports_removed_false(client):
    r = client.post("/api/apps/unlink", json={"name": "nope"}, headers=HDRS)
    assert r.status_code == 200 and r.json()["removed"] is False


# --------------------------------------------------------------- link status


def test_link_status_tracks_the_lifecycle(client, tmp_path, workspace):
    d = _folder(tmp_path, "notes")
    st = lambda p: client.get("/api/apps/link-status", params={"path": p}).json()

    assert st(str(d)) == {"status": "unlinked", "name": None, "tag": None}
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert st(str(d)) == {"status": "linked", "name": "notes", "tag": "linked"}
    client.post("/api/apps/unlink", json={"name": "notes"}, headers=HDRS)
    assert st(str(d)) == {"status": "unlinked", "name": None, "tag": None}

    # A workspace app dir carries its route identity; the root and a tag
    # folder don't (the button falls back to the fs path there).
    inside = workspace / "local" / "real-app"
    inside.mkdir(parents=True)
    assert st(str(inside)) == {"status": "workspace", "name": "real-app", "tag": "local"}
    assert st(str(workspace / "local")) == {"status": "workspace", "name": None, "tag": None}


# ------------------------------------------------------------------- recents


def test_recents_resolve_linked_tag_through_the_registry(client, tmp_path):
    d = _folder(tmp_path, "notes")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    r = client.post(
        "/api/apps/recents/open",
        json={"tag": "linked", "name": "notes"},
        headers=HDRS,
    )
    assert r.json()["recorded"] is True
    assert [e["name"] for e in client.get("/api/apps/recents").json()["entries"]] == ["notes"]

    # unlink: the recent stops resolving and is filtered from GET
    client.post("/api/apps/unlink", json={"name": "notes"}, headers=HDRS)
    assert client.get("/api/apps/recents").json()["entries"] == []


# ------------------------------------------------------------ template gates
#
# The app/claude gates accept a linked folder through the
# FUSED_RENDER_LINKED_APPS env var (exported on every registry write) — pure
# env membership, no file reads. `history` stays workspace-only on purpose:
# its backend writes git history with the Fused identity, and a linked folder
# is the user's own repository.


def _condition(name):
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fused_render", "templates", name, "condition.py",
    )
    spec = importlib.util.spec_from_file_location(f"test_{name}_condition", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_link_and_unlink_export_the_env_var(client, tmp_path):
    d = _folder(tmp_path, "notes")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert os.environ["FUSED_RENDER_LINKED_APPS"] == str(d)
    client.post("/api/apps/unlink", json={"name": "notes"}, headers=HDRS)
    assert os.environ["FUSED_RENDER_LINKED_APPS"] == ""


# The chat gate was parametrised here alongside `app`: the two app modes had to
# agree about which folders are apps, or a folder would offer one without the
# other. It is out now — that mode is the ONE chat template and is offered on
# every directory, linked or not, so it has no app-folder rule left to agree
# about. The exclusion is asserted directly in
# test_a_linked_folder_needs_no_gate_for_the_chat below, rather than left implicit
# in a shortened parametrize list.
@pytest.mark.parametrize("template", ["app"])
def test_app_gates_accept_a_linked_folder(client, tmp_path, workspace, template):
    d = _folder(tmp_path, "notes")
    cond = _condition(template)
    assert cond.main(str(d)) is False  # not linked yet
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert cond.main(str(d)) is True
    # only the registered folder itself — never its parent or children
    assert cond.main(str(d.parent)) is False
    assert cond.main(str(d / "sub")) is False
    # the workspace two-level rule is untouched
    app_dir = workspace / "local" / "real"
    app_dir.mkdir(parents=True)
    assert cond.main(str(app_dir)) is True


def test_a_linked_folder_needs_no_gate_for_the_chat(client, tmp_path):
    """The chat gate stopped caring about linked apps when it stopped caring about
    app folders at all: a linked folder, its parent and its children are all just
    directories, and all of them are offered the chat. Pinned because the OLD
    behaviour — refuse until linked — is what a reader of the `app` test above
    would still expect of its former parameter."""
    cond = _condition("claude")
    d = _folder(tmp_path, "notes")
    (d / "sub").mkdir()
    assert cond.main(str(d)) is True          # never linked
    assert cond.main(str(d.parent)) is True
    assert cond.main(str(d / "sub")) is True
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert cond.main(str(d)) is True          # and linking changes nothing


def _git_repo(d):
    import subprocess

    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)


def test_history_gate_accepts_git_backed_linked_folders(client, tmp_path):
    d = _folder(tmp_path, "notes")
    cond = _condition("history")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert cond.main(str(d)) is False  # linked but no repo: no history to show
    _git_repo(d)
    assert cond.main(str(d)) is True
    assert cond.main(str(d / "index.html")) is True  # files inside too


def test_history_gate_finds_the_git_at_an_ancestor(client, tmp_path):
    """A linked folder is often a SUBFOLDER of the user's repository — the
    gate asks git (rev-parse ascent), like git/condition.py, instead of
    statting `.git` on the folder itself."""
    repo = tmp_path / "bigrepo"
    d = repo / "sub" / "app"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    _git_repo(repo)
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert _condition("history").main(str(d)) is True
    # ...and `git` is offered there too. It used to step aside ("one story, one
    # mode") with a whole extra `rev-parse` fork on every stat to work out
    # whether it should. `git` is the working tree and `history` is the
    # history, so both answer and the fork is gone.
    assert _condition("git").main(str(d)) is True


def test_git_gate_keeps_serving_ungitted_linked_folders(client, tmp_path, workspace):
    """Being linked has no bearing on the git gate either way: the only
    question is whether git says the path is in a work tree."""
    d = _folder(tmp_path, "notes")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    # linked but not git-backed: there is genuinely no repo, so no mode
    assert _condition("git").main(str(d)) is False
    assert _condition("history").main(str(d)) is False
    # a repo nested DEEPER than the linked folder: git serves it
    nested = d / "vendor"
    nested.mkdir()
    (nested / "f.txt").write_text("x")
    _git_repo(nested)
    # ...but on the FOLDER, not on a file in it: `git` is folder-only now (the
    # working tree belongs to the directory, and per-file history is `history`).
    assert _condition("git").main(str(nested / "f.txt")) is False
    assert _condition("git").main(str(nested)) is True


def test_history_backend_scopes_to_the_linked_subtree(client, tmp_path):
    """Linked app inside a larger repo: log lists only commits touching the
    app's folder, and a snapshot materialises the folder's subtree (its
    index.html at the top), not the whole repository."""
    import importlib.util
    import subprocess

    repo = tmp_path / "bigrepo"
    d = repo / "sub" / "app"
    d.mkdir(parents=True)
    _git_repo(repo)

    def commit(msg):
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "-m", msg], check=True)

    # `main.html`, not index.html: the snapshot's entry must resolve by the
    # app-entry rule (single top-level .html), not a hardcoded index.html.
    (repo / "other.txt").write_text("x")
    commit("repo-only commit")
    (d / "main.html").write_text("<html>v1</html>")
    commit("app commit")

    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fused_render", "templates", "history", "history.py",
    )
    spec = importlib.util.spec_from_file_location("test_linked_history_sub", path)
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)

    log = history.main("log", str(d / "index.html"))
    assert [c["subject"] for c in log["commits"]] == ["app commit"]

    snap = history.main("snapshot", str(d), log["commits"][0]["sha"])
    assert snap["entry"] and os.path.basename(snap["entry"]) == "main.html"
    with open(snap["entry"]) as f:
        assert f.read() == "<html>v1</html>"
    # subtree only — the repo-level file is not in the snapshot
    assert not os.path.exists(os.path.join(snap["dir"], "other.txt"))


def test_history_backend_shows_history_but_refuses_revert_for_linked(
    client, tmp_path
):
    """View-only history for a linked app: log/snapshot work, revert refuses —
    the repo is the user's own, no Fused-identity commits (linked_apps.py)."""
    import importlib.util
    import subprocess

    d = _folder(tmp_path, "notes")
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "user commit"],
        check=True,
    )
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fused_render", "templates", "history", "history.py",
    )
    spec = importlib.util.spec_from_file_location("test_linked_history", path)
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)

    log = history.main("log", str(d / "index.html"))
    assert [c["subject"] for c in log["commits"]] == ["user commit"]
    assert log["can_revert"] is False

    # snapshot works read-only: materialised under the shell home, repo untouched
    snap = history.main("snapshot", str(d / "index.html"), log["commits"][0]["sha"])
    assert snap.get("entry") and os.path.isfile(snap["entry"])

    sha = log["commits"][0]["sha"]
    res = history.main("revert", str(d / "index.html"), sha)
    assert "revert is disabled for linked apps" in res["error"]
    # nothing was written: still exactly the user's one commit
    out = subprocess.run(
        ["git", "-C", str(d), "log", "--format=%s"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "user commit"


def test_git_scoping_ignores_linked_folders(client, tmp_path, workspace):
    """A linked folder lives outside the workspace, so app_git's path-prefix
    scoping must yield None for it — nothing fused-render commits on its own
    (a Claude turn) may ever land in the user's own repository."""
    from fused_render import app_git

    d = _folder(tmp_path, "myrepo")
    client.post("/api/apps/link", json={"path": str(d)}, headers=HDRS)
    assert app_git.app_dir_for(str(d / "index.html")) is None
