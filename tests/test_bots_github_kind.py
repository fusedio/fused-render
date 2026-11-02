"""The `github` app source kind of the bot package (`.fusedbot`, docs/bots.md §17): export resolves a clean,
pushed GitHub clone to a reference (no bytes, no member), a dirty/unpushed one to bundled with a `fallback`
note; import accepts only a `https://github.com/<owner>/<repo>` url, reads installed/missing from local git
and the workspace walk only, and a clone failure is that row's error, never the bot's.
_bots_conftest points FUSED_RENDER_HOME and FUSED_RENDER_DIR at tmp dirs; no test touches the network
(`appfetch.download_github_app` is stubbed wherever an install would clone)."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import os
import subprocess

import pytest

from fused_render.bots import paths as bpaths

REPO = "someone/gh-app"
URL = f"https://github.com/{REPO}"


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _gh_clone(folder="gh-app", *, pushed=True):
    """An app under apps_root() that heads its own repo with a GitHub `origin`; `pushed` fakes the fetch of the
    default branch at HEAD (`refs/remotes/origin/main`), which is all github_link_state reads — no network."""
    app = os.path.join(bpaths.apps_root(), folder)
    os.makedirs(app)
    with open(os.path.join(app, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><html><head><meta name="fused-app"><title>GH App</title></head><body>hi</body></html>')
    _git("init", "-q", "-b", "main", cwd=app)
    _git("add", ".", cwd=app)
    _git("commit", "-q", "-m", "init", cwd=app)
    _git("remote", "add", "origin", URL + ".git", cwd=app)
    if pushed:
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=app)
    return os.path.realpath(app), _git("rev-parse", "HEAD", cwd=app)


def _row(**src):
    return {"folder": "gh-app", "name": "GH App", "app_id": None, "source": {"kind": "github", "url": URL, "repo": REPO, **src}}


# ------------------------------------------------------------------ export ---
def test_clean_pushed_clone_travels_as_a_github_reference():
    from fused_render.bots import package

    app, head = _gh_clone()
    assert package.resolve_source(app) == {"kind": "github", "url": URL, "repo": REPO, "commit": head}
    row = package._app_row("gh-app", {"folder": "gh-app", "name": "GH App", "app_id": None, "source": {"kind": "bundled"}})
    # A reference: nothing ships, nothing is sized, and the app folder is never stamped with an id.
    assert row["source"]["kind"] == "github" and row["bytes"] == 0
    assert "mints_app_id" not in row and "fallback" not in row and "problem" not in row
    # A folder that is gone keeps its github manifest source (a reference needs no folder to travel).
    gone = package._app_row("gone-app", {"folder": "gone-app", "name": "Gone", "app_id": "x",
                                         "source": {"kind": "github", "url": "https://github.com/a/b", "repo": "a/b"}})
    assert gone["source"]["kind"] == "github" and "problem" not in gone


@pytest.mark.parametrize("change,code", [("dirty", "dirty"), ("commit", "unpushed"), ("no_remote_ref", "no_default")])
def test_github_copy_the_link_would_not_carry_is_bundled_with_a_fallback(change, code):
    from fused_render.bots import package

    app, _head = _gh_clone(pushed=change != "no_remote_ref")
    if change in ("dirty", "commit"):
        with open(os.path.join(app, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("mine")
    if change == "commit":
        _git("add", ".", cwd=app)
        _git("commit", "-q", "-m", "local", cwd=app)
    assert package.resolve_source(app) == {"kind": "bundled", "fallback": {"kind": "github", "code": code, "repo": REPO}}
    row = package._app_row("gh-app", {"folder": "gh-app", "name": "GH App", "app_id": None, "source": {"kind": "bundled"}})
    # `fallback` moves onto the plan row; the row's source (what an index row would carry) is plain bundled.
    assert row["source"] == {"kind": "bundled"} and row["fallback"]["code"] == code and row["bytes"] > 0


def test_github_link_state_that_raises_degrades_to_bundled(monkeypatch):
    from fused_render import share_app
    from fused_render.bots import package

    app, _head = _gh_clone()

    def boom(*_a, **_k):
        raise PermissionError("no git for you")

    monkeypatch.setattr(share_app, "github_link_state", boom)
    assert package.resolve_source(app) == {"kind": "bundled"}


# ------------------------------------------------------------------ import ---
@pytest.mark.parametrize("url", [
    None, "", "http://evil.example/owner/repo", "https://github.com.evil.example/owner/repo",
    "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo", "https://github.com/owner",
    "https://github.com/owner/..", "https://github.com/owner/repo/tree/main", "https://github.com/../etc",
    "file:///etc/passwd", "--upload-pack=touch /tmp/x", "https://github.com/-owner/repo",
])
def test_app_rows_turn_a_github_row_without_a_repo_link_into_unknown(url):
    from fused_render.bots import botfile

    src = {"kind": "github", "repo": REPO}
    if url is not None:
        src["url"] = url
    rows = botfile._app_rows({"apps": [{"folder": "x", "source": src}]})
    assert rows[0]["source"]["kind"] == "unknown"
    assert botfile._row_state(rows[0], None)["tickable"] is False


def test_app_rows_keep_a_github_link_and_only_the_known_bounded_keys():
    from fused_render.bots import botfile

    rows = botfile._app_rows({"apps": [{"folder": "x", "source": {
        "kind": "github", "url": URL, "repo": REPO, "commit": "a" * 40, "evil": "rm -rf", "n": 3}}]})
    assert rows[0]["source"] == {"kind": "github", "url": URL, "repo": REPO, "commit": "a" * 40}


def test_row_state_github_installed_by_clone_or_app_id_else_missing(monkeypatch):
    from fused_render import appfile
    from fused_render.bots import botfile

    assert botfile._row_state(_row(), None) == {"state": "missing", "ticked": True, "tickable": True}
    app, _head = _gh_clone()
    st = botfile._row_state(_row(), None)
    assert st["state"] == "installed" and st["ticked"] and st["tickable"] and st["reason"]
    # No clone of the repo, but a copy carrying the row's app id: also installed, same shape.
    other = {**_row(url="https://github.com/other/repo", repo="other/repo"), "app_id": "abc123"}
    monkeypatch.setattr(appfile, "workspace_app_with_id", lambda app_id, **_k: app if app_id == "abc123" else None)
    assert botfile._row_state(other, None) == st


def test_offline_github_row_is_an_error_row_and_the_bot_is_still_made(monkeypatch, tmp_path):
    from fused_render import appfetch
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, registry

    _gh_clone()
    b = bm.create("GH bot", "", "", "do things", greet=False)
    with b.lock:
        b.meta["apps"] = [{"folder": "gh-app", "name": "GH App", "app_id": None, "source": {"kind": "bundled"}}]
        b.save()
    path = package.write(b.id, memory=False, apps=["gh-app"], file_name=None, dest_dir=str(tmp_path / "out"))

    # A second machine: a fresh workspace with no clone of the repo, and GitHub unreachable.
    ws2 = tmp_path / "ws2"
    (ws2 / "app").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws2))
    reset_bots_caches()  # noqa: F405 — from _bots_conftest

    def offline(url):
        raise appfetch.FetchError("network is unreachable")

    monkeypatch.setattr(appfetch, "download_github_app", offline)
    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)  # no greeting thread outliving the test
    probe = botfile.probe(path)
    assert [(a["source"]["kind"], a["state"], a["ticked"]) for a in probe["apps"]] == [("github", "missing", True)]
    out = botfile.install(path, apps=["gh-app"], memory=False)
    assert out["ok"] and out["bot_id"]
    assert out["apps"] == [{"folder": "gh-app", "ok": False, "state": "error",
                            "error": f"could not clone {REPO} from GitHub: network is unreachable"}]
    nb = registry.get(out["bot_id"])
    assert "gh-app" not in (nb.meta.get("trusted_apps") or [])  # a failed row grants nothing
    assert not os.path.exists(ws2 / "app" / "gh-app")


def test_contributor_copy_with_own_edits_is_bundled_and_a_clean_one_drops_the_local_commit(monkeypatch):
    from fused_render import share_app
    from fused_render.bots import package

    app, _head = _gh_clone()
    state = {"ok": True, "url": "x", "repo": "author/app", "reason": None, "code": "contributor", "changes": 2}
    monkeypatch.setattr(share_app, "github_link_state", lambda *_a, **_k: dict(state))
    # Edits of their own: the author's link would drop them, so the copy travels bundled (its edits with it).
    assert package.resolve_source(app) == {"kind": "bundled",
                                           "fallback": {"kind": "github", "code": "dirty", "repo": "author/app"}}
    # No edits: the author's repo travels, but never the local HEAD (it need not exist upstream).
    state["changes"] = 0
    assert package.resolve_source(app) == {"kind": "github", "url": "https://github.com/author/app",
                                           "repo": "author/app"}


@pytest.mark.parametrize("src,want", [
    # `repo` is never taken from the file: the url (what gets cloned) names it.
    ({"url": "https://github.com/attacker/evil", "repo": "trusted/app"},
     {"url": "https://github.com/attacker/evil", "repo": "attacker/evil"}),
    ({"url": "https://github.com/Owner/App.git/?x=1#readme"}, {"url": "https://github.com/Owner/App", "repo": "Owner/App"}),
    ({"url": "https://github.com/owner/repo?" + "x" * 300, "repo": "owner/repo"}, None),  # over-long: refused raw
])
def test_app_rows_rewrite_a_github_row_from_its_url(src, want):
    from fused_render.bots import botfile

    rows = botfile._app_rows({"apps": [{"folder": "x", "source": {"kind": "github", **src}}]})
    if want is None:
        assert rows[0]["source"]["kind"] == "unknown"
    else:
        assert rows[0]["source"] == {"kind": "github", **want}


def test_app_id_match_is_display_only_the_install_still_clones(monkeypatch, tmp_path):
    from fused_render import appfetch, appfile
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile, package, registry

    _gh_clone()
    b = bm.create("GH bot", "", "", "do things", greet=False)
    with b.lock:
        b.meta["apps"] = [{"folder": "gh-app", "name": "GH App", "app_id": "abc123", "source": {"kind": "bundled"}}]
        b.save()
    path = package.write(b.id, memory=False, apps=["gh-app"], file_name=None, dest_dir=str(tmp_path / "out"))

    # A second machine with no clone of the repo, but a hand-made app carrying the row's (public) app id.
    ws2 = tmp_path / "ws2"
    mine = ws2 / "app" / "mine"
    mine.mkdir(parents=True)
    (mine / "index.html").write_text("<p>mine</p>", encoding="utf-8")
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws2))
    reset_bots_caches()  # noqa: F405 — from _bots_conftest
    monkeypatch.setattr(appfile, "workspace_app_with_id", lambda app_id, **_k: str(mine) if app_id == "abc123" else None)
    cloned = []

    def offline(url):
        cloned.append(url)
        raise appfetch.FetchError("network is unreachable")

    monkeypatch.setattr(appfetch, "download_github_app", offline)
    monkeypatch.setattr(bm.Bot, "greet", lambda self: None)  # no greeting thread outliving the test
    assert [a["state"] for a in botfile.probe(path)["apps"]] == ["installed"]  # the page may say so
    out = botfile.install(path, apps=["gh-app"], memory=False)
    assert out["ok"] and cloned == [URL]  # ...but the install asked GitHub, it did not take the id's word
    assert out["apps"] == [{"folder": "gh-app", "ok": False, "state": "error",
                            "error": f"could not clone {REPO} from GitHub: network is unreachable"}]
    nb = registry.get(out["bot_id"])
    trusted = nb.meta.get("trusted_apps") or []
    assert "gh-app" not in trusted and "mine" not in trusted
    assert "mine" not in [r.get("folder") for r in nb.meta.get("apps") or []]
    assert sorted(os.listdir(mine)) == ["index.html"] and (mine / "index.html").read_text(encoding="utf-8") == "<p>mine</p>"
