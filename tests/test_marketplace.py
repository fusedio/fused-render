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
SHA = "c0ffee" + "0" * 34


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
    # Tests refresh back to back; the once-a-minute limit has its own test.
    monkeypatch.setattr(mp, "REFRESH_MIN_INTERVAL_S", 0)
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
    assert chat["icon_url"] == f"/api/marketplace/asset/local-chat/icon.svg?v={chat['tree_sha'][:12]}"
    assert chat["preview_url"] == f"/api/marketplace/asset/local-chat/preview.png?v={chat['tree_sha'][:12]}"
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
            return Resp({"sha": SHA}), None
        if f"/git/trees/{SHA}" in url:
            return Resp(tree), None
        if url == f"{mp.RAW_BASE}/o/r/{SHA}/hello/metadata.json":
            return Resp(content=meta), None
        return None, "not found on GitHub (404)"

    monkeypatch.setattr(mp, "_http_get", fake_get)
    cat = mp.catalog()
    assert cat["source"] == mp.SOURCE_API and cat["commit"] == SHA
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
    # Let the update job finish while the fixture's env still points it at
    # tmp_path (teardown restores the real home dirs under a live thread).
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get("/api/marketplace/jobs").json()["jobs"]["sine-starter"]
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "done", job


@needs_git
def test_asset_and_detail_routes(env):
    client = _client()
    r = client.get("/api/marketplace/asset/local-chat/icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    # Unversioned (readme-relative) URLs revalidate; the catalog's own
    # icon/preview URLs carry ?v=<tree sha> and cache for a day.
    assert r.headers["cache-control"] == "no-cache"
    entry = next(a for a in client.get("/api/marketplace/catalog").json()["apps"]
                 if a["slug"] == "local-chat")
    assert "?v=" in entry["icon_url"]
    r2 = client.get(entry["icon_url"])
    assert r2.status_code == 200 and r2.headers["cache-control"] == "max-age=86400"
    # A `v` that is not the app's tree sha (a readme's own `?v=1`) earns nothing.
    r3 = client.get("/api/marketplace/asset/local-chat/icon.svg?v=1")
    assert r3.status_code == 200 and r3.headers["cache-control"] == "no-cache"
    # The key comes back with the bytes, from the one snapshot both were read
    # from — never from a second cache read a refresh could slip between.
    data, _ctype, version = mp.get_asset_versioned("local-chat", "icon.svg")
    assert data == b"<svg/>" and entry["icon_url"].endswith(f"?v={version}")
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


def test_api_commit_must_be_a_full_sha(env, monkeypatch):
    """A commit that is not 40/64 hex never becomes a cache path or a URL."""
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_REPO", "o/r")
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_SOURCE", "api")

    class Resp:
        def json(self):
            return {"sha": "../../etc"}

    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (Resp(), None))
    cat = mp.catalog()
    assert cat["status"] == "error" and "unexpected" in cat["message"]


# --- 1: a keep-data uninstall's husk is not "installed" -----------------------

@needs_git
def test_husk_is_not_installed_but_install_reuses_its_data(env):
    snap, _ = mp.get_snapshot()
    mp.install_app("sine-starter", snap)
    dest = os.path.join(str(env["ws"]), "community", "sine-starter")
    write(dest, ".fused/data/state.db", "user state")
    # What routers/app_uninstall._strip_app_files leaves: only `.fused/`
    # (the marketplace stamp inside it survives).
    for name in os.listdir(dest):
        if name != ".fused":
            p = os.path.join(dest, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    assert mp.read_stamp(dest) is not None and mp._is_husk(dest)
    assert mp.installed_apps() == {}
    entry = next(a for a in mp.catalog()["apps"] if a["slug"] == "sine-starter")
    assert entry["installed"] is None and entry["update_available"] is False
    assert mp.installed_list()["apps"] == []
    assert mp.app_detail("sine-starter")["app"]["installed"] is None
    # Installing again is an install to the user, but lands IN the husk,
    # keeping its data.
    res = mp.install_app("sine-starter", snap)
    assert res["action"] == "install" and res["path"] == dest
    assert open(os.path.join(dest, "index.html")).read() == "<html>v1</html>"
    assert open(os.path.join(dest, ".fused/data/state.db")).read() == "user state"
    assert list(mp.installed_apps()) == ["sine-starter"]


# --- 2: only /catalog rebuilds a stale cache; nobody queues behind a build ----

def _api_cache(monkeypatch, files, checked_at):
    """A github-api catalog.json for repo o/r, `checked_at` as given."""
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_REPO", "o/r")
    snap = {"source": mp.SOURCE_API, "commit": SHA, "fetched_at": "then",
            "checked_at": checked_at, "stale": False, "warning": None,
            "apps": {"hello": {"meta": {"name": "Hello"}, "tree_sha": "t",
                               "files": [{"path": p, "size": 4} for p in files]}},
            **mp._cfg_fields(mp.repo_config())}
    mp._write_cache(snap)
    return snap


class _Bytes:
    def __init__(self, content):
        self.content = content


def test_stale_cache_readers_never_call_the_builder(env, monkeypatch):
    _api_cache(monkeypatch, ["icon.svg", "metadata.json"], checked_at=0)  # ancient
    built = []
    monkeypatch.setattr(mp, "_build_snapshot", lambda: built.append(1) or 1 / 0)
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (_Bytes(b"<svg/>"), None))
    assert mp.get_asset("hello", "icon.svg") == (b"<svg/>", "image/svg+xml")
    assert mp.app_detail("hello")["app"]["slug"] == "hello"
    assert mp.installed_list() == {"apps": []}
    assert built == []
    # The route too.
    assert _client().get("/api/marketplace/asset/hello/icon.svg").status_code == 200
    assert built == []
    # /catalog is the one that rebuilds (here it crashes → stale cache).
    cat = mp.catalog()
    assert built == [1] and cat["status"] == "ok" and cat["stale"] is True


def test_catalog_does_not_wait_behind_a_running_rebuild(env, monkeypatch):
    _api_cache(monkeypatch, ["icon.svg"], checked_at=0)
    monkeypatch.setattr(mp, "_build_snapshot", lambda: pytest.fail("built while locked"))
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (_Bytes(b"<svg/>"), None))
    assert mp._CATALOG_LOCK.acquire(timeout=1)  # "another request is rebuilding"
    try:
        t0 = time.monotonic()
        cat = mp.catalog()
        assert cat["status"] == "ok" and [a["slug"] for a in cat["apps"]] == ["hello"]
        assert mp.get_asset("hello", "icon.svg") is not None
        assert time.monotonic() - t0 < 2
    finally:
        mp._CATALOG_LOCK.release()


def test_crashing_build_backs_off_and_refresh_is_rate_limited(env, monkeypatch):
    monkeypatch.setattr(mp, "REFRESH_MIN_INTERVAL_S", 60)
    _api_cache(monkeypatch, ["icon.svg"], checked_at=0)
    built = []

    def crash():
        built.append(1)
        raise RuntimeError("bug")

    monkeypatch.setattr(mp, "_build_snapshot", crash)
    assert mp.catalog()["stale"] is True and built == [1]
    # The crash stamped checked_at: neither the next view nor a forced
    # refresh within the minute retries it.
    assert mp.catalog()["status"] == "ok" and built == [1]
    assert mp.catalog(refresh=True)["status"] == "ok" and built == [1]
    # Past the minute, refresh=1 rebuilds again.
    cache = mp._read_cache()
    cache["checked_at"] = time.time() - 61
    mp._write_cache(cache)
    mp.catalog(refresh=True)
    assert built == [1, 1]


# --- 3: KEEP names and case-fold collisions (case-insensitive filesystems) ----

def test_wanted_skips_keep_names_case_insensitively():
    files = [{"path": p, "size": 1} for p in
             (".FUSED/x", ".Venv/bin/python", ".GIT/config", ".fused/y", "index.html",
              "docs/.git/x")]
    assert [f["path"] for f in mp._wanted(files)] == ["index.html", "docs/.git/x"]


def test_case_fold_collisions_are_refused(env, monkeypatch):
    def paths(*ps):
        return [{"path": p, "size": 1} for p in ps]

    assert mp._case_collisions(paths("README.md", "lib/a.js", "lib/b.js")) == []
    assert mp._case_collisions(paths("README.md", "readme.md")) == ["README.md / readme.md"]
    assert mp._case_collisions(paths("Lib/a.js", "lib/b.js")) == ["Lib / lib"]
    snap = {"source": mp.SOURCE_API, "commit": SHA, "repo": "o/r",
            "apps": {"clash": {"meta": {}, "tree_sha": "t",
                               "files": paths("index.html", "Index.html")}}}
    monkeypatch.setattr(mp, "_stage", lambda *a: pytest.fail("staged a colliding app"))
    with pytest.raises(mp.InstallError, match="differ only by upper/lower case"):
        mp.install_app("clash", snap)
    assert not os.path.exists(os.path.join(str(env["ws"]), "community", "clash"))


# --- 4: a mirror install copies the catalog's commit, not the checkout --------

@needs_git
def test_mirror_install_copies_the_catalog_commit(env):
    snap, _ = mp.get_snapshot()
    # The branch moves on and the mirror's checkout with it.
    _push(env["seed"], {"sine-starter/index.html": "<html>v2</html>"})
    with mp._mirror_lock():
        mp._ensure_mirror(fetch=True)
    assert open(os.path.join(mp.mirror_dir(), "sine-starter", "index.html")).read() \
        == "<html>v2</html>"
    res = mp.install_app("sine-starter", snap)
    dest = res["path"]
    assert open(os.path.join(dest, "index.html")).read() == "<html>v1</html>"
    assert res["stamp"]["commit"] == snap["commit"]
    assert res["stamp"]["tree_sha"] == snap["apps"]["sine-starter"]["tree_sha"]
    assert os.path.isfile(os.path.join(mp.state_dir(), "mirror.lock"))


# --- 5: raw.githubusercontent paths are percent-encoded ----------------------

def test_raw_urls_are_percent_encoded(env, monkeypatch):
    odd = "img/my shot #1?.png"
    _api_cache(monkeypatch, [odd, "index.html", "metadata.json"], checked_at=time.time())
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return _Bytes(b"PNG"), None

    monkeypatch.setattr(mp, "_http_get", fake_get)
    assert mp.get_asset("hello", odd)[0] == b"PNG"
    assert seen == [f"{mp.RAW_BASE}/o/r/{SHA}/hello/img/my%20shot%20%231%3F.png"]

    # The installer's downloads too.
    import httpx

    requested = []

    def handler(request):
        requested.append(request.url.raw_path.decode())
        return httpx.Response(200, content=b"data")

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    snap = mp._read_cache()
    res = mp.install_app("hello", snap)
    assert f"/o/r/{SHA}/hello/img/my%20shot%20%231%3F.png" in requested
    assert open(os.path.join(res["path"], "img", "my shot #1?.png"), "rb").read() == b"data"


def test_install_fails_closed_when_the_catalog_commit_is_gone(env):
    # A catalog naming a commit the mirror can't get must not install the tip
    # instead — that would put code on disk the user never saw described.
    snap, _ = mp.get_snapshot()
    _push(env["seed"], {"sine-starter/index.html": "<html>v2</html>"})
    bogus = dict(snap, commit="f" * 40)
    with pytest.raises(mp.InstallError, match="refresh the Marketplace"):
        mp.install_app("sine-starter", bogus)
    assert not os.path.exists(os.path.join(mp.community_dir(), "sine-starter"))


def test_new_install_stamp_rides_in_with_the_payload(env, monkeypatch):
    # The stamp is in place the moment the folder appears: a crash right after
    # the rename leaves an app the Marketplace still owns (updatable), never an
    # "unowned" folder it refuses to touch.
    snap, _ = mp.get_snapshot()
    seen = {}
    real_rename = os.rename

    def spy(src, dst):
        if dst.endswith(os.path.join("community", "sine-starter")):
            seen["stamp_before_rename"] = mp.read_stamp(src)
        return real_rename(src, dst)

    monkeypatch.setattr(mp.os, "rename", spy)
    mp.install_app("sine-starter", snap)
    assert seen["stamp_before_rename"]["tree_sha"] == snap["apps"]["sine-starter"]["tree_sha"]
