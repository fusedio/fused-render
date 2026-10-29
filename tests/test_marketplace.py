"""The Marketplace backend (fused_render/marketplace.py + routers/marketplace.py),
the reviewed marketplace of docs/CONTRIB_LOOP.md §18.5.

No real network. The backend is faked at its one seam, `marketplace._udf_get`
(`FakeUdf` below answers each endpoint from an in-memory app table shaped
like §18.3's rows: `repo, branch, commit, release_id == commit, releases`…;
`CATALOG_KEYS` pins the field names so the fake cannot grow fields the
backend lacks). Raw GitHub (the asset proxy) is faked at `_http_get`. The
install path is git-real: the author's repo lives on a FakeGitHub
(contrib/gh.py, `FUSED_RENDER_CONTRIB_FAKE_GH`, every `https://github.com/
<o>/<r>` rewritten to its bare repos), and an install is a real clone at the
row's approved commit placed by the GitHub link installer (the real
`appfile_git.clone(commit=)` / `install(kind="marketplace", market=)`). The
engine's moves
(`engine.bring_in`, `market.switch_to_reviewed` / `adopt_legacy`,
`blocks.*`) belong to contrib's own tests (test_contrib_market/blocks.py);
here they are stubbed at their seams to prove this module routes to them.

Covered: the catalog payload with the §18.3 fields, the asset base off
`repo` + `commit` (and the old backend's form), the stale-cache fallback,
search and detail, `installed` from marketplace records and legacy stamps,
the install job (a real clone pinned to the approved commit, the author's
newer tip NOT installed), update → bring_in, the switch question and switch,
the refusals (legacy, no source, an unrecorded clone), adopt, check, the
block list (TTL, offline, fail-closed parsing), the open gate on /render and
/api/apps/entry, the bots' `_market_target`, the deep link, the Showcase
retirement, and that the package path is gone.
"""
import inspect
import json
import os
import shutil
import sys
import time
import types
from urllib.parse import parse_qs, quote, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fused_render import marketplace as mp

#: Field names of a §18.3 `marketplace_catalog` row.
CATALOG_KEYS = (
    "slug", "name", "description", "author", "tags", "category", "version",
    "requires_python", "min_fused_render", "ai_capabilities", "ai_models",
    "ai_model_param", "icon_url", "preview_url", "size_bytes", "file_count",
    "release_id", "commit", "repo", "branch", "source_url", "published_at", "updated_at",
    "featured", "remix_of", "remix_count", "contributor_count", "last_release_at",
    "releases", "installs", "installs_30d", "views", "views_30d", "catalog_version", "generated_at")
OWNER = "alice"
FUSED = {"X-Fused": "1"}
_REAL_UDF_GET = mp._udf_get

def _sha1(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()


class FakeUdf:
    """The backend canvas, answering `_udf_get(endpoint, **params)`."""

    def __init__(self):
        self.apps: dict[str, dict] = {}
        self.categories = [("starters", "Starters"), ("local-ai", "Local AI")]
        self.collections: list[dict] = []
        self.blocked: list[dict] = []
        self.calls: list[tuple] = []
        self.timeouts: list[tuple] = []         # (endpoint, timeout) per call
        self.fail: str | None = None            # set → every call raises SourceError
        self.catalog_version = "cv1"
        self.installs: list[str] = []           # fused_app_install asks, in order
        self.install_fail: str | None = None    # set → fused_app_install alone raises

    # -- authoring ------------------------------------------------------
    def add_app(self, slug, files, version="1.0.0", category="starters", **row):
        self.set_files(slug, files, version, category=category, **row)

    def set_files(self, slug, files, version=None, *, commit=None, whats_new=None, **row):
        old = self.apps.get(slug, {})
        files = {p: (d.encode() if isinstance(d, str) else d) for p, d in files.items()}
        version = version or old["row"]["version"]
        commit = commit or _sha1(json.dumps(sorted((p, d.decode("latin-1")) for p, d in files.items()))
                                 + version + slug)
        r = {k: None for k in CATALOG_KEYS}
        r.update(old.get("row", {}))
        repo = row.pop("repo", None) or r["repo"] or f"{OWNER}/{slug}"
        base = f"https://raw.githubusercontent.com/{repo}/{commit}/"
        cat = row.pop("category", None) or r["category"] or "starters"
        releases = [{"commit": commit, "at": "2026-10-20T10:00:00Z", "whats_new": whats_new or f"{version} notes",
                     "version": version, "blocked": None}] + [
            x for x in (r["releases"] or []) if x["commit"] != commit]
        r.update(
            slug=slug, name=row.pop("name", None) or r["name"] or slug.replace("-", " ").title(),
            description=f"{slug} app", author={"name": "Alice", "github": OWNER},
            tags=[cat], category=cat, version=version,
            requires_python=False, ai_capabilities=[], ai_models=[],
            icon_url=base + "icon.svg" if "icon.svg" in files else None,
            preview_url=base + "preview.png" if "preview.png" in files else None,
            size_bytes=sum(len(d) for d in files.values()), file_count=len(files),
            release_id=commit, commit=commit, repo=repo, branch="main",
            source_url=f"https://github.com/{repo}",
            published_at="2026-08-18T12:54:44Z", updated_at="2026-10-20T10:00:00Z",
            last_release_at="2026-10-20T10:00:00Z", remix_of=None, remix_count=2,
            contributor_count=3, installs=12, installs_30d=4, views=40, views_30d=9, releases=releases,
            featured=r["featured"] or False, catalog_version=self.catalog_version,
            generated_at="2026-10-15T08:02:00Z")
        r.update(row)
        self.apps[slug] = {"row": r, "files": files}

    # -- the API ---------------------------------------------------------
    def __call__(self, endpoint, timeout=None, **params):
        self.calls.append((endpoint, params))
        self.timeouts.append((endpoint, timeout))
        if self.fail:
            raise mp.SourceError(self.fail)
        return getattr(self, f"_{endpoint}")(**params)

    def _marketplace_catalog(self, client_version):
        assert client_version
        return [json.loads(json.dumps(a["row"])) for _s, a in sorted(self.apps.items())]

    def _marketplace_categories(self):
        return [{"id": i, "label": lab, "order": n, "catalog_version": self.catalog_version,
                 "count": sum(a["row"]["category"] == i for a in self.apps.values())}
                for n, (i, lab) in enumerate(self.categories)]

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
                     readme_base_url=f"https://raw.githubusercontent.com/{a['row']['repo']}/{a['row']['commit']}/",
                     files=[{"path": p, "size": len(d)} for p, d in sorted(a["files"].items())])]

    def _marketplace_blocklist(self):
        return [dict(b, generated_at="2026-10-26T00:00:00Z") for b in self.blocked]

    def _fused_app_install(self, slug):
        if self.install_fail:
            raise mp.SourceError(self.install_fail)
        if slug not in self.apps:
            raise mp.SourceError(f'The app "{slug}" isn\'t in the marketplace.', "app_not_found")
        r = self.apps[slug]["row"]
        self.installs.append(slug)
        return [{"slug": slug, "name": r["name"], "repo": r["repo"], "commit": r["commit"],
                 "release_id": r["commit"], "branch": r["branch"], "version": r["version"],
                 "blocked": r.get("blocked"), "catalog_version": self.catalog_version,
                 "generated_at": "2026-10-26T17:00:00Z", "stale": False, "counted": True}]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated state dir + workspace and a FakeUdf wired in: local-chat
    (local-ai) and sine-starter (starters)."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("FUSED_RENDER_MARKETPLACE_API", raising=False)
    monkeypatch.delenv("FUSED_RENDER_MARKETPLACE_INDEX", raising=False)
    api = FakeUdf()
    api.add_app("local-chat", {"index.html": "<html>chat</html>", "readme.md": "# Local Chat\n\n![shot](preview.png)\n",
                               "preview.png": b"PNGDATA", "icon.svg": "<svg/>"},
                version="1.2.0", category="local-ai", featured=True)
    api.add_app("sine-starter", {"index.html": "<html>v1</html>", "sine.py": "print(1)\n"})
    api.collections = [
        {"collection": "shelf", "shelf_id": "starters", "title": "Starters", "position": 0, "slug": "sine-starter"},
        {"collection": "shelf", "shelf_id": "local-ai", "title": "Local AI", "position": 1, "slug": "local-chat"},
        {"collection": "shelf", "shelf_id": "local-ai", "title": "Local AI", "position": 0, "slug": "ghost-app"},
        {"collection": "new_and_updated", "shelf_id": None, "title": "New and updated", "position": 0, "slug": "local-chat"},
        {"collection": "new_and_updated", "shelf_id": None, "title": "New and updated", "position": 1, "slug": "sine-starter"},
    ]
    monkeypatch.setattr(mp, "_udf_get", api)
    # Tests refresh back to back; the once-a-minute limit has its own test.
    monkeypatch.setattr(mp, "REFRESH_MIN_INTERVAL_S", 0)
    raw = {}

    def fake_http_get(url, **kw):
        raw.setdefault("urls", []).append(url)
        for a in api.apps.values():
            base = f"https://raw.githubusercontent.com/{a['row']['repo']}/{a['row']['commit']}/"
            if url.startswith(base) and url[len(base):] in a["files"]:
                return _Bytes(a["files"][url[len(base):]]), None
        return None, "not found (404)"

    monkeypatch.setattr(mp, "_http_get", fake_http_get)
    with mp._JOBS_LOCK:
        mp._JOBS.clear()
        mp._JOB_FINISHED.clear()
    with mp._DETAIL_MEMO_LOCK:
        mp._DETAIL_MEMO.clear()
    return {"home": tmp_path / "home", "ws": tmp_path / "ws", "api": api, "raw": raw, "tmp": tmp_path}


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


def _app_dir(env, name):
    return os.path.join(str(env["ws"]), "app", name)


def _market_record(path, slug, commit, *, repo=None, listing="live", block=None, version="1.0.0"):
    """What `appfile_git.install(kind="marketplace")` records (§18.4), put
    straight into the store for tests that are not about the clone."""
    from fused_render.contrib import store

    os.makedirs(path, exist_ok=True)
    rec = store.new_record("github", repo=repo or f"{OWNER}/{slug}", branch="main", classified_by="install")
    rec.update(kind="marketplace", slug=slug,
               approved={"commit": commit, "at": "2026-10-20T10:00:00Z", "whats_new": "notes", "version": version},
               listing={"status": listing, "checked_at": time.time(), "delisted_at": None},
               block=block, switched=[])
    store.put(path, rec)
    return store.key(path)


def _legacy_copy(env, slug, files=None, **stamp):
    """A copy the OLD package Marketplace installed: plain files + its stamp."""
    import hashlib

    d = _app_dir(env, slug)
    files = files or {"index.html": "<html>legacy</html>", "sine.py": "print(0)\n"}
    for rel, text in files.items():
        _write(d, rel, text)
    st = {"slug": slug, "version": "0.9.0", "release_id": "r" * 12, "commit": "c" * 40,
          "api": mp.API_BASE, "installed_at": "2026-09-01T00:00:00Z", "source": "udf-api",
          "files": {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in files.items()}}
    st.update(stamp)
    _write(d, ".fused/marketplace.json", json.dumps(st))
    return d


def _stub_contrib(monkeypatch, name, **attrs):
    """Stub functions of `fused_render.contrib.<name>` — the real module when
    WS-ENGINE has landed it, a stand-in module when not yet."""
    import fused_render.contrib as pkg

    try:
        mod = __import__(f"fused_render.contrib.{name}", fromlist=[name])
    except ImportError:
        mod = types.ModuleType(f"fused_render.contrib.{name}")
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
        monkeypatch.setattr(pkg, name, mod, raising=False)
    for k, v in attrs.items():
        monkeypatch.setattr(mod, k, v, raising=False)
    return mod


# --- _udf_get: the HTTP seam ---------------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.headers = body, status, {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_udf_get_asks_for_json_and_reads_rows(monkeypatch):
    seen = {}

    def fake(url, **kw):
        seen["url"] = url
        return _Resp([{"a": 1}]), None

    monkeypatch.setattr(mp, "_http_get", fake)
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_API", "https://api.test/base/")
    assert mp._udf_get("marketplace_search", q="", category=None) == [{"a": 1}]
    u = urlparse(seen["url"])
    # `<endpoint>.json` always (the public canvas path 404s without it); an
    # empty value is kept; None is dropped; no `format` param.
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://api.test/base/marketplace_search.json"
    assert parse_qs(u.query, keep_blank_values=True) == {"q": [""]}
    # An empty list is a valid answer (an empty block list).
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (_Resp([]), None))
    assert mp._udf_get("marketplace_blocklist") == []


def test_udf_get_error_row_transport_and_shape(monkeypatch):
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (_Resp(
        [{"error_code": "app_not_found", "error_message": "no such app"}]), None))
    with pytest.raises(mp.SourceError, match="no such app") as ei:
        mp._udf_get("marketplace_app_detail", slug="x")
    assert ei.value.error_code == "app_not_found"
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (None, "could not reach the Community (X)"))
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
    assert cat["index"] == "fusedio/fused-apps-index"
    assert cat["generated_at"] == "2026-10-15T08:02:00Z"
    assert [a["slug"] for a in cat["apps"]] == ["local-chat", "sine-starter"]
    assert cat["categories"] == [{"id": "starters", "label": "Starters", "count": 1},
                                 {"id": "local-ai", "label": "Local AI", "count": 1}]
    assert cat["collections"] == [
        {"id": "starters", "kind": "shelf", "title": "Starters", "slugs": ["sine-starter"]},
        {"id": "local-ai", "kind": "shelf", "title": "Local AI", "slugs": ["local-chat"]},
        {"id": "new-and-updated", "kind": "new_and_updated", "title": "New and updated",
         "slugs": ["local-chat", "sine-starter"]},
    ]
    chat = cat["apps"][0]
    row = env["api"].apps["local-chat"]["row"]
    assert chat["name"] == "Local Chat" and chat["version"] == "1.2.0" and chat["featured"] is True
    # §18.3 fields pass through; release_id is the approved commit.
    assert chat["release_id"] == chat["commit"] == row["commit"]
    assert (chat["repo"], chat["branch"]) == ("alice/local-chat", "main")
    assert (chat["remix_of"], chat["remix_count"], chat["contributor_count"]) == (None, 2, 3)
    assert chat["install_count"] == 12  # §18.12: the marketplace's count, passed through
    assert chat["last_release_at"] == "2026-10-20T10:00:00Z"
    assert chat["releases"] == [{"commit": row["commit"], "at": "2026-10-20T10:00:00Z",
                                 "whats_new": "1.2.0 notes", "version": "1.2.0", "blocked": None}]
    assert chat["whats_new"] == "1.2.0 notes" and chat["installable"] is True
    assert chat["source_url"] == "https://github.com/alice/local-chat"
    v = row["commit"][:12]
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


def test_index_repo_override(env, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_INDEX", "fusedio/apps-index")
    assert mp.index_repo() == "fusedio/apps-index" and mp.catalog()["index"] == "fusedio/apps-index"
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_INDEX", "not a repo/../x")
    assert mp.index_repo() == mp.INDEX_REPO


def test_releases_and_remix_fields_are_shape_checked(env):
    api = env["api"]
    api.apps["sine-starter"]["row"].update(
        remix_of="local-chat", remix_count=-1, contributor_count="7",
        releases=[{"commit": "a" * 40, "at": "2026-10-21T00:00:00Z", "whats_new": "New", "version": "2",
                   "blocked": "soft"},
                  {"commit": "nope"}, "junk", {"commit": "b" * 40, "blocked": "weird"}])
    e = _entry("sine-starter")
    assert e["remix_of"] == "local-chat" and e["remix_count"] == 0 and e["contributor_count"] == 0
    assert [(r["commit"][0], r["blocked"]) for r in e["releases"]] == [("a", "soft"), ("b", None)]
    assert e["whats_new"] == "New"


def test_whats_new_and_blocked_follow_the_current_commit(env):
    # After a rollback the row's commit is not releases[0]: the current one wins.
    row = env["api"].apps["sine-starter"]["row"]
    cur = row["commit"]
    row["releases"] = [{"commit": "a" * 40, "at": "x", "whats_new": "Bad one", "version": "2",
                        "blocked": "hard", "release_id": "a" * 40, "changelog": "Bad one", "revoked": True},
                       {"commit": cur, "at": "y", "whats_new": "Good one", "version": "1", "blocked": None}]
    e = _entry("sine-starter", refresh=True)
    assert (e["whats_new"], e["blocked"]) == ("Good one", None)
    assert e["releases"][0] == {"commit": "a" * 40, "at": "x", "whats_new": "Bad one", "version": "2",
                                "blocked": "hard"}
    # The backend's top-level keys for the current commit take precedence.
    row.update(whats_new="Top level", blocked="soft")
    e = _entry("sine-starter", refresh=True)
    assert (e["whats_new"], e["blocked"]) == ("Top level", "soft")


def test_asset_base_new_and_old_backend_forms():
    c = "c" * 40
    assert mp._asset_base("x-app", {"repo": "alice/x", "commit": c}) == \
        f"https://raw.githubusercontent.com/alice/x/{c}/"
    # §18.3 source_url (the repo) + commit.
    assert mp._asset_base("x-app", {"source_url": "https://github.com/alice/x", "commit": c}) == \
        f"https://raw.githubusercontent.com/alice/x/{c}/"
    # The old backend's folder of the community-apps repo keeps its slug dir.
    old = {"source_url": f"https://github.com/fusedio/community/tree/{c}/x-app", "commit": c}
    assert mp._asset_base("x-app", old) == f"https://raw.githubusercontent.com/fusedio/community/{c}/x-app/"
    assert mp.row_repo(old) is None  # …and is not an installable source
    assert mp._asset_base("x-app", {"repo": "alice/x", "commit": "short"}) is None


def test_old_backend_rows_browse_but_do_not_install(env):
    row = env["api"].apps["sine-starter"]["row"]
    c = row["commit"]
    row.update(repo=None, source_url=f"https://github.com/fusedio/community/tree/{c}/sine-starter")
    e = _entry("sine-starter")
    assert e["repo"] is None and e["installable"] is False
    with pytest.raises(mp.InstallError, match="no reviewed version"):
        mp.start_install("sine-starter")


def test_incompatible_app_is_flagged(env):
    env["api"].add_app("future-app", {"index.html": "x"}, min_fused_render="999.0.0")
    assert _entry("future-app")["compatible"] is False


def test_snapshot_written_by_the_old_backend_is_no_cache(env):
    mp._write_cache({"source": "github-api", "commit": "c" * 40, "apps": {"x": {"meta": {}}},
                     "fetched_at": "then", "checked_at": time.time()})
    assert mp._read_cache() is None
    assert [a["slug"] for a in mp.catalog()["apps"]] == ["local-chat", "sine-starter"]
    # ...and a cache for another API base (the old constant, once the owner
    # swaps in the canvas's URL) is not trusted either.
    assert mp._read_cache() is not None
    os.environ["FUSED_RENDER_MARKETPLACE_API"] = "https://other.test"
    try:
        assert mp._read_cache() is None
    finally:
        del os.environ["FUSED_RENDER_MARKETPLACE_API"]


def test_stale_cache_fallback(env):
    cat = mp.catalog()
    assert cat["stale"] is False
    env["api"].fail = "could not reach the Community (ConnectError)"
    cat2 = mp.catalog(refresh=True)
    assert cat2["status"] == "ok" and cat2["stale"] is True
    assert "could not reach" in cat2["warning"]
    assert [a["slug"] for a in cat2["apps"]] == ["local-chat", "sine-starter"]
    shutil.rmtree(mp.state_dir())
    err = mp.catalog(refresh=True)
    assert err["status"] == "error" and "could not reach" in err["message"]
    assert err["apps"] == [] and err["collections"] == []
    env["api"].fail = None
    assert mp.catalog(refresh=True)["stale"] is False


def test_a_new_catalog_version_prunes_unreferenced_asset_caches(env):
    mp.catalog()
    mp.get_asset("local-chat", "icon.svg")
    root = os.path.join(mp.state_dir(), "assets")
    old = env["api"].apps["local-chat"]["row"]["commit"]
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
    r = client.get("/api/marketplace/search", params={"q": "app", "limit": 1}).json()
    assert [a["slug"] for a in r["apps"]] == ["local-chat"] and r["next_cursor"] == "1"
    assert env["api"].calls[-1][1]["cursor"] is None and env["api"].calls[-1][1]["category"] is None
    r = client.get("/api/marketplace/search", params={"q": "app", "limit": 1, "cursor": "1"}).json()
    assert [a["slug"] for a in r["apps"]] == ["sine-starter"] and r["next_cursor"] is None
    client.get("/api/marketplace/search", params={"q": "x", "limit": 9999})
    assert env["api"].calls[-1][1]["limit"] == 100


def test_search_merges_installed_and_reports_errors_as_200(env):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    _market_record(_app_dir(env, "sine-starter"), "sine-starter", commit)
    [hit] = _client().get("/api/marketplace/search", params={"q": "sine"}).json()["apps"]
    assert hit["installed"]["kind"] == "marketplace" and hit["installed"]["commit"] == commit
    assert hit["update_available"] is False
    env["api"].fail = "the Community answered 502"
    r = _client().get("/api/marketplace/search", params={"q": "sine"})
    assert r.status_code == 200 and r.json() == {"status": "error", "message": "the Community answered 502"}


def test_search_row_without_commit_does_not_flag_an_installed_current_app(env):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    _market_record(_app_dir(env, "sine-starter"), "sine-starter", commit)
    mp.catalog()
    orig = env["api"]._marketplace_search
    env["api"]._marketplace_search = lambda *a, **kw: [
        {k: v for k, v in r.items() if k not in ("release_id", "commit")} for r in orig(*a, **kw)]
    [hit] = mp.search("sine")["apps"]
    assert hit["installed"] is not None and hit["update_available"] is False
    assert hit["commit"] == commit


# --- installed: marketplace records + legacy stamps --------------------------------

def test_installed_marketplace_copy_and_update_available(env):
    api = env["api"]
    old = api.apps["sine-starter"]["row"]["commit"]
    path = _app_dir(env, "sine-starter")
    _market_record(path, "sine-starter", old)
    inst = _entry("sine-starter")["installed"]
    assert os.path.realpath(inst.pop("path")) == os.path.realpath(path)
    assert inst == {"kind": "marketplace", "commit": old, "version": "1.0.0", "installed_at": None,
                    "update_available": False, "modified": None, "delisted": False, "block": None}
    # A release moves the row: the copy is behind until the next check moves it.
    api.set_files("sine-starter", {"index.html": "<html>v2</html>"}, "1.1.0", whats_new="Faster sine")
    e = _entry("sine-starter", refresh=True)
    assert e["installed"]["update_available"] is True and e["update_available"] is True
    assert e["whats_new"] == "Faster sine" and [r["commit"] for r in e["releases"]][1] == old


def test_installed_block_is_shown(env):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    _market_record(_app_dir(env, "sine-starter"), "sine-starter", commit,
                   block={"level": "hard", "commit": commit, "reason": "Reads saved passwords",
                          "what_to_do": "Rotate your keys", "at": "2026-10-26", "rolled_back_to": None,
                          "conflict": False, "confirmed_at": None})
    b = _entry("sine-starter")["installed"]["block"]
    assert b == {"level": "hard", "commit": commit, "reason": "Reads saved passwords",
                 "what_to_do": "Rotate your keys", "at": "2026-10-26", "rolled_back_to": None,
                 "rolled_back_to_at": None, "conflict": False, "confirmed": False}


def test_installed_list_keeps_delisted_copies(env):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    path = _app_dir(env, "sine-starter")
    _market_record(path, "sine-starter", commit, version="1.0.0")
    _write(path, "metadata.json", json.dumps({"name": "My Sine", "version": "1.0.0"}))
    mp.catalog()
    del env["api"].apps["sine-starter"]
    mp.catalog(refresh=True)
    [app] = mp.installed_list()["apps"]
    assert app["slug"] == "sine-starter" and app["name"] == "My Sine"
    assert app["installed"]["kind"] == "marketplace" and app["installed"]["delisted"] is True
    assert app["update_available"] is False and app["installable"] is False
    # Detail of a delisted copy: the local copy describes itself.
    _write(path, "readme.md", "# mine")
    d = mp.app_detail("sine-starter")
    assert d["readme"] == "# mine" and d["releases"] == [] and d["app"]["installed"]["delisted"] is True


def test_a_record_whose_folder_is_gone_is_not_installed(env):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    path = _app_dir(env, "sine-starter")
    _market_record(path, "sine-starter", commit)
    shutil.rmtree(path)
    assert mp.installed_copies() == {} and _entry("sine-starter")["installed"] is None


def test_legacy_copy_is_installed_modified_and_never_updates(env):
    d = _legacy_copy(env, "sine-starter")
    inst = _entry("sine-starter")["installed"]
    assert inst["kind"] == "legacy" and inst["update_available"] is False and inst["modified"] is False
    assert inst["delisted"] is False and inst["version"] == "0.9.0" and inst["commit"] == "c" * 40
    _write(d, "sine.py", "print('mine')\n")
    assert _entry("sine-starter")["installed"]["modified"] is True
    # Not listed: "No longer updated".
    del env["api"].apps["sine-starter"]
    mp.catalog(refresh=True)
    [app] = mp.installed_list()["apps"]
    assert app["installed"]["kind"] == "legacy" and app["installed"]["delisted"] is True
    assert app["version"] == "0.9.0" and app["file_count"] == 2


def test_a_legacy_copy_is_found_by_its_app_id_not_its_old_name_slug(env):
    """Owner 2026-10-27: the index lists apps by `fused-app-id`. An old
    package's stamp carries the old store's NAME slug; the copy's own page
    id is what names its row now."""
    aid = "sine-starter-0a1b2c3d"
    page = ('<html><head><meta name="fused-app" />'
            f'<meta name="fused-app-id" content="{aid}" /></head></html>')
    _legacy_copy(env, "sine-starter", files={"index.html": page, "sine.py": "print(0)\n"})
    row = env["api"].apps.pop("sine-starter")["row"]
    env["api"].add_app(aid, {"index.html": page, "sine.py": "print(0)\n"},
                       repo=row["repo"], name="Sine Starter")
    mp.catalog(refresh=True)
    copies = mp.installed_copies()
    assert set(copies) == {aid} and copies[aid]["kind"] == "legacy"
    assert _entry(aid)["installed"]["kind"] == "legacy"


def test_a_husk_is_not_installed(env):
    d = _legacy_copy(env, "sine-starter")
    for n in os.listdir(d):
        if n != ".fused":
            os.remove(os.path.join(d, n))
    assert mp.installed_copies() == {}


def test_legacy_stamp_inside_a_marketplace_clone_is_not_listed_twice(env):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    d = _legacy_copy(env, "sine-starter")
    _market_record(d, "sine-starter", commit)
    copies = mp.installed_copies()
    assert list(copies) == ["sine-starter"] and copies["sine-starter"]["kind"] == "marketplace"


# --- install: a real clone at the approved commit -------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8" /><meta name="fused-app" />
<meta name="fused-api-version" content="1" />
<meta name="fused-app-id" content="sine-starter-0a1b2c3d" /><title>Sine</title></head>
<body><p>v1</p></body></html>
"""


def _git(root, *args):
    import subprocess

    from fused_render.shell import git_tools

    r = subprocess.run([git_tools.git_bin(), "-C", str(root), *args], capture_output=True, text=True,
                       close_fds=False)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture()
def gh_world(env, monkeypatch):
    """Alice's `sine-starter` repo on a FakeGitHub, and the catalog row for
    it pointing at its first commit (the approved one)."""
    from fused_render.contrib import gh

    monkeypatch.setenv(gh.FAKE_ENV, str(env["tmp"] / "github"))
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    gh.forget_identity()
    fake.add_user("alice", "Alice")
    fake.add_user("bob", "Bob")
    fake.sign_in("bob")
    full = fake.create_repo_as("alice", "sine-starter", {"index.html": PAGE, "app.py": "A = 1\n"})
    approved = fake.tip(full, "main")
    env["api"].set_files("sine-starter", {"index.html": PAGE, "app.py": "A = 1\n"}, "1.0.0",
                         commit=approved, repo=full)
    return types.SimpleNamespace(fake=fake, full=full, approved=approved)


def _wait_job(client, slug):
    deadline = time.time() + 60
    job = None
    while time.time() < deadline:
        job = client.get("/api/marketplace/jobs").json()["jobs"][slug]
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    return job


def test_install_clones_the_approved_commit_not_the_authors_tip(env, gh_world):
    # Alice pushes after the release: an unreviewed commit on her main.
    tip = gh_world.fake.commit(gh_world.full, "main", {"app.py": "A = 2  # unreviewed\n"}, "Unreviewed",
                                 author="alice")
    assert tip != gh_world.approved
    client = _client()
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"})
    assert r.status_code == 403  # the X-Fused guard
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    job = r.json()["job"]
    assert job["action"] == "install" and job["step"] in ("download", "place", "done")
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done" and job["step"] == "done", job
    assert job["path"].endswith("/app/sine-starter") and job["message"] == "Installed Sine Starter"
    dest = job["path"]
    assert _git(dest, "rev-parse", "HEAD") == gh_world.approved
    with open(os.path.join(dest, "app.py")) as f:
        assert f.read() == "A = 1\n"
    assert _git(dest, "config", "--get", "remote.origin.url").endswith("alice/sine-starter")
    # Recorded as a marketplace copy of the slug at the approved commit.
    from fused_render.contrib import store

    rec = store.get(dest)
    assert rec["kind"] == "marketplace" and rec["slug"] == "sine-starter"
    assert rec["approved"]["commit"] == gh_world.approved and rec["approved"]["version"] == "1.0.0"
    [app] = client.get("/api/marketplace/installed").json()["apps"]
    assert app["installed"]["kind"] == "marketplace" and app["installed"]["commit"] == gh_world.approved
    assert app["update_available"] is False
    # Nothing staged is left behind (the clone moved; the built .fused is gone).
    from fused_render import appfetch

    assert not [n for n in os.listdir(appfetch.downloads_dir()) if n.endswith(".fused")]


def test_install_asks_the_marketplace_once_with_the_slug_alone(env, gh_world):
    """§18.12: the install's one lookup is what the marketplace counts. It
    carries the slug and nothing else."""
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    assert _wait_job(client, "sine-starter")["state"] == "done"
    assert env["api"].installs == ["sine-starter"]
    [(endpoint, params)] = [c for c in env["api"].calls if c[0] == "fused_app_install"]
    assert params == {"slug": "sine-starter"}


def test_install_takes_the_live_row_over_a_stale_catalog(env, gh_world):
    """The catalog was read before a release: the marketplace's live answer
    names the new approved commit, and that is what lands."""
    mp.catalog()  # the cached snapshot: sine-starter at gh_world.approved
    newer = gh_world.fake.commit(gh_world.full, "main", {"app.py": "A = 3\n"}, "Release 1.0.1",
                                 author="alice")
    env["api"].set_files("sine-starter", {"index.html": PAGE, "app.py": "A = 3\n"}, "1.0.1",
                         commit=newer, repo=gh_world.full)
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done", job
    assert _git(job["path"], "rev-parse", "HEAD") == newer
    from fused_render.contrib import store

    rec = store.get(job["path"])
    assert rec["approved"]["commit"] == newer and rec["approved"]["version"] == "1.0.1"


def test_install_goes_by_the_cached_row_when_the_marketplace_is_away(env, gh_world):
    env["api"].install_fail = "the marketplace can't be reached"
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done", job
    assert _git(job["path"], "rev-parse", "HEAD") == gh_world.approved
    assert env["api"].installs == []


def _older_and_newer(env, gh_world):
    """sine-starter released twice: the approved commit, then a newer
    release (the row's commit), both on Alice's branch."""
    newer = gh_world.fake.commit(gh_world.full, "main", {"app.py": "A = 2\n"}, "Release 1.1.0",
                                 author="alice")
    env["api"].set_files("sine-starter", {"index.html": PAGE, "app.py": "A = 2\n"}, "1.1.0",
                         commit=newer, repo=gh_world.full)
    return gh_world.approved, newer


@pytest.mark.parametrize("where", ["release", "row", "blocklist"])
def test_the_cached_row_fallback_never_installs_a_blocked_release(env, gh_world, where):
    """M3, fail closed: with the marketplace away the install goes by the
    cached row, which may predate a hard block. The commit is vetted there
    too (`market._pick`): the newest release not blocked — by the row's
    release, the row itself, or the block list on disk — is what lands and
    what the copy records, and the open gate knows its HEAD at once."""
    older, newer = _older_and_newer(env, gh_world)
    row = env["api"].apps["sine-starter"]["row"]
    if where == "release":
        row["releases"][0]["blocked"] = "hard"
    elif where == "row":
        row["blocked"] = "hard"
    else:
        env["api"].blocked = [_blk(commit=newer)]
        assert mp.blocklist(force=True)["blocked"][0]["commit"] == newer
    mp.catalog(refresh=True)                          # the cache now holds the blocked row
    env["api"].install_fail = "the marketplace can't be reached"
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done", job
    assert _git(job["path"], "rev-parse", "HEAD") == older
    from fused_render.contrib import blocks, store

    rec = store.get(job["path"])
    assert rec["approved"]["commit"] == older and rec["approved"]["version"] == "1.0.0"
    assert rec["last_head"] == older
    assert blocks.gate(job["path"]) is None


def test_the_cached_row_fallback_refuses_when_every_release_is_blocked(env, gh_world):
    row = env["api"].apps["sine-starter"]["row"]
    for r in row["releases"]:
        r["blocked"] = "hard"
    mp.catalog(refresh=True)
    env["api"].install_fail = "the marketplace can't be reached"
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "error" and "withdrew" in job["error"], job
    assert not os.path.exists(_app_dir(env, "sine-starter"))


def test_install_refuses_a_release_the_marketplace_withdrew(env, gh_world):
    env["api"].apps["sine-starter"]["row"]["blocked"] = "hard"
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "error" and "withdrew" in job["error"], job


def test_a_release_moves_the_copy_through_the_real_engine(env, gh_world):
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    dest = _wait_job(client, "sine-starter")["path"]
    # Alice pushes; nothing moves (M1). Then the row moves to that commit: a release.
    tip = gh_world.fake.commit(gh_world.full, "main", {"app.py": "A = 2\n"}, "Faster", author="alice")
    assert _entry("sine-starter")["installed"]["update_available"] is False
    env["api"].set_files("sine-starter", {"index.html": PAGE, "app.py": "A = 2\n"}, "1.1.0",
                         commit=tip, whats_new="Faster sine")
    assert _entry("sine-starter", refresh=True)["installed"]["update_available"] is True
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.json()["job"]["action"] == "update"
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done", job
    assert _git(dest, "rev-parse", "HEAD") == tip
    from fused_render.contrib import store

    assert store.get(dest)["approved"]["commit"] == tip
    assert _entry("sine-starter")["installed"]["update_available"] is False


def test_second_install_is_an_update_through_the_engine(env, gh_world, monkeypatch):
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    dest = _wait_job(client, "sine-starter")["path"]
    calls = []
    from fused_render.contrib import engine

    monkeypatch.setattr(engine, "bring_in", lambda root: calls.append(root) or
                        {"ok": True, "message": "Updated to alice's release of Oct 26: Faster sine"})
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.json()["job"]["action"] == "update"
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done" and job["message"].startswith("Updated to alice's release")
    assert calls == [os.path.realpath(dest)]
    # The engine's refusal is the job's error, verbatim; an overlap says to open the app.
    monkeypatch.setattr(engine, "bring_in", lambda root: {"ok": True, "needs_merge": True,
                                                          "message": "alice changed the same parts you did."})
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "error" and job["error"].startswith("alice changed the same parts")
    assert "Open the app" in job["error"]


def _link_copy(env, gh_world):
    """Bob already follows the app by GitHub link: a clone in the workspace
    recorded as kind `github`."""
    from fused_render.contrib import store

    d = _app_dir(env, "sine-starter")
    os.makedirs(os.path.dirname(d), exist_ok=True)
    _git(os.path.dirname(d), "clone", "-q", f"https://github.com/{gh_world.full}", d)
    store.put(d, store.new_record("github", repo=gh_world.full, branch="main", classified_by="install"))
    return d


def test_a_github_link_copy_asks_before_switching(env, gh_world, monkeypatch):
    d = _link_copy(env, gh_world)
    switched = []
    _stub_contrib(monkeypatch, "market", switch_to_reviewed=lambda root, row: switched.append(
        (root, row["commit"])) or {"ok": True, "message": "Switched to reviewed releases."})
    client = _client()
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "confirm" and body["switch"] == "reviewed"
    assert body["who"] == "alice" and os.path.realpath(body["path"]) == os.path.realpath(d)
    assert client.get("/api/marketplace/jobs").json()["jobs"] == {} and switched == []
    r = client.post("/api/marketplace/install", json={"slug": "sine-starter", "switch": True}, headers=FUSED)
    assert r.json()["status"] == "ok" and r.json()["job"]["action"] == "switch"
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "done" and job["message"] == "Switched to reviewed releases."
    assert switched == [(os.path.realpath(d), gh_world.approved)]


def test_the_authors_own_clone_with_a_stale_stamp_is_her_app_not_legacy(env, gh_world, monkeypatch):
    # The live shape: Alice's own clone (origin her repo, HEAD main, no record
    # yet) still carrying the old package installer's stamp.
    gh_world.fake.sign_in("alice")
    d = _app_dir(env, "sine-starter")
    os.makedirs(os.path.dirname(d), exist_ok=True)
    _git(os.path.dirname(d), "clone", "-q", f"https://github.com/{gh_world.full}", d)
    _write(d, ".fused/marketplace.json", json.dumps({"slug": "sine-starter", "version": "0.9.0",
                                                     "files": {"index.html": "0" * 64}}))
    adopted = []
    _stub_contrib(monkeypatch, "market", adopt_legacy=lambda root, row: adopted.append(root) or {"ok": True})
    inst = _entry("sine-starter")["installed"]
    assert inst["kind"] == "own" and inst["update_available"] is False and inst["modified"] is None
    assert os.path.realpath(inst["path"]) == os.path.realpath(d)
    # The dead stamp is gone; the record says own; she stays "Your app".
    assert not os.path.exists(os.path.join(d, ".fused", "marketplace.json"))
    from fused_render.contrib import store

    assert store.get(d)["kind"] == "own"
    assert _entry("sine-starter")["installed"]["kind"] == "own"
    [app] = mp.installed_list()["apps"]
    assert app["installed"]["kind"] == "own"
    # Never adopted (no `git init` over a repo), never installed over.
    r = _client().post("/api/marketplace/adopt", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.status_code == 400 and r.json()["message"] == "This is already a copy of the app's repo."
    assert adopted == []
    with pytest.raises(mp.InstallError, match="your own app"):
        mp.start_install("sine-starter")


def test_a_link_clone_with_a_stale_stamp_asks_to_switch(env, gh_world):
    d = _link_copy(env, gh_world)
    _write(d, ".fused/marketplace.json", json.dumps({"slug": "sine-starter", "version": "0.9.0"}))
    assert _entry("sine-starter")["installed"] is None
    assert not os.path.exists(os.path.join(d, ".fused", "marketplace.json"))
    assert mp.start_install("sine-starter")["status"] == "confirm"


def test_an_unrecorded_clone_of_the_repo_is_refused(env, gh_world):
    from fused_render.contrib import store

    d = _link_copy(env, gh_world)
    store.drop(d)
    with pytest.raises(mp.InstallError, match="already have a copy"):
        mp.start_install("sine-starter")


def test_install_refuses_a_legacy_copy(env):
    _legacy_copy(env, "sine-starter")
    r = _client().post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.status_code == 400 and r.json()["message"].startswith("Switch to the reviewed version first")


def test_install_route_errors(env):
    client = _client()
    r = client.post("/api/marketplace/install", json={"slug": "../x"}, headers=FUSED)
    assert r.status_code == 400 and r.json()["status"] == "error"
    r = client.post("/api/marketplace/install", json={"slug": "ghost-app"}, headers=FUSED)
    assert r.status_code == 400 and "no app named" in r.json()["message"]


def test_a_failing_clone_surfaces_on_the_job(env, monkeypatch):
    from fused_render import appfile_git

    def boom(url, repo, **kw):
        raise appfile_git.GitInstallError("The reviewed version isn't in alice/sine-starter's main")

    monkeypatch.setattr(appfile_git, "clone", boom)
    client = _client()
    client.post("/api/marketplace/install", json={"slug": "sine-starter"}, headers=FUSED)
    job = _wait_job(client, "sine-starter")
    assert job["state"] == "error" and "reviewed version isn't in" in job["error"]


# --- adopt + check ------------------------------------------------------------------

def test_adopt_a_legacy_copy(env, monkeypatch):
    d = _legacy_copy(env, "sine-starter")
    seen = []
    _stub_contrib(monkeypatch, "market", adopt_legacy=lambda root, row: seen.append((root, row["commit"])) or {
        "ok": True, "message": "Switched to the reviewed version. Your edits and data stayed."})
    client = _client()
    assert client.post("/api/marketplace/adopt", json={"slug": "sine-starter"}).status_code == 403
    r = client.post("/api/marketplace/adopt", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["message"].startswith("Switched to the reviewed version")
    assert seen == [(os.path.realpath(d), env["api"].apps["sine-starter"]["row"]["commit"])]
    # The engine's refusal comes back as the error.
    _stub_contrib(monkeypatch, "market", adopt_legacy=lambda root, row: {
        "ok": False, "message": "This folder is already a repo of another app."})
    r = client.post("/api/marketplace/adopt", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.status_code == 400 and r.json()["message"] == "This folder is already a repo of another app."
    # Unlisted: nothing to switch to.
    mp.catalog()
    del env["api"].apps["sine-starter"]
    mp.catalog(refresh=True)
    r = client.post("/api/marketplace/adopt", json={"slug": "sine-starter"}, headers=FUSED)
    assert r.status_code == 400 and "no longer in the Community" in r.json()["message"]
    r = client.post("/api/marketplace/adopt", json={"slug": "local-chat"}, headers=FUSED)
    assert r.status_code == 400 and "no copy from the old app store" in r.json()["message"]


def test_check_forces_the_catalog_refreshes_blocks_and_syncs_the_copy(env, monkeypatch):
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    path = _app_dir(env, "sine-starter")
    _market_record(path, "sine-starter", commit)
    mp.catalog()
    order = []
    _stub_contrib(monkeypatch, "blocks", refresh=lambda: order.append("refresh") or True,
                  enforce_all=lambda: order.append("enforce") or [{"path": path, "level": "soft"}])
    from fused_render import git_upstream

    monkeypatch.setattr(git_upstream, "sync_repo", lambda root, *, action, push: order.append(
        ("sync", root, action, push)) or {"ok": True, "status": "synced"})
    builds = len([c for c in env["api"].calls if c[0] == "marketplace_catalog"])
    client = _client()
    assert client.post("/api/marketplace/check", json={"slug": "sine-starter"}).status_code == 403
    r = client.post("/api/marketplace/check", json={"slug": "sine-starter"}, headers=FUSED).json()
    # The catalog was rebuilt inside its 10-minute TTL (the button is never a no-op).
    assert len([c for c in env["api"].calls if c[0] == "marketplace_catalog"]) == builds + 1
    assert r["status"] == "ok" and r["catalog"]["ok"] is True
    assert order == ["refresh", "enforce",
                     ("sync", os.path.realpath(path), "Check for a new release", False)]
    assert r["enforced"] == [{"path": path, "level": "soft"}] and r["sync"]["status"] == "synced"
    assert r["app"]["slug"] == "sine-starter" and r["blocklist"]["ok"] is True
    # No slug: catalog + block list only.
    order.clear()
    r = client.post("/api/marketplace/check", json={}, headers=FUSED).json()
    assert order == ["refresh", "enforce"] and r["sync"] is None and r["app"] is None


# --- block list --------------------------------------------------------------------

def _blk(slug="sine-starter", commit="a" * 40, **kw):
    return {"slug": slug, "commit": commit, "level": "hard", "reason": "Reads saved passwords",
            "what_to_do": "Rotate your GitHub token", "at": "2026-10-26T00:00:00Z", **kw}


def test_blocklist_fetch_ttl_force_and_route(env):
    api = env["api"]
    api.blocked = [_blk()]
    bl = mp.blocklist()
    assert bl["status"] == "ok" and bl["stale"] is False and isinstance(bl["checked_at"], float)
    assert bl["blocked"] == [{k: v for k, v in _blk().items()}]
    with open(mp.blocklist_path()) as f:
        assert json.load(f)["blocked"] == bl["blocked"]
    # Within the TTL: no second call; `force` fetches.
    n = len([c for c in api.calls if c[0] == "marketplace_blocklist"])
    api.blocked = [_blk(commit="e" * 40)]
    assert mp.blocklist()["blocked"] == bl["blocked"]
    assert len([c for c in api.calls if c[0] == "marketplace_blocklist"]) == n
    assert mp.blocklist(force=True)["blocked"] == [_blk(commit="e" * 40)]
    # The route answers the cached list.
    r = _client().get("/api/marketplace/blocklist").json()
    assert r["blocked"] == [_blk(commit="e" * 40)] and r["status"] == "ok"


def _header(entries, generated_at="2026-10-27T00:00:00Z", **kw):
    """The self-describing block list answer (contract 5), as `_udf_get`
    hands it on: one row."""
    return [{"generated_at": generated_at, "entries": entries, **kw}]


def test_blocklist_reads_both_shapes_and_never_trusts_an_unknown_answer(env, monkeypatch):
    """Contract 5, client half: the bare list (the older canvas) and the
    self-describing header both read; an answer that cannot vouch for itself
    (stale, a header without `generated_at`, older than the list here, or
    every known entry gone without a newer header) keeps the last list —
    it may add entries, never remove one."""
    api = env["api"]
    api.blocked = [_blk()]
    assert mp.blocklist(force=True)["blocked"] == [_blk()]
    answers = []
    monkeypatch.setattr(api, "_marketplace_blocklist", lambda: answers.pop(0))

    def ask(answer):
        answers.append(answer)
        return mp.blocklist(force=True)

    # Every known entry gone from a bare list (an unreadable index used to read so).
    bl = ask([])
    assert bl["status"] == "stale" and bl["blocked"] == [_blk()]
    # A bare list whose every row is garbled is the same thing.
    bl = ask([{"slug": "../x", "commit": "short", "generated_at": "2026-10-28T00:00:00Z"}])
    assert bl["status"] == "stale" and bl["blocked"] == [_blk()]
    # A row flagged stale (the backend's fallback copy): unknown, but it may ADD.
    bl = ask([dict(_blk(commit="b" * 40), stale=True, generated_at="2026-10-28T00:00:00Z")])
    assert bl["status"] == "stale" and bl["blocked"] == [_blk(), _blk(commit="b" * 40)]
    # A header without `generated_at`, or flagged stale: unknown.
    assert ask(_header([], generated_at=None))["blocked"] == [_blk(), _blk(commit="b" * 40)]
    assert ask(_header([], stale=True))["status"] == "stale"
    # A header with the list: trusted, and it may shrink the list.
    bl = ask(_header([_blk(level="Hard")]))           # a malformed level reads as hard
    assert bl["status"] == "ok" and bl["header"] is True and bl["blocked"] == [_blk()]
    assert bl["generated_at"] == "2026-10-27T00:00:00Z"
    # Older than the list here: unknown.
    assert ask(_header([], generated_at="2026-10-01T00:00:00Z"))["blocked"] == [_blk()]
    # A newer header carrying an entry fused cannot read never lifts one.
    bl = ask(_header([{"slug": "sine-starter", "commit": "abc", "level": "hard",
                       "malformed": True}], generated_at="2026-10-28T00:00:00Z"))
    assert bl["status"] == "stale" and bl["blocked"] == [_blk()]
    # The same `generated_at` and empty: unknown (nothing says it changed).
    assert ask(_header([]))["blocked"] == [_blk()]
    # A NEWER header that names nothing: Fused lifted the last block.
    bl = ask(_header([], generated_at="2026-10-28T00:00:00Z"))
    assert bl["status"] == "ok" and bl["blocked"] == []
    from fused_render.contrib import blocks
    assert blocks.list_vouched()


def test_udf_get_answers_a_self_describing_object_as_one_row(env, monkeypatch):
    monkeypatch.setattr(mp, "_udf_get", _REAL_UDF_GET)

    class R:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    bodies = [{"generated_at": "g", "entries": []}, {"rows": []}]
    monkeypatch.setattr(mp, "_http_get", lambda url, **kw: (R(bodies.pop(0)), None))
    assert mp._udf_get("marketplace_blocklist") == [{"generated_at": "g", "entries": []}]
    with pytest.raises(mp.SourceError):
        mp._udf_get("marketplace_blocklist")


def test_an_implausible_catalog_is_stale_until_it_repeats(env):
    """A zero-app catalog, or one missing most of the apps the cache holds,
    is how an unreadable index looks: the last build stands, flagged stale,
    so no copy reads as delisted. The same answer twice is believed (a real
    mass delisting looks that way too)."""
    api = env["api"]
    mp.catalog(refresh=True)
    saved = dict(api.apps)
    api.apps.clear()
    snap, _e = mp.get_snapshot(force=True)
    assert snap["stale"] and set(snap["apps"]) == set(saved)
    assert "empty catalog" in snap["warning"]
    snap, _e = mp.get_snapshot(force=True)            # the same answer again: believed
    assert not snap["stale"] and snap["apps"] == {}
    api.apps.update(saved)
    assert set(mp.get_snapshot(force=True)[0]["apps"]) == set(saved)
    # One of two apps missing is not "most": believed at once.
    api.apps.pop("local-chat")
    assert not mp.get_snapshot(force=True)[0]["stale"]


def test_a_catalog_the_backend_served_stale_never_delists(env):
    api = env["api"]
    mp.catalog(refresh=True)
    api.apps["sine-starter"]["row"]["stale"] = True
    api.apps["local-chat"]["row"]["stale"] = True
    gone = api.apps.pop("local-chat")
    snap, _e = mp.get_snapshot(force=True)
    assert snap["stale"] and "local-chat" in snap["apps"]
    from fused_render.contrib import market

    assert not market._fresh(snap)
    api.apps["local-chat"] = gone


def test_check_waits_for_a_build_in_flight_instead_of_answering_the_old_cache(env):
    import threading

    mp.catalog(refresh=True)
    env["api"].set_files("sine-starter", {"index.html": "<html>v2</html>"}, "2.0.0")
    mp._CATALOG_LOCK.acquire()
    threading.Timer(0.3, mp._CATALOG_LOCK.release).start()
    snap, _e = mp.get_snapshot(force=True)
    assert not snap["stale"] and snap["apps"]["sine-starter"]["version"] == "2.0.0"


@pytest.mark.parametrize("url,want", [
    ("javascript:alert(1)", "https://github.com/alice/sine-starter"),
    ("https://evil.example/x", "https://github.com/alice/sine-starter"),
    ("https://github.com/x/y\"onclick=1", "https://github.com/alice/sine-starter"),
    ("https://github.com/alice/sine-starter", "https://github.com/alice/sine-starter"),
])
def test_source_url_is_only_ever_a_github_page(env, url, want):
    env["api"].apps["sine-starter"]["row"]["source_url"] = url
    mp.catalog(refresh=True)
    assert _entry("sine-starter")["source_url"] == want


def test_a_hard_block_shuts_the_run_doors_too(env, tmp_path):
    """A window already open on a blocked copy keeps calling runPython, and
    its background daemon can be started again: both are refused (423)."""
    from fused_render.server import create_app

    app_dir = tmp_path / "ws" / "app" / "sine-starter"
    app_dir.mkdir(parents=True)
    _git(app_dir, "init", "-q")
    _git(app_dir, "remote", "add", "origin", "https://github.com/alice/sine-starter")
    (app_dir / "index.html").write_text(PAGE)
    (app_dir / "pyproject.toml").write_text(
        '[project]\nname = "s"\nversion = "0"\n[tool.fused-render.app]\nmain = "app.py"\n')
    (app_dir / "app.py").write_text("def ping():\n    return 1\n")
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    _market_record(str(app_dir), "sine-starter", commit,
                   block={"level": "hard", "commit": commit, "reason": "Reads saved passwords",
                          "what_to_do": "", "at": "2026-10-26", "rolled_back_to": None,
                          "conflict": True, "confirmed_at": None})
    client = TestClient(create_app(start_dir=str(tmp_path)))
    html = str(app_dir / "index.html")
    r = client.post("/api/run", json={"html": html, "py": "x.py"}, headers=FUSED)
    assert r.status_code == 423 and r.json()["blocked"]["level"] == "hard"
    for door in ("start", "restart"):
        r = client.post(f"/api/apps/background/{door}", json={"html": html}, headers=FUSED)
        assert r.status_code == 423 and r.json()["blocked"]["conflict"] is True, (door, r.text)
    # A daemon already running before the block: `fused.daemon.call()` /
    # `.run()` reach it through the engine proxy, refused the same way.
    from fused_render import background_apps
    from fused_render.server import engine_host

    eid = background_apps.engine_id_for(str(app_dir))
    child = engine_host.Child(engine_id=eid, python="python", daemon="d.py", cache="c",
                              version="v", folder=os.path.realpath(str(app_dir)))
    engine_host._children[eid] = child
    try:
        for method in ("get", "post"):
            r = getattr(client, method)(f"/api/engines/{eid}/proxy/call", headers=FUSED)
            assert r.status_code == 423 and r.json()["blocked"]["level"] == "hard", r.text
    finally:
        engine_host._children.pop(eid, None)


def test_blocklist_offline_keeps_the_last_list(env, monkeypatch):
    env["api"].blocked = [_blk()]
    mp.blocklist()
    env["api"].fail = "could not reach the Community (ConnectError)"
    bl = mp.blocklist(force=True)
    assert bl["status"] == "stale" and bl["stale"] is True and bl["blocked"] == [_blk()]
    assert "could not reach" in bl["message"]
    # The failure stamped checked_at: the next plain read backs off.
    n = len(env["api"].calls)
    assert mp.blocklist()["blocked"] == [_blk()] and len(env["api"].calls) == n
    # Never fetched at all + offline: an empty list that says so.
    os.remove(mp.blocklist_path())
    bl = mp.blocklist()
    assert bl == {**bl, "blocked": [], "status": "error", "stale": True}
    assert mp.cached_blocklist()["status"] == "error"


def test_blocklist_reads_the_slim_file_contrib_blocks_writes(env):
    # contrib/blocks.refresh keeps the same file as `{blocked, checked_at}`.
    os.makedirs(mp.state_dir(), exist_ok=True)
    with open(mp.blocklist_path(), "w") as f:
        json.dump({"blocked": [_blk()], "checked_at": time.time()}, f)
    bl = mp.blocklist()
    assert bl["status"] == "ok" and bl["blocked"] == [_blk()]
    env["api"].fail = "offline"
    bl = mp.blocklist(force=True)
    assert bl["status"] == "stale" and bl["blocked"] == [_blk()]


def test_blocklist_parsing_fails_closed(env):
    env["api"].blocked = [_blk(level="maybe"), _blk(slug="../x"), _blk(commit="short"),
                          _blk(slug="other-app", commit="b" * 40, level="soft", reason=None)]
    got = mp.blocklist()["blocked"]
    assert [(b["slug"], b["level"]) for b in got] == [("sine-starter", "hard"), ("other-app", "soft")]
    assert got[1]["reason"] == ""


# --- the open gate ------------------------------------------------------------------

def test_open_gate_answers_423_on_render_and_app_entry(env, tmp_path, monkeypatch):
    from fused_render.server import create_app

    app_dir = tmp_path / "ws" / "app" / "sine-starter"
    (app_dir / "lib").mkdir(parents=True)
    _git(app_dir, "init", "-q")
    (app_dir / "index.html").write_text(PAGE)
    (app_dir / "lib" / "page.html").write_text("<html>sub</html>")
    notice = {"blocked": {"level": "hard", "reason": "Reads saved passwords",
                          "what_to_do": "Rotate your keys", "rolled_back_to": None}}
    asked = []
    _stub_contrib(monkeypatch, "blocks", gate=lambda root: asked.append(root) or notice)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    for preview in ({}, {"_preview": "1"}):
        r = client.get("/render", params={"path": str(app_dir / "index.html"), **preview})
        assert r.status_code == 423 and r.json() == notice
    # A page deeper in the copy is gated by the copy's repo root.
    r = client.get("/render", params={"path": str(app_dir / "lib" / "page.html")})
    assert r.status_code == 423
    assert set(asked) == {str(app_dir)}
    r = client.get("/api/apps/entry", params={"path": str(app_dir)})
    assert r.status_code == 423 and r.json() == notice
    # A browser / an iframe framing the app gets the notice as HTML, not JSON;
    # /api/apps/entry stays JSON whatever it is asked for.
    notice["blocked"]["reason"] = "Reads <saved> passwords"
    notice["blocked"]["rolled_back_date"] = "Oct 20"
    r = client.get("/render", params={"path": str(app_dir / "index.html")},
                   headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 423 and r.headers["content-type"].startswith("text/html")
    assert "Reads &lt;saved&gt; passwords" in r.text and "Rotate your keys" in r.text
    assert "Rolled back to the release of Oct 20." in r.text
    assert "href='/apps/" in r.text and "target='_top'" in r.text and "<script" not in r.text
    r = client.get("/api/apps/entry", params={"path": str(app_dir)}, headers={"Accept": "text/html"})
    assert r.status_code == 423 and r.json() == notice
    # No block: both open as ever.
    _stub_contrib(monkeypatch, "blocks", gate=lambda root: None)
    assert client.get("/render", params={"path": str(app_dir / "index.html")}).status_code == 200
    r = client.get("/api/apps/entry", params={"path": str(app_dir)})
    assert r.status_code == 200 and r.json()["entry"].endswith("index.html")
    # A gate that crashes never takes pages down.
    _stub_contrib(monkeypatch, "blocks", gate=lambda root: 1 / 0)
    assert client.get("/render", params={"path": str(app_dir / "index.html")}).status_code == 200


def test_soft_block_rides_app_entry_and_apps_rows(env, tmp_path):
    from fused_render.server import create_app

    app_dir = tmp_path / "ws" / "app" / "sine-starter"
    app_dir.mkdir(parents=True)
    _git(app_dir, "init", "-q")
    # The record binds to the repo by its origin (store._bound), like a real copy.
    _git(app_dir, "remote", "add", "origin", "https://github.com/alice/sine-starter")
    (app_dir / "index.html").write_text(PAGE)
    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    _market_record(str(app_dir), "sine-starter", commit,
                   block={"level": "soft", "commit": commit, "reason": "Sends data to a new server",
                          "what_to_do": "Check before opening", "at": "2026-10-26", "rolled_back_to": None,
                          "conflict": False, "confirmed_at": None})
    client = TestClient(create_app(start_dir=str(tmp_path)))
    # Contract 1: an unconfirmed soft block shuts every run door with a 423
    # carrying `level: "soft"` — the app page asks first off it.
    r = client.get("/api/apps/entry", params={"path": str(app_dir)})
    assert r.status_code == 423
    b = r.json()["blocked"]
    assert (b["level"], b["reason"], b["what_to_do"], b["confirmed"]) == (
        "soft", "Sends data to a new server", "Check before opening", False)
    page = client.get("/render", params={"path": str(app_dir / "index.html")},
                      headers={"Accept": "text/html"})
    assert page.status_code == 423 and "asks before it opens" in page.text
    run = client.post("/api/run", json={"html": str(app_dir / "index.html"), "py": "x.py"},
                      headers=FUSED)
    assert run.status_code == 423 and run.json()["blocked"]["level"] == "soft"
    # confirm-block is the way through — the action route answers it with
    # every switch off (a blocked copy's panel route, §18.8).
    r = client.post("/api/contrib/action", json={"path": str(app_dir), "action": "confirm-block"},
                    headers=FUSED)
    assert r.status_code == 200 and r.json().get("ok") is not False, r.text
    r = client.get("/api/apps/entry", params={"path": str(app_dir)})
    assert r.status_code == 200 and r.json()["entry"].endswith("index.html")
    assert r.json()["contrib"]["block"]["confirmed"] is True
    assert client.get("/render", params={"path": str(app_dir / "index.html")}).status_code == 200
    rows = client.get("/api/apps").json()["apps"]
    [row] = [x for x in rows if os.path.basename(x["path"].rstrip("/")) == "sine-starter"]
    assert row["contrib"]["block"]["level"] == "soft"
    # No block, no key.
    from fused_render.contrib import store

    store.update(str(app_dir), lambda rec: rec.update(block=None) or rec)
    assert "contrib" not in client.get("/api/apps/entry", params={"path": str(app_dir)}).json()


# --- assets ----------------------------------------------------------------------

def test_asset_proxy_routes_and_caching(env):
    client = _client()
    r = client.get("/api/marketplace/asset/local-chat/icon.svg")
    assert r.status_code == 200 and r.content == b"<svg/>"
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.headers["cache-control"] == "no-cache"
    entry = next(a for a in client.get("/api/marketplace/catalog").json()["apps"] if a["slug"] == "local-chat")
    r2 = client.get(entry["icon_url"])
    assert r2.status_code == 200 and r2.headers["cache-control"] == "max-age=86400"
    r3 = client.get("/api/marketplace/asset/local-chat/icon.svg?v=1")
    assert r3.status_code == 200 and r3.headers["cache-control"] == "no-cache"
    data, _ctype, version = mp.get_asset_versioned("local-chat", "icon.svg")
    assert data == b"<svg/>" and entry["icon_url"].endswith(f"?v={version}")
    # Fetched from the repo root at the approved commit (no slug dir), once.
    row = env["api"].apps["local-chat"]["row"]
    base = f"https://raw.githubusercontent.com/alice/local-chat/{row['commit']}/"
    assert set(env["raw"]["urls"]) == {base + "icon.svg"}
    assert os.path.isfile(os.path.join(mp.state_dir(), "assets", row["commit"], "local-chat", "icon.svg"))
    assert client.get("/api/marketplace/asset/local-chat/preview.png").content == b"PNGDATA"
    assert client.get("/api/marketplace/asset/local-chat/missing.png").status_code == 404


@pytest.mark.parametrize("path", [
    "../sine-starter/index.html", "a/../../etc/passwd", "%2e%2e/README.md",
    "/etc/passwd", ".git/../../x", "",
])
def test_asset_path_traversal_rejected(env, path):
    assert mp.get_asset("local-chat", path) is None
    assert mp.get_asset("../local-chat", "icon.svg") is None
    r = _client().get(f"/api/marketplace/asset/local-chat/{quote(path, safe='')}")
    assert r.status_code == 404
    assert not any("../" in u or "/etc/" in u for u in env["raw"].get("urls", []))


def test_asset_for_an_app_without_a_github_base_is_404(env):
    row = env["api"].apps["local-chat"]["row"]
    row.update(repo=None, source_url="https://example.com/elsewhere", icon_url="https://example.com/icon.svg")
    assert mp.get_asset("local-chat", "icon.svg") is None
    assert _entry("local-chat")["icon_url"] is None


def test_asset_proxy_serves_only_icon_preview_or_listed_files(env):
    mp.catalog()
    api = env["api"]
    api.apps["local-chat"]["files"]["secret.env"] = b"TOKEN"
    detail = api._marketplace_app_detail

    def unlisted(slug, client_version=None):
        [row] = detail(slug, client_version)
        row["files"] = [f for f in row["files"] if f["path"] != "secret.env"]
        return [row]

    api._marketplace_app_detail = unlisted
    assert mp.get_asset("local-chat", "secret.env") is None
    assert not any(u.endswith("secret.env") for u in env["raw"].get("urls", []))
    before = len(_calls(env, "marketplace_app_detail"))
    assert mp.get_asset("local-chat", "icon.svg")[0] == b"<svg/>"
    assert len(_calls(env, "marketplace_app_detail")) == before
    assert mp.get_asset("local-chat", "index.html")[0] == b"<html>chat</html>"
    assert before == 1 and len(_calls(env, "marketplace_app_detail")) == 1


# --- detail --------------------------------------------------------------------

def _calls(env, endpoint):
    return [p for e, p in env["api"].calls if e == endpoint]


def test_detail_found_and_not_found(env):
    client = _client()
    d = client.get("/api/marketplace/app/local-chat").json()
    assert d["status"] == "ok" and d["app"]["slug"] == "local-chat"
    assert d["readme"].startswith("# Local Chat")
    assert d["readme_base"] == "/api/marketplace/asset/local-chat/"
    assert {"path": "preview.png", "size": 7} in d["files"]
    row = env["api"].apps["local-chat"]["row"]
    assert d["releases"] == [{"commit": row["commit"], "at": "2026-10-20T10:00:00Z",
                              "whats_new": "1.2.0 notes", "version": "1.2.0", "blocked": None}]
    r = client.get("/api/marketplace/app/nope-nope")
    assert r.status_code == 404 and r.json()["status"] == "error"
    assert client.get("/api/marketplace/app/..%2Fx").status_code == 404


def test_detail_falls_back_to_the_snapshot_when_the_api_is_down(env):
    mp.catalog()
    env["api"].fail = "the Community answered 502"
    d = mp.app_detail("local-chat")
    assert d["status"] == "ok" and d["app"]["slug"] == "local-chat"
    assert d["readme"] == "" and d["files"] == [] and d["releases"] == d["app"]["releases"]
    assert mp.app_detail("unknown-app") is None


def test_readme_is_capped(env, monkeypatch):
    monkeypatch.setattr(mp, "MAX_README_BYTES", 10)
    assert mp.app_detail("local-chat")["readme"] == "# Local Ch"


def test_detail_memo_answers_without_a_network_call(env):
    mp.catalog()
    first = mp.app_detail("local-chat")
    n = len(_calls(env, "marketplace_app_detail"))
    assert n == 1 and ("marketplace_app_detail", mp.DETAIL_TIMEOUT_S) in env["api"].timeouts
    again = mp.app_detail("local-chat")
    assert again == first and len(_calls(env, "marketplace_app_detail")) == n
    assert mp.get_asset("local-chat", "readme.md") is not None
    assert len(_calls(env, "marketplace_app_detail")) == n
    env["api"].set_files("local-chat", {"index.html": "<html>2</html>", "icon.svg": "<svg2/>"}, "1.3.0")
    mp.catalog(refresh=True)
    mp.app_detail("local-chat")
    assert len(_calls(env, "marketplace_app_detail")) == n + 1


def test_detail_memo_is_bounded(env, monkeypatch):
    monkeypatch.setattr(mp, "_DETAIL_MEMO_MAX", 3)
    for i in range(5):
        mp._memo_put(("a", str(i)), {"i": i})
    assert list(mp._DETAIL_MEMO) == [("a", "2"), ("a", "3"), ("a", "4")]


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
    assert _client().get("/api/marketplace/asset/local-chat/icon.svg").status_code == 200
    assert built == []
    cat = mp.catalog()
    assert built == [1] and cat["status"] == "ok" and cat["stale"] is True


def test_catalog_does_not_wait_behind_a_running_rebuild(env, monkeypatch):
    _old_cache(env)
    monkeypatch.setattr(mp, "_build_snapshot", lambda: pytest.fail("built while locked"))
    assert mp._CATALOG_LOCK.acquire(timeout=1)
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
    assert mp.catalog()["status"] == "ok" and built == [1]
    assert mp.catalog(refresh=True)["status"] == "ok" and built == [1]
    cache = mp._read_cache()
    cache["checked_at"] = time.time() - 61
    mp._write_cache(cache)
    mp.catalog(refresh=True)
    assert built == [1, 1]


def test_old_backends_and_the_package_path_are_gone():
    # The GitHub-backed catalog before the API…
    for name in ("SOURCE_GIT", "SOURCE_API", "DEFAULT_FEATURED", "CATEGORY_ORDER", "repo_config",
                 "forced_source", "mirror_dir", "_ls_tree", "_raw_url", "_download_api",
                 "_copy_from_mirror", "_write_blobs", "_git", "_git_out", "_mirror_lock",
                 # …and the release-package installer (§18.0 M2: no app code from Fused storage).
                 "_stage", "_download_package", "_extract_package", "_fetch_release",
                 "_prune_dropped", "_write_stamp", "MAX_PACKAGE_BYTES", "KEEP", "_sweep_staging",
                 "STAGING_TTL_S"):
        assert not hasattr(mp, name), name
    src = inspect.getsource(mp)
    assert "marketplace_release" not in src.replace("`marketplace_release`", "")


def test_snapshot_fields_survive_odd_types(env):
    api = env["api"]
    for a in api.apps.values():
        a["row"]["catalog_version"] = 20261015
    orig_cats, orig_cols = api._marketplace_categories, api._marketplace_collections
    api._marketplace_categories = lambda: orig_cats() + ["junk", {"id": "weird", "label": "W", "count": "n/a"}]
    api._marketplace_collections = lambda new_and_updated_limit: orig_cols(new_and_updated_limit) + ["junk", 7]
    cat = mp.catalog()
    assert cat["status"] == "ok"
    assert cat["catalog_version"] == "20261015" and isinstance(cat["generated_at"], str)
    assert {"id": "weird", "label": "W", "count": 0} in cat["categories"]
    assert [c["id"] for c in cat["collections"]] == ["starters", "local-ai", "new-and-updated"]


def test_shelf_rows_without_a_shelf_id_are_skipped():
    apps = {"a-app": {}}
    out = mp._group_collections([
        {"collection": "shelf", "shelf_id": None, "title": "Nameless", "position": 0, "slug": "a-app"},
        {"collection": "shelf", "shelf_id": "", "title": "Blank", "position": 0, "slug": "a-app"},
        {"collection": "shelf", "shelf_id": "x", "title": "X", "position": 0, "slug": "a-app"},
        {"collection": "new_and_updated", "shelf_id": None, "title": "New", "position": 0, "slug": "a-app"},
    ], apps)
    assert [(c["id"], c["kind"]) for c in out] == [("x", "shelf"), ("new-and-updated", "new_and_updated")]


def test_asset_cache_is_pruned_when_the_catalog_has_no_version(env):
    env["api"].catalog_version = None
    mp.catalog()
    mp.get_asset("local-chat", "icon.svg")
    root = os.path.join(mp.state_dir(), "assets")
    old = env["api"].apps["local-chat"]["row"]["commit"]
    assert os.listdir(root) == [old]
    env["api"].set_files("local-chat", {"index.html": "<html>2</html>", "icon.svg": "<svg2/>"}, "1.3.0")
    mp.catalog(refresh=True)
    assert not os.path.exists(os.path.join(root, old))


# --- the bots' door (bots/tools._market_target) ---------------------------------------

def test_bot_market_target_reads_the_same_plan(env, gh_world):
    from fused_render.bots import tools

    slug, update, err = tools._market_target({"slug": "sine-starter"})
    assert (slug, update, err) == ("sine-starter", False, "")
    assert tools._market_where("sine-starter").endswith(os.path.join("app", "sine-starter"))
    # A GitHub-link copy: switching is the user's call, never a bot's.
    _link_copy(env, gh_world)
    _s, _u, err = tools._market_target({"slug": "sine-starter"})
    assert "the user decides that in the app" in err
    _s, _u, err = tools._market_target({"slug": "sine-starter", "update": True})
    assert "the user decides that in the app" in err


def test_bot_market_target_installed_and_legacy(env):
    from fused_render.bots import tools

    commit = env["api"].apps["sine-starter"]["row"]["commit"]
    _market_record(_app_dir(env, "sine-starter"), "sine-starter", commit)
    _s, update, err = tools._market_target({"slug": "sine-starter"})
    assert err.startswith("already installed at")
    _s, update, err = tools._market_target({"slug": "sine-starter", "update": True})
    assert (update, err) == (True, "")
    _legacy_copy(env, "local-chat")
    _s, _u, err = tools._market_target({"slug": "local-chat"})
    assert err.startswith("Switch to the reviewed version first")


def test_a_community_copy_is_never_uploaded_and_shares_its_listing(env, tmp_path):
    """P1, the kind decides: a `marketplace` copy is someone else's code —
    the Fused upload is refused — and the link it hands on is its LISTING
    page (the reviewed install), never the author's GitHub link (that one
    follows the author's unreviewed tip)."""
    from fused_render import backend_canvas, share_app

    app_dir = tmp_path / "ws" / "app" / "sine-starter"
    app_dir.mkdir(parents=True)
    _git(app_dir, "init", "-q")
    _git(app_dir, "remote", "add", "origin", "https://github.com/alice/sine-starter")
    (app_dir / "index.html").write_text(PAGE)
    _market_record(str(app_dir), "sine-starter", "c" * 40)
    api = FastAPI()
    api.include_router(share_app.router)
    c = TestClient(api)
    st = c.get("/api/share/status", params={"path": str(app_dir)}).json()
    assert (st["can_share"], st["refusal_code"]) == (False, "github_copy")
    r = c.post("/api/share/publish", data={"path": str(app_dir)}, headers=FUSED)
    assert r.status_code == 409 and r.json()["code"] == "github_copy"
    link = share_app.github_link_state(str(app_dir), copy_status={"changes": 0})
    assert link["code"] == "contributor"
    assert link["url"] == backend_canvas.listing_page_url("sine-starter")
    # No slug on the record: no link at all, never the author's GitHub one.
    from fused_render.contrib import store

    store.update(str(app_dir), lambda rec: rec.update(slug=None) or rec)
    link = share_app.github_link_state(str(app_dir), copy_status={"changes": 0})
    assert link["ok"] is False and link["url"] is None
