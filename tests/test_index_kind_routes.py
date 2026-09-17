"""`/api/index/*` routes threading a `kind` parameter to a registered
`IndexKind`'s own store, instead of always answering for "files".

See fused_render/index/specs/index-plugins.md and
DECISIONS-index-plugins.md's "router generalization" entry for what is (and
is not) generalized here: `/config`, `/scan`, `/status`, `/cancel`, `/delete`
and `/query` work for any registered kind; `/stats`/`/search`/`/ask` remain
files-tree-shaped by design (a flat kind has no directory tree for those to
describe); `/rank` branches to `search_apps_ranked` for any kind other than
"files".
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from fused_render.index import apps_kind
from fused_render.index.config import IndexConfig, index_dir
from fused_render.index.store import Sink, compact
from fused_render.server import create_app


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway shell home, so every kind's store lands under it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def _seed_apps_store(root="/apps", rows=()):
    """Build a real, compacted "apps"-kind store at the exact directory
    `load_config(kind="apps")` will resolve to under the current
    FUSED_RENDER_HOME (see `home` fixture) — the same construction
    tests/test_index_apps_search.py uses, aimed at the real on-disk layout
    instead of a scratch directory, so an HTTP request against the running
    app finds these rows."""
    cfg = IndexConfig(dir=index_dir("apps"), kind="apps")
    shards = os.path.join(cfg.dir, "run", "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows, kind="apps")
    # One row per `apps_kind.COLUMNS` entry — a real registered kind's full
    # row shape, not the minimal stand-in test_index_apps_search.py's own
    # throwaway kind gets away with, since this seeds the actual built-in
    # "apps" kind the running app registers at import time.
    payload = [{
        "name": n, "path": p, "entry": p, "entry_html": p, "id": p,
        "preview_image": "", "category": "", "icon": "", "icon_mtime": 0.0,
        "title": n, "updated_at": u,
    } for n, p, u in rows]
    sink.add(root, "s", ("sig", payload, 0, 1, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- unknown kind ----------------------------------------------------------

@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/api/index/config", {"params": {"kind": "bogus"}}),
    ("post", "/api/index/scan", {"json": {"kind": "bogus"},
                                 "headers": {"X-Fused": "1"}}),
    ("get", "/api/index/status", {"params": {"kind": "bogus"}}),
    ("get", "/api/index/rank", {"params": {"kind": "bogus", "root": "/x",
                                           "q": "a"}}),
    ("post", "/api/index/delete", {"json": {"kind": "bogus"},
                                   "headers": {"X-Fused": "1"}}),
])
def test_an_unregistered_kind_is_a_400(home, tmp_path, method, path, kwargs):
    client = _client(tmp_path)
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 400
    assert "bogus" in resp.json()["error"]


# -- default kind stays "files" — zero-migration ---------------------------

def test_config_with_no_kind_answers_for_the_original_files_store(home, tmp_path):
    resp = _client(tmp_path).get("/api/index/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["location"] == os.path.join(str(home), "index")


def test_config_for_the_apps_kind_is_nested_under_its_own_directory(home, tmp_path):
    resp = _client(tmp_path).get("/api/index/config", params={"kind": "apps"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["location"] == os.path.join(str(home), "index", "apps")


def test_deleting_one_kind_never_touches_a_sibling_kind(home, tmp_path):
    _seed_apps_store(rows=[("solo", "/apps/solo/index.html", 1.0)])
    client = _client(tmp_path)
    # A "files" store exists too (an empty one is fine; delete_store must
    # tolerate "nothing scanned yet" the same way it always has).
    before = client.get("/api/index/status", params={"kind": "apps"}).json()
    assert before["has_index"] is True

    resp = client.post("/api/index/delete", json={"kind": "files"},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 200

    after = client.get("/api/index/status", params={"kind": "apps"}).json()
    assert after["has_index"] is True, "an unrelated kind's delete must not touch this store"


# -- /api/index/rank for a flat (non-"files") kind --------------------------

def test_rank_for_the_apps_kind_ranks_against_search_apps_ranked(home, tmp_path):
    _seed_apps_store(rows=[
        ("editor", "/apps/editor/index.html", 100.0),
        ("thing", "/apps/editor-folder/thing/index.html", 200.0),
    ])
    resp = _client(tmp_path).get("/api/index/rank",
                                 params={"kind": "apps", "q": "editor"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["reason"] == ""
    assert [h["rel"] for h in body["hits"]] == [
        "/apps/editor/index.html", "/apps/editor-folder/thing/index.html"]
    # The wire-drop fields never reach an "apps" hit either.
    for h in body["hits"]:
        assert "score" not in h and "tier" not in h


def test_rank_for_the_apps_kind_does_not_require_a_root(home, tmp_path):
    """`root` is a "files"-tree concept (the box's own folder); a flat kind
    has no such thing, so omitting it must not 400 the way it would for
    "files"."""
    _seed_apps_store(rows=[("solo", "/apps/solo/index.html", 1.0)])
    resp = _client(tmp_path).get("/api/index/rank",
                                 params={"kind": "apps", "q": "solo"})
    assert resp.status_code == 200
    assert resp.json()["hits"][0]["rel"] == "/apps/solo/index.html"


# -- scan_roots' per-kind default -------------------------------------------

def test_scan_roots_defaults_the_apps_kind_to_the_app_workspace(home, tmp_path, monkeypatch):
    from fused_render.index.config import IndexConfig
    from fused_render.server.routers import index as index_router

    workspace = tmp_path / "Fused"
    workspace.mkdir()
    monkeypatch.setattr(index_router, "fused_dir", lambda: str(workspace))
    cfg = IndexConfig(dir=index_dir("apps"), kind="apps")
    assert index_router.scan_roots(cfg) == [
        index_router.runner.canonical_root(str(workspace))]


def test_scan_roots_still_defaults_files_to_home(home, tmp_path):
    from fused_render.index.config import IndexConfig
    from fused_render.server.routers import index as index_router

    cfg = IndexConfig(dir=index_dir("files"), kind="files")
    assert index_router.scan_roots(cfg) == [
        index_router.runner.canonical_root("~")]


# -- /api/index/search degrades to zero rows for a flat (non-"files") kind --

def test_search_for_the_apps_kind_is_zero_rows_not_a_500(home, tmp_path):
    """`search_under` (query.py's `index_search`) reaches for `dir`/`depth`
    columns a flat "apps" store's schema does not have. Per SPEC-index-
    plugins.md, an index that cannot answer degrades to zero rows, silently
    — never an error — the same posture exported_apps.py and git_repos.py
    already hold for their own index reads."""
    _seed_apps_store(rows=[("solo", "/apps/solo/index.html", 1.0)])
    resp = _client(tmp_path).get(
        "/api/index/search",
        params={"kind": "apps", "root": "/apps", "q": "solo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["covered"] is False
    assert body["entries"] == []
    assert body["total"] == 0


# -- /api/index/kinds — what the management page lists ----------------------

def test_kinds_lists_files_first_then_every_registered_kind(home, tmp_path):
    resp = _client(tmp_path).get("/api/index/kinds")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # "files" always leads: it predates the plugin registry and is never
    # itself inside kinds.registered() (see `_kind_param`'s docstring).
    assert body["kinds"][0] == "files"
    # The built-in "apps" kind is registered at import time of this router
    # (`apps_kind.register_builtin()`, unit 13's own note), so it is always
    # present regardless of what a running server has scanned.
    assert "apps" in body["kinds"][1:]
