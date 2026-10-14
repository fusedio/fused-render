"""The Marketplace backend (fused_render/marketplace.py + routers/marketplace.py).

No real network: the git-mirror source is pointed at a local bare repo built
with tests/_git_repo.py (FUSED_RENDER_MARKETPLACE_REPO accepts a path), and
the github-api source is exercised with `_http_get` faked out.

Covered: the catalog from both sources (and the API → mirror fallback with its
warning), AppEntry shape, install + update (update keeps `.venv`, `.fused` and
the user's own files, prunes what the author dropped, replaces what they
changed), the job runner, asset path validation, the stale-cache fallback, and
the X-Fused guard on POST /install.
"""
import json
import os
import shutil
import sys
import time
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import bare_repo, git, git_available, write  # noqa: E402

from fused_render import marketplace as mp  # noqa: E402

needs_git = pytest.mark.skipif(not git_available(), reason="git not installed")


def _meta(name, category="starters", version="1.0.0", **extra):
    return json.dumps({"schema": 1, "name": name, "description": f"{name} app",
                       "author": {"name": "Tester"}, "tags": [category],
                       "category": category, "version": version,
                       "requires_python": False, **extra})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated state dir + workspace, and a bare 'community repo' with two
    apps: local-chat (local-ai, featured by default) and sine-starter."""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.delenv("FUSED_RENDER_MARKETPLACE_SOURCE", raising=False)
    monkeypatch.delenv("FUSED_RENDER_MARKETPLACE_BRANCH", raising=False)
    seed = str(tmp_path / "seed")
    remote = str(tmp_path / "remote.git")
    os.makedirs(seed)
    git(seed, "init", "-q")
    write(seed, "README.md", "# community\n")
    write(seed, "local-chat/metadata.json", _meta("Local Chat", "local-ai", "1.2.0"))
    write(seed, "local-chat/index.html", "<html>chat</html>")
    write(seed, "local-chat/readme.md", "# Local Chat\n\n![shot](preview.png)\n")
    write(seed, "local-chat/preview.png", "PNGDATA")
    write(seed, "local-chat/icon.svg", "<svg/>")
    write(seed, "sine-starter/metadata.json", _meta("Sine Starter", version="1.0.0"))
    write(seed, "sine-starter/index.html", "<html>v1</html>")
    write(seed, "sine-starter/sine.py", "print(1)\n")
    write(seed, "sine-starter/old.txt", "author file\n")
    write(seed, "sine-starter/lib/helper.js", "// v1\n")
    # Not apps: no metadata.json / not slug-shaped.
    write(seed, "no-meta/index.html", "x")
    write(seed, "Bad_Name/metadata.json", _meta("Bad"))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    bare_repo(remote)
    git(seed, "remote", "add", "origin", remote)
    git(seed, "push", "-q", "-u", "origin", "HEAD:main")
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_REPO", remote)
    with mp._JOBS_LOCK:
        mp._JOBS.clear()
        mp._JOB_FINISHED.clear()
    return {"home": home, "ws": ws, "seed": seed, "remote": remote}


def _push(seed, changes, removes=()):
    for rel, text in changes.items():
        write(seed, rel, text)
    for rel in removes:
        os.remove(os.path.join(seed, rel))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "change")
    git(seed, "push", "-q", "origin", "HEAD:main")


def _client():
    from fused_render.server.routers.marketplace import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@needs_git
def test_catalog_from_git_mirror(env):
    cat = mp.catalog()
    assert cat["status"] == "ok"
    assert cat["source"] == mp.SOURCE_GIT
    assert cat["stale"] is False
    # A non-GitHub repo has no API to try, so no fallback warning.
    assert cat["warning"] is None
    assert [a["slug"] for a in cat["apps"]] == ["local-chat", "sine-starter"]
    assert [c["id"] for c in cat["categories"]][:4] == list(mp.CATEGORY_ORDER)
    counts = {c["id"]: c["count"] for c in cat["categories"]}
    assert counts["starters"] == 1 and counts["local-ai"] == 1
    chat = cat["apps"][0]
    assert chat["name"] == "Local Chat" and chat["version"] == "1.2.0"
    assert chat["author"] == {"name": "Tester", "github": None}
    assert chat["icon_url"] == "/api/marketplace/asset/local-chat/icon.svg"
    assert chat["preview_url"] == "/api/marketplace/asset/local-chat/preview.png"
    assert chat["featured"] is True  # curated default set
    assert chat["installed"] is None and chat["update_available"] is False
    assert chat["compatible"] is True
    assert chat["file_count"] == 5
    # git's own tree sha — the same value the API source reports.
    tree = git(env["seed"], "rev-parse", "HEAD:local-chat").strip()
    assert chat["tree_sha"] == tree
    sine = cat["apps"][1]
    assert sine["featured"] is False and sine["icon_url"] is None
    # requires_python is taken from metadata when declared.
    assert sine["requires_python"] is False
    # Cached: catalog.json exists and a second call doesn't rebuild.
    assert os.path.isfile(mp.catalog_path())
    assert mp.catalog()["fetched_at"] == cat["fetched_at"]


@needs_git
def test_api_failure_falls_back_to_git_mirror_with_warning(env, monkeypatch):
    remote = env["remote"]
    monkeypatch.setattr(mp, "repo_config", lambda: {
        "repo": "o/r", "gh": "o/r", "clone_url": remote, "branch": "main"})
    monkeypatch.setattr(mp, "_http_get", lambda *a, **k: (None, "GitHub API rate-limited"))
    cat = mp.catalog()
    assert cat["status"] == "ok"
    assert cat["source"] == mp.SOURCE_GIT
    assert cat["warning"] == "GitHub API rate-limited — using git mirror"
    assert cat["apps"][0]["source_url"] == "https://github.com/o/r/tree/main/local-chat"


def test_catalog_from_github_api(env, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_REPO", "o/r")

    class Resp:
        def __init__(self, payload=None, content=b""):
            self._payload, self.content = payload, content

        def json(self):
            return self._payload

    tree = {"truncated": False, "tree": [
        {"path": "hello", "type": "tree", "sha": "t-hello"},
        {"path": "hello/metadata.json", "type": "blob", "size": 50},
        {"path": "hello/index.html", "type": "blob", "size": 10},
        {"path": "hello/preview.png", "type": "blob", "size": 100},
        {"path": "hello/pyproject.toml", "type": "blob", "size": 5},
        {"path": "hello/../evil", "type": "blob", "size": 1},
        {"path": "nometa", "type": "tree", "sha": "t-x"},
        {"path": "nometa/index.html", "type": "blob", "size": 1},
    ]}
    meta = json.dumps({"name": "Hello", "category": "geospatial", "version": "2.0.0",
                       "min_fused_render": "999.0.0", "featured": True}).encode()

    def fake_get(url, **kw):
        if url.endswith("/commits/main"):
            return Resp({"sha": "c0ffee"}), None
        if "/git/trees/c0ffee" in url:
            return Resp(tree), None
        if url == f"{mp.RAW_BASE}/o/r/c0ffee/hello/metadata.json":
            return Resp(content=meta), None
        return None, "not found on GitHub (404)"

    monkeypatch.setattr(mp, "_http_get", fake_get)
    cat = mp.catalog()
    assert cat["source"] == mp.SOURCE_API and cat["commit"] == "c0ffee"
    assert cat["repo"] == "o/r" and cat["warning"] is None
    (app,) = cat["apps"]
    assert app["slug"] == "hello" and app["tree_sha"] == "t-hello"
    assert app["size_bytes"] == 165 and app["file_count"] == 4  # traversal path dropped
    assert app["featured"] is True  # declared in metadata
    assert app["compatible"] is False  # min_fused_render 999
    assert app["requires_python"] is True  # undeclared: inferred from pyproject.toml
    assert app["source_url"] == "https://github.com/o/r/tree/main/hello"


@needs_git
def test_install_then_update_keeps_venv_and_user_files(env):
    snap, _ = mp.get_snapshot()
    res = mp.install_app("sine-starter", snap)
    dest = os.path.join(str(env["ws"]), "community", "sine-starter")
    assert res["action"] == "install" and res["path"] == dest
    assert open(os.path.join(dest, "index.html")).read() == "<html>v1</html>"
    stamp = mp.read_stamp(dest)
    assert stamp["slug"] == "sine-starter" and stamp["version"] == "1.0.0"
    assert stamp["source"] == mp.SOURCE_GIT
    assert set(stamp["files"]) == {"metadata.json", "index.html", "sine.py", "old.txt",
                                   "lib/helper.js"}
    assert {"slug", "version", "tree_sha", "commit", "repo", "installed_at",
            "source", "files"} <= set(stamp)
    # No staging left behind in community/.
    assert sorted(os.listdir(os.path.dirname(dest))) == ["sine-starter"]
    entry = next(a for a in mp.catalog()["apps"] if a["slug"] == "sine-starter")
    assert entry["installed"]["modified"] is False
    assert entry["installed"]["tree_sha"] == entry["tree_sha"]
    assert entry["update_available"] is False

    # The user works in their copy.
    os.makedirs(os.path.join(dest, ".venv", "bin"))
    write(dest, ".venv/bin/python", "venv")
    write(dest, ".fused/data/state.db", "user state")
    write(dest, "notes.txt", "user notes")
    write(dest, "sine.py", "print('edited')\n")
    entry = next(a for a in mp.catalog()["apps"] if a["slug"] == "sine-starter")
    assert entry["installed"]["modified"] is True

    # The author ships v2: changes index.html, deletes old.txt.
    _push(env["seed"], {"sine-starter/index.html": "<html>v2</html>",
                        "sine-starter/metadata.json": _meta("Sine Starter", version="2.0.0")},
          removes=["sine-starter/old.txt"])
    entry = next(a for a in mp.catalog(refresh=True)["apps"] if a["slug"] == "sine-starter")
    assert entry["update_available"] is True and entry["version"] == "2.0.0"

    snap, _ = mp.get_snapshot()
    res = mp.install_app("sine-starter", snap)
    assert res["action"] == "update"
    assert open(os.path.join(dest, "index.html")).read() == "<html>v2</html>"
    assert open(os.path.join(dest, ".venv/bin/python")).read() == "venv"
    assert open(os.path.join(dest, ".fused/data/state.db")).read() == "user state"
    assert open(os.path.join(dest, "notes.txt")).read() == "user notes"
    assert not os.path.exists(os.path.join(dest, "old.txt"))  # author's deletion
    assert res["pruned"] == ["old.txt"]
    # A tracked file is replaced by the app's version on update.
    assert open(os.path.join(dest, "sine.py")).read() == "print(1)\n"
    entry = next(a for a in mp.catalog()["apps"] if a["slug"] == "sine-starter")
    assert entry["update_available"] is False and entry["installed"]["version"] == "2.0.0"
    assert [a["slug"] for a in mp.installed_list()["apps"]] == ["sine-starter"]


@needs_git
def test_install_refuses_a_foreign_folder(env):
    dest = os.path.join(str(env["ws"]), "community", "sine-starter")
    write(dest, "mine.txt", "not from the marketplace")
    snap, _ = mp.get_snapshot()
    with pytest.raises(mp.InstallError, match="was not installed from the Marketplace"):
        mp.install_app("sine-starter", snap)
    assert os.listdir(dest) == ["mine.txt"]


@needs_git
def test_install_job_runs_in_background_and_is_idempotent(env):
    client = _client()
    # The X-Fused guard.
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"})
    assert r.status_code == 403
    r = client.post("/api/marketplace/install", json={"slug": "../x"},
                    headers={"X-Fused": "1"})
    assert r.status_code == 400 and r.json()["status"] == "error"
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    job = r.json()["job"]
    assert job["slug"] == "sine-starter" and job["action"] == "install"
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get("/api/marketplace/jobs").json()["jobs"]["sine-starter"]
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "done", job
    assert job["progress"] == 1.0 and job["bytes_done"] == job["bytes_total"]
    assert job["path"].endswith("/community/sine-starter")
    installed = client.get("/api/marketplace/installed").json()["apps"]
    assert [a["slug"] for a in installed] == ["sine-starter"]
    # A second POST after it finished is an update.
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"},
                    headers={"X-Fused": "1"})
    assert r.json()["job"]["action"] == "update"


@needs_git
def test_asset_and_detail_routes(env):
    client = _client()
    r = client.get("/api/marketplace/asset/local-chat/icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.headers["cache-control"] == "max-age=3600"
    assert r.content == b"<svg/>"
    # Disk-cached per commit.
    commit = mp.get_snapshot()[0]["commit"]
    assert os.path.isfile(os.path.join(mp.state_dir(), "assets", commit, "local-chat", "icon.svg"))
    d = client.get("/api/marketplace/app/local-chat").json()
    assert d["status"] == "ok" and d["app"]["slug"] == "local-chat"
    assert d["readme"].startswith("# Local Chat")
    assert d["readme_base"] == "/api/marketplace/asset/local-chat/"
    assert {"path": "preview.png", "size": 7} in d["files"]
    assert client.get("/api/marketplace/app/nope-nope").status_code == 404


@needs_git
@pytest.mark.parametrize("path", [
    "../sine-starter/index.html", "a/../../etc/passwd", "%2e%2e/README.md",
    "/etc/passwd", "missing.png", ".git/config", "",
])
def test_asset_path_traversal_rejected(env, path):
    assert mp.get_asset("local-chat", path) is None
    assert mp.get_asset("../local-chat", "icon.svg") is None
    assert mp.get_asset("local-chat", "../README.md") is None
    # Percent-encoded so the HTTP client cannot normalise the `..` away
    # before it reaches the route (the server decodes it back).
    r = _client().get(f"/api/marketplace/asset/local-chat/{quote(path, safe='')}")
    assert r.status_code == 404


@needs_git
def test_stale_cache_fallback(env):
    cat = mp.catalog()
    assert cat["stale"] is False
    # The remote disappears (offline). With the mirror still there, a refresh
    # serves the old checkout, flagged stale.
    shutil.move(env["remote"], env["remote"] + ".gone")
    cat = mp.catalog(refresh=True)
    assert cat["status"] == "ok" and cat["stale"] is True
    assert "could not update the git mirror" in cat["warning"]
    assert [a["slug"] for a in cat["apps"]] == ["local-chat", "sine-starter"]
    # Without the mirror either, the cached catalog.json is served, stale.
    shutil.rmtree(mp.mirror_dir())
    cat = mp.catalog(refresh=True)
    assert cat["status"] == "ok" and cat["stale"] is True
    assert cat["warning"].startswith("Could not refresh the catalog")
    assert len(cat["apps"]) == 2
    # And with no cache at all: an error, no apps.
    os.remove(mp.catalog_path())
    cat = mp.catalog(refresh=True)
    assert cat["status"] == "error" and cat["apps"] == [] and cat["message"]
