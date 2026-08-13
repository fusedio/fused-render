"""Linked apps (fused_render/linked_apps.py): folders anywhere on disk listed
as apps under the virtual "linked" tag, via ~/.fused-render/linked_apps.json —
never a symlink in the workspace.

The registry is READ-ONLY as of D264: "Add as app" was its only writer and went
with the app concept, taking `link_app`/`unlink_app` and the link/unlink/status
routes with it. What is left, and what these tests cover, is everything that
still READS it — the /apps hub listing, the env export, and the template gates
and history scoping that ask whether a path belongs to a registered folder.
Registration happens here through `write_entries`, the module's one remaining
way in, which is also how a user with an existing registry file gets there.
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


def _register(*folders) -> None:
    """Put folders in the registry under their basenames, as an install that
    predates D264 would have. Goes through write_entries so the env export
    (which the template gates read) happens exactly as in production."""
    linked_apps.write_entries(
        [{"name": os.path.basename(str(d)), "path": str(d)} for d in folders]
    )


# ------------------------------------------------------------------- listing


def test_a_registered_folder_lists_under_the_linked_tag(client, tmp_path):
    d = _folder(tmp_path, "notes", title="My Notes")
    _register(d)

    (app,) = client.get("/api/apps").json()["apps"]
    assert app["tag"] == linked_apps.LINKED_TAG
    assert app["name"] == "notes"
    assert app["path"] == str(d)
    # Same app_dict shape as a workspace app — the registry reuses it wholesale.
    assert app["entry"] == app["entry_html"] == str(d / "index.html")
    assert app["title"] == "My Notes"


def test_workspace_linked_tag_collision_registry_wins(client, tmp_path, workspace):
    """A real <workspace>/linked/<name> folder colliding with a registry entry:
    the LISTING drops the workspace twin — two cards with the same
    ("linked", name) identity are indistinguishable to the recents store, which
    keys on exactly that pair.

The link-status half of this rule is gone with the route that reported it
    (D264); what remains is the listing, which is where the ambiguity actually
    mattered."""
    ws_twin = workspace / "linked" / "notes"
    ws_twin.mkdir(parents=True)
    (ws_twin / "index.html").write_text("<html></html>")
    ws_free = workspace / "linked" / "solo"
    ws_free.mkdir(parents=True)

    d = _folder(tmp_path, "notes")
    _register(d)

    listed = [(a["tag"], a["name"], a["path"]) for a in
              client.get("/api/apps").json()["apps"]]
    assert ("linked", "notes", str(d)) in listed
    assert ("linked", "notes", str(ws_twin)) not in listed
    assert ("linked", "solo", str(ws_free)) in listed


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


def test_linked_apps_merge_with_workspace_apps_sorted(client, tmp_path, workspace):
    d = _folder(tmp_path, "zeta")
    (workspace / "local" / "alpha").mkdir(parents=True)
    (workspace / "local" / "alpha" / "index.html").write_text("<html></html>")
    _register(d)

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
    _register(d)

    (app,) = client.get("/api/apps").json()["apps"]
    assert app["preview_image"] == str(d / "preview.png")


def test_missing_linked_folder_drops_out_but_stays_registered(client, tmp_path):
    d = _folder(tmp_path, "gone")
    _register(d)
    (d / "index.html").unlink()
    d.rmdir()
    assert client.get("/api/apps").json()["apps"] == []
    # read-only filtering: the entry survives for when the folder comes back
    assert len(linked_apps.read_entries()) == 1


def test_a_linked_folder_resolves_its_entry_on_the_shared_rule(client, tmp_path):
    """A linked app goes through the SAME `app_entry` as a workspace app, so
    D269's rule reaches it too: several pages resolve to the first in name order,
    and only a folder with none is an entry-less card.

    This asserted `entry_html is None` for the many-pages case until D269 — the
    registry never had a rule of its own, so widening the server's rule widened
    this with it, which is the point of `app_dict` being shared.
    """
    many = _folder(tmp_path, "multi", htmls=("b.html", "a.html"))
    _register(many)
    (app,) = client.get("/api/apps").json()["apps"]
    assert app["entry_html"] == str(many / "a.html")

    linked_apps.write_entries([])
    bare = _folder(tmp_path, "bare", htmls=())
    _register(bare)
    (app,) = client.get("/api/apps").json()["apps"]
    assert app["entry_html"] is None


def test_corrupt_registry_reads_as_empty(client, tmp_path, monkeypatch):
    path = linked_apps._registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")
    assert linked_apps.read_entries() == []
    assert client.get("/api/apps").json()["apps"] == []


def test_recents_resolve_linked_tag_through_the_registry(client, tmp_path):
    d = _folder(tmp_path, "notes")
    _register(d)
    r = client.post(
        "/api/apps/recents/open",
        json={"tag": "linked", "name": "notes"},
        headers=HDRS,
    )
    assert r.json()["recorded"] is True
    assert [e["name"] for e in client.get("/api/apps/recents").json()["entries"]] == ["notes"]

    # De-registered (there is no unlink route any more — a hand-edited or
    # emptied registry is the remaining way): the recent stops resolving and is
    # filtered out of the GET, which is the behaviour that matters.
    linked_apps.write_entries([])
    assert client.get("/api/apps/recents").json()["entries"] == []


# ------------------------------------------------------------ template gates
#
# The chat gate accepts a linked folder through the FUSED_RENDER_LINKED_APPS
# env var (exported on every registry write) — pure env membership, no file
# reads. The `app` gate used to be the other half of this and is gone with the
# template (D264). `history` REFUSES to revert there on purpose: its backend
# writes git history with the Fused identity, and a linked folder is the user's
# own repository — which is why the env export outlived the registration UI.


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
    _register(d)
    assert cond.main(str(d)) is True          # and linking changes nothing


def _git_repo(d):
    import subprocess

    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)


def test_history_gate_accepts_git_backed_linked_folders(client, tmp_path):
    d = _folder(tmp_path, "notes")
    cond = _condition("history")
    _register(d)
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
    _register(d)
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
    _register(d)
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

    _register(d)

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
    _register(d)

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
    _register(d)
    assert app_git.app_dir_for(str(d / "index.html")) is None
