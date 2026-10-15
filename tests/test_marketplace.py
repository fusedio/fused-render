"""The Marketplace backend (fused_render/marketplace.py + routers/marketplace.py).

No real network. The udf.ai API is faked at its one seam, `marketplace._udf_get`
(`FakeUdf` below answers each endpoint from an in-memory app table), and the
release download at `marketplace._download_package` (it serves an in-memory
tar.gz built per release, with real sha256s). The rows are shaped from the real
API's samples (`CATALOG_KEYS` etc. pin the field names) so the fake cannot
grow fields the real service lacks. `_udf_get` itself is tested against a
faked `_http_get`.

Covered: the catalog + categories + collections payload, the stale-cache
fallback, the search and detail routes, install and update (update keeps
`.venv`, `.fused` and the user's own files, prunes what the author dropped,
replaces what they changed), a release that is revoked / moved on / corrupt /
unsafe (traversal, symlink, extra or missing files, case collisions), the job
runner, the asset proxy, and the X-Fused guard on POST /install.
"""
import hashlib
import io
import json
import os
import shutil
import tarfile
import time
from urllib.parse import parse_qs, quote, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fused_render import marketplace as mp

#: Field names of the real `marketplace_catalog` row (from a live response).
CATALOG_KEYS = (
    "slug", "name", "description", "author", "tags", "category", "version",
    "requires_python", "min_fused_render", "ai_capabilities", "ai_models",
    "ai_model_param", "icon_url", "preview_url", "size_bytes", "file_count",
    "release_id", "commit", "source_url", "published_at", "updated_at", "featured",
    "catalog_version", "generated_at")
GH = "fusedio/fused-render-community-apps"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_tar(files: dict, extra=()) -> bytes:
    """A release package: members at the archive root, regular files only (the
    real layout). `extra` are ready-made TarInfo/bytes pairs for hostile cases."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            ti = tarfile.TarInfo(path)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for ti, data in extra:
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


class FakeUdf:
    """The udf.ai API, answering `_udf_get(endpoint, **params)`."""

    def __init__(self):
        self.apps: dict[str, dict] = {}
        self.categories = [("starters", "Starters"), ("local-ai", "Local AI")]
        self.collections: list[dict] = []
        self.packages: dict[str, bytes] = {}   # package_url -> tar.gz bytes
        self.revoked: set[str] = set()
        self.calls: list[tuple] = []
        self.fail: str | None = None            # set → every call raises SourceError
        self.catalog_version = "cv1"
        self.release_override: dict = {}        # tweaks applied to marketplace_release rows

    # -- authoring ------------------------------------------------------
    def add_app(self, slug, files, version="1.0.0", category="starters", **row):
        self.set_files(slug, files, version, category=category, **row)

    def set_files(self, slug, files, version=None, **row):
        old = self.apps.get(slug, {})
        files = {p: (d.encode() if isinstance(d, str) else d) for p, d in files.items()}
        release_id = hashlib.sha1(json.dumps(sorted((p, _sha(d)) for p, d in files.items())).encode()
                                  + (version or old["row"]["version"]).encode()).hexdigest()
        commit = hashlib.sha1(release_id.encode()).hexdigest()
        base = f"https://raw.githubusercontent.com/{GH}/{commit}/{slug}/"
        r = {k: None for k in CATALOG_KEYS}
        r.update(old.get("row", {}))
        cat = row.get("category") or r["category"] or "starters"
        r.update(
            slug=slug, name=row.pop("name", None) or r["name"] or slug.replace("-", " ").title(),
            description=f"{slug} app", author={"name": "Tester", "github": None},
            tags=[cat], category=cat, version=version or r["version"],
            requires_python=False, ai_capabilities=[], ai_models=[],
            icon_url=base + "icon.svg" if "icon.svg" in files else None,
            preview_url=base + "preview.png" if "preview.png" in files else None,
            size_bytes=sum(len(d) for d in files.values()), file_count=len(files),
            release_id=release_id, commit=commit,
            source_url=f"https://github.com/{GH}/tree/{commit}/{slug}",
            published_at="2026-08-18T12:54:44Z", updated_at="2026-10-12T12:55:16Z",
            featured=r["featured"] or False, catalog_version=self.catalog_version,
            generated_at="2026-10-15T08:02:00Z")
        row.pop("category", None)
        r.update(row)
        url = f"https://s3.example/pkg/{release_id}.tar.gz"
        self.packages[url] = build_tar(files)
        self.apps[slug] = {"row": r, "files": files, "url": url}

    # -- the API ---------------------------------------------------------
    def __call__(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if self.fail:
            raise mp.SourceError(self.fail)
        return getattr(self, f"_{endpoint}")(**params)

    def _marketplace_catalog(self, client_version):
        assert client_version
        return [dict(a["row"]) for _s, a in sorted(self.apps.items())]

    def _marketplace_categories(self):
        return [{"id": i, "label": l, "order": n, "catalog_version": self.catalog_version,
                 "count": sum(a["row"]["category"] == i for a in self.apps.values())}
                for n, (i, l) in enumerate(self.categories)]

    def _marketplace_collections(self, new_and_updated_limit):
        return [dict(r, catalog_version=self.catalog_version) for r in self.collections]

    def _marketplace_search(self, q, category=None, tags=None, limit=24, cursor=None, client_version=None):
        hits = [a["row"] for _s, a in sorted(self.apps.items())
                if (not q or q.lower() in (a["row"]["name"] + a["row"]["description"]).lower())
                and (not category or a["row"]["category"] == category)]
        start = int(cursor or 0)
        page = hits[start:start + limit]
        nxt = str(start + limit) if start + limit < len(hits) else None
        return [dict(r, rank=start + i + 1, score=1.5, next_cursor=nxt,
                     highlights=[{"field": "name", "snippet": f"<b>{q}</b>"}] if q else [])
                for i, r in enumerate(page)]

    def _marketplace_app_detail(self, slug, client_version=None):
        if slug not in self.apps:
            raise mp.SourceError(f'The app "{slug}" isn\'t in the marketplace.', "app_not_found")
        a = self.apps[slug]
        return [dict(a["row"], readme_markdown=a["files"].get("readme.md", b"").decode(),
                     readme_base_url=a["row"]["source_url"].replace("github.com", "raw.githubusercontent.com")
                     .replace("/tree", "") + "/",
                     files=[{"path": p, "size": len(d)} for p, d in sorted(a["files"].items())],
                     releases=[{"release_id": a["row"]["release_id"], "version": a["row"]["version"],
                                "changelog": "x", "published_at": a["row"]["published_at"],
                                "revoked": a["row"]["release_id"] in self.revoked}])]

    def _marketplace_release(self, slug, release_id):
        if slug not in self.apps:
            raise mp.SourceError("no such app", "app_not_found")
        a = self.apps[slug]
        pkg = self.packages[a["url"]]
        rid = a["row"]["release_id"]
        row = {"slug": slug, "release_id": rid, "version": a["row"]["version"],
               "commit": a["row"]["commit"],
               "files": [{"path": p, "sha256": _sha(d), "size": len(d)}
                         for p, d in sorted(a["files"].items())],
               "package_url": a["url"], "package_sha256": _sha(pkg), "package_size": len(pkg),
               "revoked": rid in self.revoked}
        if release_id and release_id != rid:
            row["release_id"] = release_id  # an older release id still resolves to its own row
        row.update(self.release_override)
        return [row]

    # -- the download ------------------------------------------------------
    def download(self, url, dest, progress, max_bytes):
        data = self.packages[url]
        with open(dest, "wb") as f:
            f.write(data)
        progress(len(data))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated state dir + workspace and a FakeUdf wired in: local-chat
    (local-ai) and sine-starter (starters)."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("FUSED_RENDER_MARKETPLACE_API", raising=False)
    api = FakeUdf()
    api.add_app("local-chat", {"index.html": "<html>chat</html>", "readme.md": "# Local Chat\n\n![shot](preview.png)\n",
                               "preview.png": b"PNGDATA", "icon.svg": "<svg/>"},
                version="1.2.0", category="local-ai", featured=True)
    api.add_app("sine-starter", {"index.html": "<html>v1</html>", "sine.py": "print(1)\n",
                                 "old.txt": "author file\n", "lib/helper.js": "// v1\n"})
    api.collections = [
        {"collection": "shelf", "shelf_id": "starters", "title": "Starters", "position": 0, "slug": "sine-starter"},
        {"collection": "shelf", "shelf_id": "local-ai", "title": "Local AI", "position": 1, "slug": "local-chat"},
        {"collection": "shelf", "shelf_id": "local-ai", "title": "Local AI", "position": 0, "slug": "ghost-app"},
        {"collection": "new_and_updated", "shelf_id": None, "title": "New and updated", "position": 0, "slug": "local-chat"},
        {"collection": "new_and_updated", "shelf_id": None, "title": "New and updated", "position": 1, "slug": "sine-starter"},
    ]
    monkeypatch.setattr(mp, "_udf_get", api)
    monkeypatch.setattr(mp, "_download_package", api.download)
    # Tests refresh back to back; the once-a-minute limit has its own test.
    monkeypatch.setattr(mp, "REFRESH_MIN_INTERVAL_S", 0)
    raw = {}

    def fake_http_get(url, **kw):
        raw.setdefault("urls", []).append(url)
        for a in api.apps.values():
            base = a["row"]["source_url"].replace("github.com", "raw.githubusercontent.com").replace("/tree", "") + "/"
            if url.startswith(base) and url[len(base):] in a["files"]:
                return _Bytes(a["files"][url[len(base):]]), None
        return None, "not found (404)"

    monkeypatch.setattr(mp, "_http_get", fake_http_get)
    with mp._JOBS_LOCK:
        mp._JOBS.clear()
        mp._JOB_FINISHED.clear()
    return {"home": tmp_path / "home", "ws": tmp_path / "ws", "api": api, "raw": raw}


class _Bytes:
    def __init__(self, content):
        self.content = content


def _write(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)


def _client():
    from fused_render.server.routers.marketplace import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _entry(slug, refresh=False):
    return next(a for a in mp.catalog(refresh=refresh)["apps"] if a["slug"] == slug)


def _dest(env, slug="sine-starter"):
    return os.path.join(str(env["ws"]), "community", slug)


# --- _udf_get: the HTTP seam ---------------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.headers = body, status, {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_udf_get_adds_format_json_and_reads_rows(monkeypatch):
    seen = {}

    def fake(url, **kw):
        seen["url"] = url
        return _Resp([{"a": 1}]), None

    monkeypatch.setattr(mp, "_http_get", fake)
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_API", "https://api.test/base/")
    assert mp._udf_get("marketplace_release", slug="x", release_id="", q=None) == [{"a": 1}]
    u = urlparse(seen["url"])
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://api.test/base/marketplace_release"
    # format=json always; an empty value is kept ("latest"); None is dropped.
    assert parse_qs(u.query, keep_blank_values=True) == {"slug": ["x"], "release_id": [""], "format": ["json"]}


def test_udf_get_error_row_transport_and_shape(monkeypatch):
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (_Resp(
        [{"error_code": "app_not_found", "error_message": "no such app"}]), None))
    with pytest.raises(mp.SourceError, match="no such app") as ei:
        mp._udf_get("marketplace_app_detail", slug="x")
    assert ei.value.error_code == "app_not_found"
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (None, "could not reach the marketplace (X)"))
    with pytest.raises(mp.SourceError, match="could not reach"):
        mp._udf_get("marketplace_catalog")
    for body in ({"not": "a list"}, ["not-a-dict"], ValueError("html")):
        monkeypatch.setattr(mp, "_http_get", lambda url, b=body, **kw: (_Resp(b), None))
        with pytest.raises(mp.SourceError, match="unexpected"):
            mp._udf_get("marketplace_catalog")


# --- catalog -------------------------------------------------------------------

def test_catalog_payload(env):
    cat = mp.catalog()
    assert cat["status"] == "ok" and cat["source"] == "udf-api" and cat["stale"] is False
    assert cat["api"] == mp.API_BASE and cat["catalog_version"] == "cv1"
    assert cat["generated_at"] == "2026-10-15T08:02:00Z"
    assert [a["slug"] for a in cat["apps"]] == ["local-chat", "sine-starter"]
    # Categories come from the API, in its order, with its counts.
    assert cat["categories"] == [{"id": "starters", "label": "Starters", "count": 1},
                                 {"id": "local-ai", "label": "Local AI", "count": 1}]
    # Collections: grouped, ordered by position, groups by first appearance,
    # slugs the catalog lacks dropped.
    assert cat["collections"] == [
        {"id": "starters", "kind": "shelf", "title": "Starters", "slugs": ["sine-starter"]},
        {"id": "local-ai", "kind": "shelf", "title": "Local AI", "slugs": ["local-chat"]},
        {"id": "new-and-updated", "kind": "new_and_updated", "title": "New and updated",
         "slugs": ["local-chat", "sine-starter"]},
    ]
    chat = cat["apps"][0]
    row = env["api"].apps["local-chat"]["row"]
    assert chat["name"] == "Local Chat" and chat["version"] == "1.2.0" and chat["featured"] is True
    assert "tree_sha" not in chat
    assert (chat["release_id"], chat["commit"]) == (row["release_id"], row["commit"])
    assert chat["published_at"] == row["published_at"] and chat["updated_at"] == row["updated_at"]
    assert chat["source_url"] == row["source_url"]
    v = row["release_id"][:12]
    assert chat["icon_url"] == f"/api/marketplace/asset/local-chat/icon.svg?v={v}"
    assert chat["preview_url"] == f"/api/marketplace/asset/local-chat/preview.png?v={v}"
    assert chat["installed"] is None and chat["update_available"] is False and chat["compatible"] is True
    assert chat["file_count"] == 4 and chat["size_bytes"] == row["size_bytes"]
    sine = cat["apps"][1]
    assert sine["featured"] is False and sine["icon_url"] is None and sine["preview_url"] is None
    # Cached: catalog.json exists and a second call doesn't rebuild.
    assert os.path.isfile(mp.catalog_path())
    n = len(env["api"].calls)
    assert mp.catalog()["fetched_at"] == cat["fetched_at"] and len(env["api"].calls) == n
    assert {c[0] for c in env["api"].calls} == {"marketplace_catalog", "marketplace_categories",
                                                "marketplace_collections"}


def test_incompatible_app_is_flagged(env):
    env["api"].add_app("future-app", {"index.html": "x"}, min_fused_render="999.0.0")
    assert _entry("future-app")["compatible"] is False


def test_snapshot_written_by_the_old_backend_is_no_cache(env):
    mp._write_cache({"source": "github-api", "commit": "c" * 40, "apps": {"x": {"meta": {}}},
                     "fetched_at": "then", "checked_at": time.time()})
    assert mp._read_cache() is None
    assert [a["slug"] for a in mp.catalog()["apps"]] == ["local-chat", "sine-starter"]
    # ...and a cache for another API base is not trusted either.
    assert mp._read_cache() is not None
    os.environ["FUSED_RENDER_MARKETPLACE_API"] = "https://other.test"
    try:
        assert mp._read_cache() is None
    finally:
        del os.environ["FUSED_RENDER_MARKETPLACE_API"]


def test_stale_cache_fallback(env):
    cat = mp.catalog()
    assert cat["stale"] is False
    env["api"].fail = "could not reach the marketplace (ConnectError)"
    cat2 = mp.catalog(refresh=True)
    assert cat2["status"] == "ok" and cat2["stale"] is True
    assert "could not reach" in cat2["warning"]
    assert [a["slug"] for a in cat2["apps"]] == ["local-chat", "sine-starter"]
    # No cache at all + failure → an error payload, never a raise.
    shutil.rmtree(mp.state_dir())
    err = mp.catalog(refresh=True)
    assert err["status"] == "error" and "could not reach" in err["message"]
    assert err["apps"] == [] and err["collections"] == []
    # Recovery clears the flag.
    env["api"].fail = None
    assert mp.catalog(refresh=True)["stale"] is False


def test_a_new_catalog_version_prunes_unreferenced_asset_caches(env):
    mp.catalog()
    mp.get_asset("local-chat", "icon.svg")
    root = os.path.join(mp.state_dir(), "assets")
    old = env["api"].apps["local-chat"]["row"]["release_id"]
    assert os.listdir(root) == [old]
    env["api"].catalog_version = "cv2"
    env["api"].set_files("local-chat", {"index.html": "<html>2</html>", "icon.svg": "<svg2/>"}, "1.3.0")
    mp.catalog(refresh=True)
    assert not os.path.exists(os.path.join(root, old))


# --- search --------------------------------------------------------------------

def test_search_route(env):
    client = _client()
    r = client.get("/api/marketplace/search", params={"q": "chat"}).json()
    assert r["status"] == "ok" and r["next_cursor"] is None
    [hit] = r["apps"]
    assert hit["slug"] == "local-chat" and hit["score"] == 1.5
    assert hit["highlights"] == [{"field": "name", "snippet": "<b>chat</b>"}]
    assert hit["icon_url"].startswith("/api/marketplace/asset/local-chat/icon.svg?v=")
    assert env["api"].calls[-1][1]["client_version"]
    # Paging + category: the cursor round-trips; empty cursor/category are not sent.
    r = client.get("/api/marketplace/search", params={"q": "app", "limit": 1}).json()
    assert [a["slug"] for a in r["apps"]] == ["local-chat"] and r["next_cursor"] == "1"
    assert env["api"].calls[-1][1]["cursor"] is None and env["api"].calls[-1][1]["category"] is None
    r = client.get("/api/marketplace/search", params={"q": "app", "limit": 1, "cursor": "1"}).json()
    assert [a["slug"] for a in r["apps"]] == ["sine-starter"] and r["next_cursor"] is None
    r = client.get("/api/marketplace/search", params={"q": "app", "category": "starters"}).json()
    assert [a["slug"] for a in r["apps"]] == ["sine-starter"]
    # The limit is clamped.
    client.get("/api/marketplace/search", params={"q": "x", "limit": 9999})
    assert env["api"].calls[-1][1]["limit"] == 100


def test_search_merges_installed_and_reports_errors_as_200(env):
    mp.install_app("sine-starter", mp.get_snapshot()[0])
    [hit] = _client().get("/api/marketplace/search", params={"q": "sine"}).json()["apps"]
    assert hit["installed"]["release_id"] == env["api"].apps["sine-starter"]["row"]["release_id"]
    assert hit["update_available"] is False
    env["api"].fail = "the marketplace answered 502"
    r = _client().get("/api/marketplace/search", params={"q": "sine"})
    assert r.status_code == 200 and r.json() == {"status": "error", "message": "the marketplace answered 502"}


# --- detail --------------------------------------------------------------------

def test_detail_found_not_found_and_installed_only(env):
    client = _client()
    d = client.get("/api/marketplace/app/local-chat").json()
    assert d["status"] == "ok" and d["app"]["slug"] == "local-chat"
    assert d["readme"].startswith("# Local Chat")
    assert d["readme_base"] == "/api/marketplace/asset/local-chat/"
    assert {"path": "preview.png", "size": 7} in d["files"]
    assert d["releases"] == [{"release_id": env["api"].apps["local-chat"]["row"]["release_id"],
                              "version": "1.2.0", "changelog": "x",
                              "published_at": "2026-08-18T12:54:44Z", "revoked": False}]
    r = client.get("/api/marketplace/app/nope-nope")
    assert r.status_code == 404 and r.json()["status"] == "error"
    assert client.get("/api/marketplace/app/..%2Fx").status_code == 404
    # Installed, then dropped from the API: app_not_found falls through to the local copy.
    mp.install_app("sine-starter", mp.get_snapshot()[0])
    _write(_dest(env), "readme.md", "# my readme")
    del env["api"].apps["sine-starter"]
    d = client.get("/api/marketplace/app/sine-starter").json()
    assert d["status"] == "ok" and d["readme"] == "# my readme"
    assert d["app"]["installed"]["version"] == "1.0.0" and d["app"]["update_available"] is False
    assert {"path": "sine.py", "size": 0} in d["files"] and d["releases"] == []


def test_detail_falls_back_to_the_snapshot_when_the_api_is_down(env):
    mp.catalog()
    env["api"].fail = "the marketplace answered 502"
    d = mp.app_detail("local-chat")
    assert d["status"] == "ok" and d["app"]["slug"] == "local-chat"
    assert d["readme"] == "" and d["files"] == [] and d["releases"] == []
    assert mp.app_detail("unknown-app") is None


def test_readme_is_capped(env, monkeypatch):
    monkeypatch.setattr(mp, "MAX_README_BYTES", 10)
    assert mp.app_detail("local-chat")["readme"] == "# Local Ch"


# --- install / update ------------------------------------------------------------

def test_install_then_update_keeps_venv_and_user_files(env):
    api = env["api"]
    snap, _ = mp.get_snapshot()
    res = mp.install_app("sine-starter", snap)
    dest = _dest(env)
    assert res["action"] == "install" and res["path"] == dest
    assert open(os.path.join(dest, "index.html")).read() == "<html>v1</html>"
    row = api.apps["sine-starter"]["row"]
    stamp = mp.read_stamp(dest)
    assert stamp["slug"] == "sine-starter" and stamp["version"] == "1.0.0"
    assert stamp["source"] == "udf-api" and stamp["api"] == mp.API_BASE
    assert (stamp["release_id"], stamp["commit"]) == (row["release_id"], row["commit"])
    assert "tree_sha" not in stamp
    assert stamp["files"] == {p: _sha(d) for p, d in api.apps["sine-starter"]["files"].items()}
    # No staging left behind in community/.
    assert sorted(os.listdir(os.path.dirname(dest))) == ["sine-starter"]
    entry = _entry("sine-starter")
    assert entry["installed"]["modified"] is False
    assert entry["installed"]["release_id"] == row["release_id"]
    assert entry["update_available"] is False

    # The user works in their copy.
    os.makedirs(os.path.join(dest, ".venv", "bin"))
    _write(dest, ".venv/bin/python", "venv")
    _write(dest, ".fused/data/state.db", "user state")
    _write(dest, "notes.txt", "user notes")
    _write(dest, "sine.py", "print('edited')\n")
    assert _entry("sine-starter")["installed"]["modified"] is True

    # The author ships v2: changes index.html, deletes old.txt.
    api.set_files("sine-starter", {"index.html": "<html>v2</html>", "sine.py": "print(1)\n",
                                   "lib/helper.js": "// v1\n"}, "2.0.0")
    entry = _entry("sine-starter", refresh=True)
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
    entry = _entry("sine-starter")
    assert entry["update_available"] is False and entry["installed"]["version"] == "2.0.0"
    assert [a["slug"] for a in mp.installed_list()["apps"]] == ["sine-starter"]


def test_update_keeps_an_author_deleted_file_the_user_edited(env):
    mp.install_app("sine-starter", mp.get_snapshot()[0])
    dest = _dest(env)
    _write(dest, "old.txt", "I edited this")
    env["api"].set_files("sine-starter", {"index.html": "<html>v2</html>"}, "2.0.0")
    res = mp.install_app("sine-starter", mp.get_snapshot(refresh=True)[0])
    assert "old.txt" not in res["pruned"] and open(os.path.join(dest, "old.txt")).read() == "I edited this"


def test_a_stamp_with_only_a_tree_sha_reads_as_update_available(env):
    mp.install_app("sine-starter", mp.get_snapshot()[0])
    dest = _dest(env)
    stamp = mp.read_stamp(dest)
    old = {k: v for k, v in stamp.items() if k not in ("release_id", "api")}
    old.update(tree_sha="a" * 40, source="github-api", repo="o/r")
    mp._write_stamp(dest, old)
    entry = _entry("sine-starter")
    assert entry["update_available"] is True and entry["installed"]["release_id"] is None
    # Not-installed stays not-updatable, and a local-only entry never claims an update.
    assert _entry("local-chat")["update_available"] is False
    del env["api"].apps["sine-starter"]
    [local] = mp.installed_list(
    )["apps"] if mp.get_snapshot(refresh=True)[0] else []
    assert local["update_available"] is False and local["version"] == "1.0.0"


def test_install_refuses_a_foreign_folder(env):
    dest = _dest(env)
    _write(dest, "mine.txt", "not from the marketplace")
    with pytest.raises(mp.InstallError, match="was not installed from the Marketplace"):
        mp.install_app("sine-starter", mp.get_snapshot()[0])
    assert os.listdir(dest) == ["mine.txt"]


def test_husk_is_not_installed_but_install_reuses_its_data(env):
    snap, _ = mp.get_snapshot()
    mp.install_app("sine-starter", snap)
    dest = _dest(env)
    _write(dest, ".fused/data/state.db", "user state")
    # What routers/app_uninstall._strip_app_files leaves: only `.fused/`
    # (the marketplace stamp inside it survives).
    for name in os.listdir(dest):
        if name != ".fused":
            p = os.path.join(dest, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    assert mp.read_stamp(dest) is not None and mp._is_husk(dest)
    assert mp.installed_apps() == {}
    entry = _entry("sine-starter")
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
    assert seen["stamp_before_rename"]["release_id"] == snap["apps"]["sine-starter"]["release_id"]


def test_install_asks_for_the_snapshot_release_and_reports_package_size(env):
    snap, _ = mp.get_snapshot()
    reports = []
    mp.install_app("sine-starter", snap, lambda **kw: reports.append(kw))
    endpoint, params = env["api"].calls[-1]
    assert endpoint == "marketplace_release"
    assert params == {"slug": "sine-starter", "release_id": snap["apps"]["sine-starter"]["release_id"]}
    size = len(env["api"].packages[env["api"].apps["sine-starter"]["url"]])
    assert {"bytes_total": size, "bytes_done": 0} in reports
    assert max(r["bytes_done"] for r in reports if "bytes_done" in r) == size


# --- install refusals ------------------------------------------------------------

def _install_error(env, match):
    with pytest.raises(mp.InstallError, match=match):
        mp.install_app("sine-starter", mp.get_snapshot()[0])
    # A refused install leaves nothing behind: no app folder, no staging dir.
    root = os.path.join(str(env["ws"]), "community")
    assert os.listdir(root) == []


def test_revoked_release_is_refused(env):
    snap, _ = mp.get_snapshot()
    env["api"].revoked.add(snap["apps"]["sine-starter"]["release_id"])
    with pytest.raises(mp.InstallError, match="This release was withdrawn — refresh the Marketplace"):
        mp.install_app("sine-starter", snap)


def test_release_moved_on_since_the_page_loaded_is_refused(env):
    snap, _ = mp.get_snapshot()
    env["api"].release_override = {"release_id": "f" * 40}
    with pytest.raises(mp.InstallError, match="the marketplace changed since this page loaded"):
        mp.install_app("sine-starter", snap)


def test_api_error_on_release_is_an_install_error(env):
    snap, _ = mp.get_snapshot()
    env["api"].fail = "the marketplace answered 502"
    with pytest.raises(mp.InstallError, match="502"):
        mp.install_app("sine-starter", snap)


def test_package_checksum_mismatch_is_refused(env):
    pkg = env["api"].apps["sine-starter"]["url"]
    data = bytearray(env["api"].packages[pkg])
    data[-1] ^= 0xFF  # same size, different bytes
    env["api"].packages[pkg] = bytes(data)
    env["api"].release_override = {}
    # The release advertises the CORRUPT bytes' hash by default; pin the original.
    good = env["api"]._marketplace_release
    orig_sha = _sha(build_tar(env["api"].apps["sine-starter"]["files"]))
    env["api"].release_override = {"package_sha256": orig_sha, "package_size": len(data)}
    _install_error(env, "integrity check")


def test_package_size_mismatch_is_refused(env):
    a = env["api"]
    a.release_override = {"package_size": len(a.packages[a.apps["sine-starter"]["url"]]) + 5}
    with pytest.raises(mp.InstallError, match="(wrong size|larger)|size"):
        mp.install_app("sine-starter", mp.get_snapshot()[0])


def test_file_content_not_matching_the_release_is_refused(env):
    a = env["api"]
    url = a.apps["sine-starter"]["url"]
    files = dict(a.apps["sine-starter"]["files"], **{"index.html": b"<html>EVIL</html>"})
    a.packages[url] = build_tar(files)  # package hashes itself consistently; file list says otherwise
    _install_error(env, "index.html does not match the release \\((checksum|size)\\)")


def _swap_package(env, files, extra=()):
    a = env["api"]
    url = a.apps["sine-starter"]["url"]
    a.packages[url] = build_tar(files, extra)


def test_extra_file_in_the_package_is_refused(env):
    files = dict(env["api"].apps["sine-starter"]["files"], **{"extra.txt": b"x"})
    _swap_package(env, files)
    _install_error(env, "extra.txt")


def test_missing_file_in_the_package_is_refused(env):
    files = {p: d for p, d in env["api"].apps["sine-starter"]["files"].items() if p != "sine.py"}
    _swap_package(env, files)
    _install_error(env, "missing sine.py")


def _hostile(env, make):
    """A package whose release list names the member (so only the member's own
    shape can stop it)."""
    ti, data, name = make()
    a = env["api"]
    files = dict(a.apps["sine-starter"]["files"])
    a.packages[a.apps["sine-starter"]["url"]] = build_tar(files, [(ti, data)])
    if name and ".." not in name and not name.startswith("/"):
        a.apps["sine-starter"]["files"][name] = data or b""


def _member(name, type_=tarfile.REGTYPE, linkname="", data=b"x"):
    ti = tarfile.TarInfo(name)
    ti.type = type_
    ti.linkname = linkname
    ti.size = len(data) if type_ == tarfile.REGTYPE else 0
    return ti, (data if type_ == tarfile.REGTYPE else None), name


@pytest.mark.parametrize("make,match", [
    (lambda: _member("../escape.txt"), "unsafe path"),
    (lambda: _member("/abs/escape.txt"), "unsafe path"),
    (lambda: _member("a/../../escape.txt"), "unsafe path"),
    (lambda: _member("link", tarfile.SYMTYPE, "/etc/passwd"), "not a plain file"),
    (lambda: _member("hard", tarfile.LNKTYPE, "index.html"), "not a plain file"),
    (lambda: _member("dev", tarfile.CHRTYPE), "not a plain file"),
    (lambda: _member("fifo", tarfile.FIFOTYPE), "not a plain file"),
])
def test_hostile_package_members_are_refused(env, make, match):
    _hostile(env, make)
    _install_error(env, match)
    assert not os.path.exists(os.path.join(str(env["ws"]), "escape.txt"))


def test_duplicate_member_is_refused(env):
    a = env["api"]
    ti = tarfile.TarInfo("index.html")
    ti.size = 3
    a.packages[a.apps["sine-starter"]["url"]] = build_tar(a.apps["sine-starter"]["files"], [(ti, b"abc")])
    _install_error(env, "twice|does not match")


def test_garbage_package_is_refused(env):
    a = env["api"]
    url = a.apps["sine-starter"]["url"]
    a.packages[url] = b"this is not a tarball"
    _install_error(env, "not a valid archive")


def test_case_collision_in_the_release_is_refused(env):
    env["api"].set_files("sine-starter", {"README.md": "a", "readme.md": "b"})
    with pytest.raises(mp.InstallError, match="differ only by upper/lower case"):
        mp.install_app("sine-starter", mp.get_snapshot(refresh=True)[0])


def test_keep_names_in_the_release_are_verified_but_never_written(env):
    env["api"].set_files("sine-starter", {"index.html": "ok", ".git/config": "evil", ".FUSED/x": "evil"})
    mp.install_app("sine-starter", mp.get_snapshot(refresh=True)[0])
    dest = _dest(env)
    assert sorted(os.listdir(dest)) == [".fused", "index.html"]
    assert not os.path.exists(os.path.join(dest, ".git"))
    assert set(mp.read_stamp(dest)["files"]) == {"index.html"}


def test_wanted_skips_keep_names_case_insensitively():
    files = [{"path": p, "size": 1} for p in
             (".FUSED/x", ".Venv/bin/python", ".GIT/config", ".fused/y", "index.html",
              "docs/.git/x")]
    assert [f["path"] for f in mp._wanted(files)] == ["index.html", "docs/.git/x"]


def test_case_fold_collisions_helper():
    def paths(*ps):
        return [{"path": p, "size": 1} for p in ps]

    assert mp._case_collisions(paths("README.md", "lib/a.js", "lib/b.js")) == []
    assert mp._case_collisions(paths("README.md", "readme.md")) == ["README.md / readme.md"]
    assert mp._case_collisions(paths("Lib/a.js", "lib/b.js")) == ["Lib / lib"]


def test_download_package_streams_and_enforces_the_cap(tmp_path, monkeypatch):
    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, n):
            yield b"a" * 10
            yield b"b" * 10

    monkeypatch.setattr(mp.httpx, "stream", lambda *a, **kw: Resp())
    seen = []
    dest = str(tmp_path / "p.tgz")
    mp._download_package("https://s3.example/p", dest, seen.append, 20)
    assert open(dest, "rb").read() == b"a" * 10 + b"b" * 10 and seen == [10, 10]
    with pytest.raises(mp.InstallError, match="larger than announced"):
        mp._download_package("https://s3.example/p", dest, seen.append, 15)


# --- jobs (the route) -------------------------------------------------------------

def _wait_job(client, slug):
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get("/api/marketplace/jobs").json()["jobs"][slug]
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    return job


def test_install_job_runs_in_background_and_is_idempotent(env):
    client = _client()
    # The X-Fused guard.
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"})
    assert r.status_code == 403
    r = client.post("/api/marketplace/install", json={"slug": "../x"}, headers={"X-Fused": "1"})
    assert r.status_code == 400 and r.json()["status"] == "error"
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers={"X-Fused": "1"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    job = r.json()["job"]
    assert job["slug"] == "sine-starter" and job["action"] == "install"
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done", job
    assert job["progress"] == 1.0 and job["bytes_done"] == job["bytes_total"]
    assert job["path"].endswith("/community/sine-starter")
    installed = client.get("/api/marketplace/installed").json()["apps"]
    assert [a["slug"] for a in installed] == ["sine-starter"]
    # A second POST after it finished is an update.
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers={"X-Fused": "1"})
    assert r.json()["job"]["action"] == "update"
    assert _wait_job(client, "sine-starter")["state"] == "done"


def test_install_job_failure_surfaces_on_the_job(env):
    client = _client()
    env["api"].revoked.add(env["api"].apps["sine-starter"]["row"]["release_id"])
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers={"X-Fused": "1"})
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "error" and "withdrawn" in job["error"]
    r = client.post("/api/marketplace/install", json={"slug": "ghost-app"}, headers={"X-Fused": "1"})
    assert r.status_code == 400 and "no app named" in r.json()["message"]


# --- assets ----------------------------------------------------------------------

def test_asset_proxy_routes_and_caching(env):
    client = _client()
    r = client.get("/api/marketplace/asset/local-chat/icon.svg")
    assert r.status_code == 200 and r.content == b"<svg/>"
    assert r.headers["content-type"] == "image/svg+xml"
    # Unversioned (readme-relative) URLs revalidate; the catalog's own
    # icon/preview URLs carry ?v=<release id> and cache for a day.
    assert r.headers["cache-control"] == "no-cache"
    entry = next(a for a in client.get("/api/marketplace/catalog").json()["apps"] if a["slug"] == "local-chat")
    r2 = client.get(entry["icon_url"])
    assert r2.status_code == 200 and r2.headers["cache-control"] == "max-age=86400"
    # A `v` that is not the app's release (a readme's own `?v=1`) earns nothing.
    r3 = client.get("/api/marketplace/asset/local-chat/icon.svg?v=1")
    assert r3.status_code == 200 and r3.headers["cache-control"] == "no-cache"
    data, _ctype, version = mp.get_asset_versioned("local-chat", "icon.svg")
    assert data == b"<svg/>" and entry["icon_url"].endswith(f"?v={version}")
    # Fetched from the app's own pinned raw base, once; then served from disk.
    row = env["api"].apps["local-chat"]["row"]
    base = f"https://raw.githubusercontent.com/{GH}/{row['commit']}/local-chat/"
    assert set(env["raw"]["urls"]) == {base + "icon.svg"}
    assert os.path.isfile(os.path.join(mp.state_dir(), "assets", row["release_id"], "local-chat", "icon.svg"))
    # A readme image resolves against readme_base.
    assert client.get("/api/marketplace/asset/local-chat/preview.png").content == b"PNGDATA"
    assert client.get("/api/marketplace/asset/local-chat/missing.png").status_code == 404


@pytest.mark.parametrize("path", [
    "../sine-starter/index.html", "a/../../etc/passwd", "%2e%2e/README.md",
    "/etc/passwd", ".git/../../x", "",
])
def test_asset_path_traversal_rejected(env, path):
    assert mp.get_asset("local-chat", path) is None
    assert mp.get_asset("../local-chat", "icon.svg") is None
    assert mp.get_asset("local-chat", "../README.md") is None
    # Percent-encoded so the HTTP client cannot normalise the `..` away
    # before it reaches the route (the server decodes it back).
    r = _client().get(f"/api/marketplace/asset/local-chat/{quote(path, safe='')}")
    assert r.status_code == 404
    assert not any("../" in u or "/etc/" in u for u in env["raw"].get("urls", []))


def test_asset_for_an_app_without_a_github_base_is_404(env):
    env["api"].apps["local-chat"]["row"]["source_url"] = "https://example.com/elsewhere"
    env["api"].apps["local-chat"]["row"]["icon_url"] = "https://example.com/icon.svg"
    assert mp.get_asset("local-chat", "icon.svg") is None
    assert _entry("local-chat")["icon_url"] is None


# --- only /catalog rebuilds a stale cache; nobody queues behind a build -------------

def _old_cache(env):
    mp.catalog()
    cache = mp._read_cache()
    cache["checked_at"] = 0  # ancient
    mp._write_cache(cache)


def test_stale_cache_readers_never_call_the_builder(env, monkeypatch):
    _old_cache(env)
    built = []
    monkeypatch.setattr(mp, "_build_snapshot", lambda: built.append(1) or 1 / 0)
    assert mp.get_asset("local-chat", "icon.svg") == (b"<svg/>", "image/svg+xml")
    assert mp.app_detail("local-chat")["app"]["slug"] == "local-chat"
    assert mp.installed_list() == {"apps": []}
    assert built == []
    # The route too.
    assert _client().get("/api/marketplace/asset/local-chat/icon.svg").status_code == 200
    assert built == []
    # /catalog is the one that rebuilds (here it crashes → stale cache).
    cat = mp.catalog()
    assert built == [1] and cat["status"] == "ok" and cat["stale"] is True


def test_catalog_does_not_wait_behind_a_running_rebuild(env, monkeypatch):
    _old_cache(env)
    monkeypatch.setattr(mp, "_build_snapshot", lambda: pytest.fail("built while locked"))
    assert mp._CATALOG_LOCK.acquire(timeout=1)  # "another request is rebuilding"
    try:
        t0 = time.monotonic()
        cat = mp.catalog()
        assert cat["status"] == "ok" and len(cat["apps"]) == 2
        assert mp.get_asset("local-chat", "icon.svg") is not None
        assert time.monotonic() - t0 < 2
    finally:
        mp._CATALOG_LOCK.release()


def test_crashing_build_backs_off_and_refresh_is_rate_limited(env, monkeypatch):
    monkeypatch.setattr(mp, "REFRESH_MIN_INTERVAL_S", 60)
    _old_cache(env)
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


def test_old_github_backend_is_gone():
    for name in ("SOURCE_GIT", "SOURCE_API", "DEFAULT_FEATURED", "CATEGORY_ORDER", "repo_config",
                 "forced_source", "mirror_dir", "_ls_tree", "_raw_url", "_download_api",
                 "_copy_from_mirror", "_write_blobs", "_git", "_git_out", "_mirror_lock"):
        assert not hasattr(mp, name), name
