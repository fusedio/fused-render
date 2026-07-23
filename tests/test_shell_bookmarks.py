"""Tests for GET/PUT /api/bookmarks (fused_render/shell/bookmarks.py) — the
server-side bookmark store at ~/.fused-render/bookmarks.json.

FUSED_RENDER_HOME is redirected to a tmp dir so no test touches the real home.
"""
import json
import time
from urllib.parse import quote

from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.shell import bookmarks as bookmarks_mod
from fused_render.shell import mounts as mounts_mod


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


def test_get_reports_not_exists_when_absent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/bookmarks")
    assert resp.status_code == 200
    assert resp.json() == {"exists": False, "bookmarks": [], "missing": []}


def test_put_then_get_roundtrips(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    tree = [
        {"id": "1", "name": "a", "url": "/view/x", "created_at": 1},
        {
            "id": "2",
            "type": "folder",
            "name": "F",
            "collapsed": False,
            "children": [{"id": "3", "name": "b", "url": "/view/y", "created_at": 2}],
        },
    ]
    put = client.put("/api/bookmarks", json=tree, headers=FUSED)
    assert put.status_code == 200
    assert put.json() == {"ok": True, "count": 2}

    # File written under the overridden home, valid JSON.
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert saved == tree

    get = client.get("/api/bookmarks")
    # Neither "/x" nor "/y" exists on disk -> both flagged missing.
    assert get.json() == {"exists": True, "bookmarks": tree, "missing": ["1", "3"]}


def test_empty_list_still_reports_exists(tmp_path, monkeypatch):
    # A user who deleted every bookmark leaves []; exists must stay true —
    # an empty file is still a valid, present tree.
    client, _ = _client(tmp_path, monkeypatch)
    assert client.put("/api/bookmarks", json=[], headers=FUSED).status_code == 200
    assert client.get("/api/bookmarks").json() == {"exists": True, "bookmarks": [], "missing": []}


def test_corrupt_file_reports_not_exists(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    (home / "bookmarks.json").write_text("{ not json", encoding="utf-8")
    assert client.get("/api/bookmarks").json() == {"exists": False, "bookmarks": [], "missing": []}


def test_put_without_fused_header_is_rejected(tmp_path, monkeypatch):
    # D3 guard: a blind cross-origin PUT (no X-Fused) must not write.
    client, home = _client(tmp_path, monkeypatch)
    resp = client.put("/api/bookmarks", json=[{"id": "1", "name": "a", "url": "/x", "created_at": 1}])
    assert resp.status_code == 403
    assert not (home / "bookmarks.json").exists()


def test_put_overwrites_last_write_wins(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.put("/api/bookmarks", json=[{"id": "1", "name": "a", "url": "/view/x", "created_at": 1}], headers=FUSED)
    client.put("/api/bookmarks", json=[{"id": "2", "name": "b", "url": "/view/y", "created_at": 2}], headers=FUSED)
    got = client.get("/api/bookmarks").json()
    assert [b["id"] for b in got["bookmarks"]] == ["2"]


# --- D97 duplicate-name migration (GET-time, one write, idempotent) -----------


def _bm(id, name, created_at):
    return {"id": id, "name": name, "url": f"/view/{id}", "created_at": created_at}


def _write_tree(home, tree):
    home.mkdir(parents=True, exist_ok=True)
    (home / "bookmarks.json").write_text(json.dumps(tree), encoding="utf-8")


def test_migration_suffixes_duplicates_oldest_keeps_name(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Newest listed first: created_at, not list order, decides who keeps "a".
    _write_tree(home, [_bm("2", "a", 20), _bm("1", "a", 10), _bm("3", "a", 30)])
    got = {b["id"]: b["name"] for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert got == {"1": "a", "2": "a-1", "3": "a-2"}
    # Migrated tree persisted to disk.
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert {b["id"]: b["name"] for b in saved} == got


def test_migration_is_case_insensitive_and_global_across_folders(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    folder = {"id": "f", "type": "folder", "name": "a", "collapsed": False,
              "children": [_bm("2", "A", 20)]}
    _write_tree(home, [_bm("1", "a", 10), folder])
    got = client.get("/api/bookmarks").json()["bookmarks"]
    assert got[0]["name"] == "a"
    # Folder child collides with the top-level bookmark despite the case, but
    # the folder's own name "a" is a separate namespace and stays untouched.
    assert got[1]["name"] == "a"
    assert got[1]["children"][0]["name"] == "A-1"


def test_migration_suffix_skips_existing_names(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # A pre-existing literal "a-1" blocks that suffix: the duplicate jumps to -2.
    _write_tree(home, [_bm("1", "a", 10), _bm("2", "a", 20), _bm("3", "a-1", 30)])
    got = {b["id"]: b["name"] for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert got == {"1": "a", "2": "a-2", "3": "a-1"}


def test_migration_keys_on_sanitized_filename_stem(tmp_path, monkeypatch):
    # `a/b` and `a:b` are distinct strings but both export as `a-b.bookmark`,
    # so they must count as duplicates (key = sanitized lowercase stem). The
    # newer one gets the first suffix whose sanitized key is free.
    client, home = _client(tmp_path, monkeypatch)
    _write_tree(home, [_bm("1", "a/b", 10), _bm("2", "a:b", 20), _bm("3", "A-B", 30)])
    got = {b["id"]: b["name"] for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert got == {"1": "a/b", "2": "a:b-1", "3": "A-B-2"}
    # Idempotent: the migrated tree survives a second GET byte-identical.
    saved = (home / "bookmarks.json").read_text(encoding="utf-8")
    client.get("/api/bookmarks")
    assert (home / "bookmarks.json").read_text(encoding="utf-8") == saved


def test_migration_suffix_dodges_sanitized_collision(tmp_path, monkeypatch):
    # A literal "a-b-1" occupies the key the suffixed "a:b" would take, so the
    # duplicate jumps to "a:b-2" (sanitized key "a-b-2").
    client, home = _client(tmp_path, monkeypatch)
    _write_tree(home, [_bm("1", "a/b", 10), _bm("2", "a:b", 20), _bm("3", "a-b-1", 5)])
    got = {b["id"]: b["name"] for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert got == {"1": "a/b", "2": "a:b-2", "3": "a-b-1"}


def test_migration_is_idempotent_and_writes_only_on_change(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    _write_tree(home, [_bm("1", "a", 10), _bm("2", "a", 20)])
    first = client.get("/api/bookmarks").json()["bookmarks"]
    saved = (home / "bookmarks.json").read_text(encoding="utf-8")
    # Second GET: already unique, so the exact bytes on disk are untouched
    # (write_json would reformat — unchanged text proves no write happened).
    assert client.get("/api/bookmarks").json()["bookmarks"] == first
    assert (home / "bookmarks.json").read_text(encoding="utf-8") == saved


# --- POST /api/bookmarks/export (.bookmark file, SB-8/D98) --------------------


def _export_body(tmp_path, **overrides):
    body = {
        "dir": str(tmp_path),
        "filename": "sales-dash.bookmark",
        "content": '{\n  "version": 1,\n  "name": "sales-dash",\n  "kind": "single",\n  "path": "a.parquet",\n  "search": "sort=name"\n}\n',
    }
    body.update(overrides)
    return body


def test_export_writes_content_verbatim(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    body = _export_body(tmp_path)
    resp = client.post("/api/bookmarks/export", json=body, headers=FUSED)
    assert resp.status_code == 200
    path = resp.json()["path"]
    assert path == str(tmp_path / "sales-dash.bookmark")
    # Exact bytes: the frontend owns the formatting, the server must not touch it.
    with open(path, encoding="utf-8") as f:
        assert f.read() == body["content"]


def test_export_overwrites_existing_file(tmp_path, monkeypatch):
    # Re-saving refreshes the snapshot: the name is unique (D97), so an
    # existing file is a stale copy of the same bookmark.
    client, _ = _client(tmp_path, monkeypatch)
    (tmp_path / "sales-dash.bookmark").write_text("stale", encoding="utf-8")
    body = _export_body(tmp_path)
    assert client.post("/api/bookmarks/export", json=body, headers=FUSED).status_code == 200
    assert (tmp_path / "sales-dash.bookmark").read_text(encoding="utf-8") == body["content"]


def test_export_without_fused_header_is_rejected(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/api/bookmarks/export", json=_export_body(tmp_path))
    assert resp.status_code == 403
    assert not (tmp_path / "sales-dash.bookmark").exists()


def test_export_rejects_bad_dir(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for dir_ in ["relative/dir", str(tmp_path / "missing"), 7]:
        body = _export_body(tmp_path, dir=dir_)
        assert client.post("/api/bookmarks/export", json=body, headers=FUSED).status_code == 400
    # A file is not a directory either.
    target = tmp_path / "a.parquet"
    target.write_text("x", encoding="utf-8")
    body = _export_body(tmp_path, dir=str(target))
    assert client.post("/api/bookmarks/export", json=body, headers=FUSED).status_code == 400


def test_export_rejects_bad_filename(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for filename in [
        "no-suffix.txt",  # wrong extension
        ".bookmark",  # empty stem
        "sub/dir.bookmark",  # path separator
        "sub\\dir.bookmark",  # backslash separator
        "..bookmark",  # traversal-shaped stem
        "",  # empty
    ]:
        body = _export_body(tmp_path, filename=filename)
        assert client.post("/api/bookmarks/export", json=body, headers=FUSED).status_code == 400


def test_export_rejects_garbage_content(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    for content in [
        "not json at all",
        "[1, 2]",  # JSON, but not an object
        '{"name": "x"}',  # no version
        '{"version": "1"}',  # version not an int
        '{"version": true}',  # bool is not a version
    ]:
        body = _export_body(tmp_path, content=content)
        assert client.post("/api/bookmarks/export", json=body, headers=FUSED).status_code == 400
    assert not (tmp_path / "sales-dash.bookmark").exists()


# --- GET /api/bookmark-file (.bookmark open flow, SB-9/D99) -------------------


def _write_bookmark(tmp_path, name="sales-dash.bookmark", doc=None):
    if doc is None:
        doc = {"version": 1, "name": "sales-dash", "kind": "single",
               "path": "a.parquet", "search": "sort=name"}
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path, doc


def test_bookmark_file_returns_dir_and_content(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    path, doc = _write_bookmark(tmp_path)
    resp = client.get("/api/bookmark-file", params={"path": str(path)})
    assert resp.status_code == 200
    # `dir` is the file's own directory — what the frontend resolves the
    # record's relative paths against.
    assert resp.json() == {"dir": str(tmp_path), "bookmark": doc}


def test_bookmark_file_rejects_relative_path(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/bookmark-file", params={"path": "rel/sales.bookmark"})
    assert resp.status_code == 400


def test_bookmark_file_rejects_wrong_extension(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    other = tmp_path / "a.json"
    other.write_text("{}", encoding="utf-8")
    resp = client.get("/api/bookmark-file", params={"path": str(other)})
    assert resp.status_code == 400


def test_bookmark_file_missing_file_is_404(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/api/bookmark-file", params={"path": str(tmp_path / "gone.bookmark")})
    assert resp.status_code == 404


def test_bookmark_file_rejects_malformed_json(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    path = tmp_path / "bad.bookmark"
    path.write_text("{ not json", encoding="utf-8")
    resp = client.get("/api/bookmark-file", params={"path": str(path)})
    assert resp.status_code == 400
    # A JSON array is not a bookmark record either.
    path.write_text("[1, 2]", encoding="utf-8")
    assert client.get("/api/bookmark-file", params={"path": str(path)}).status_code == 400


def test_bookmark_file_rejects_unsupported_version(tmp_path, monkeypatch):
    # Forward-compat: a v2 file from a newer build must fail with a clear
    # message, not redirect somewhere wrong.
    client, _ = _client(tmp_path, monkeypatch)
    path, _ = _write_bookmark(tmp_path, doc={"version": 2, "name": "x", "kind": "single",
                                             "path": "a", "search": ""})
    resp = client.get("/api/bookmark-file", params={"path": str(path)})
    assert resp.status_code == 400
    assert "version" in resp.json()["error"]


# --- tree sanitization + nested folders (GET-time, D121) ---------------------


def _folder(id, name, children):
    return {"id": id, "type": "folder", "name": name, "collapsed": False,
            "children": children}


def test_get_keeps_nested_folder_in_children(tmp_path, monkeypatch):
    # D121: folders nest to arbitrary depth — a folder inside another folder's
    # children is legitimate data and must survive a GET untouched.
    client, home = _client(tmp_path, monkeypatch)
    nested = _folder("sub", "sub", [_bm("x", "x", 0)])
    outer = _folder("f", "F", [_bm("1", "a", 10), nested, _bm("2", "b", 20)])
    _write_tree(home, [outer])
    got = client.get("/api/bookmarks").json()
    assert got["exists"] is True
    kept_ids = [c["id"] for c in got["bookmarks"][0]["children"]]
    assert kept_ids == ["1", "sub", "2"]
    assert got["bookmarks"][0]["children"][1]["children"][0]["id"] == "x"


def test_get_strips_urlless_child_without_touching_valid_siblings(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    garbage = {"id": "g", "name": "garbage"}  # no url, not a folder
    outer = _folder("f", "F", [_bm("1", "a", 10), garbage])
    _write_tree(home, [outer])
    got = client.get("/api/bookmarks").json()["bookmarks"]
    assert [c["id"] for c in got[0]["children"]] == ["1"]
    # Persisted back so the corrupt entry doesn't resurface on the next GET.
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in saved[0]["children"]] == ["1"]


def test_get_strips_garbage_inside_nested_folder(tmp_path, monkeypatch):
    # Sanitization recurses: a urlless dict two folders deep is dropped while
    # its valid siblings and both enclosing folders survive.
    client, home = _client(tmp_path, monkeypatch)
    garbage = {"id": "g", "name": "garbage"}
    inner = _folder("inner", "inner", [garbage, _bm("x", "x", 0), "not-a-dict"])
    outer = _folder("f", "F", [inner, _bm("1", "a", 10)])
    _write_tree(home, [outer])
    got = client.get("/api/bookmarks").json()["bookmarks"]
    inner_got = got[0]["children"][0]
    assert [c["id"] for c in inner_got["children"]] == ["x"]
    assert [c["id"] for c in got[0]["children"]] == ["inner", "1"]


def test_get_leaves_valid_nested_tree_unwritten(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    outer = _folder("f", "F", [_folder("sub", "sub", [_bm("x", "x", 0)]),
                               _bm("1", "a", 10)])
    _write_tree(home, [outer])
    raw = (home / "bookmarks.json").read_text(encoding="utf-8")
    client.get("/api/bookmarks")
    # Compact JSON differs from write_json's output; unchanged bytes prove the
    # no-op GET never wrote.
    assert (home / "bookmarks.json").read_text(encoding="utf-8") == raw


def test_nested_folder_roundtrips_put_then_get(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    tree = [_folder("f", "F", [_folder("sub", "sub", [_bm("x", "deep", 1)]),
                               _bm("1", "top", 2)])]
    assert client.put("/api/bookmarks", json=tree, headers=FUSED).status_code == 200
    # Neither dummy target exists on disk -> both flagged missing, deepest-first
    # (tree order): the nested folder's child, then the top-level bookmark.
    assert client.get("/api/bookmarks").json() == {
        "exists": True, "bookmarks": tree, "missing": ["x", "1"],
    }


def test_migration_dedupes_across_nested_depth(tmp_path, monkeypatch):
    # A bookmark in a grandchild folder shares the global name namespace: it
    # collides with a top-level "a" and gets the suffix (newer created_at).
    client, home = _client(tmp_path, monkeypatch)
    grandchild = _folder("gc", "a", [_bm("2", "a", 20)])
    _write_tree(home, [_bm("1", "a", 10), _folder("f", "a", [grandchild])])
    got = client.get("/api/bookmarks").json()["bookmarks"]
    assert got[0]["name"] == "a"
    # Folder names ("a" twice) are a separate namespace and stay untouched.
    assert got[1]["name"] == "a"
    assert got[1]["children"][0]["name"] == "a"
    assert got[1]["children"][0]["children"][0]["name"] == "a-1"
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert saved[1]["children"][0]["children"][0]["name"] == "a-1"


def test_migration_leaves_unique_tree_unwritten(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Compact JSON (no indent) differs from write_json's output; surviving a GET
    # byte-identical proves the no-duplicates path never writes.
    _write_tree(home, [_bm("1", "a", 10), _bm("2", "b", 20)])
    raw = (home / "bookmarks.json").read_text(encoding="utf-8")
    assert client.get("/api/bookmarks").json()["exists"] is True
    assert (home / "bookmarks.json").read_text(encoding="utf-8") == raw


# --- missing-file flag (GET-time, never persisted) ----------------------------


def _view_url(path, search=""):
    # Encode each segment like the frontend's urlForFsPath (lib/router.ts) —
    # mirrors test_shell_recents.py's helper of the same name.
    encoded = "/".join(quote(s, safe="") for s in str(path).lstrip("/").split("/"))
    return "/view/" + encoded + search


def _make_file(tmp_path, name="a.parquet"):
    f = tmp_path / name
    f.write_text("x", encoding="utf-8")
    return f


def test_missing_flags_deleted_target_without_pruning_it(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    keep = _make_file(tmp_path, "keep.csv")
    gone = _make_file(tmp_path, "gone.csv")
    tree = [_bm("keep", "keep", 1), _bm("gone", "gone", 2)]
    tree[0]["url"] = _view_url(keep)
    tree[1]["url"] = _view_url(gone)
    _write_tree(home, tree)
    gone.unlink()

    got = client.get("/api/bookmarks").json()
    assert got["missing"] == ["gone"]
    # Never pruned from the tree or from disk — same posture as recents (the
    # file may come back).
    assert [b["id"] for b in got["bookmarks"]] == ["keep", "gone"]
    saved = json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))
    assert [b["id"] for b in saved] == ["keep", "gone"]
    # No per-item field added — "missing" is a side-channel, not a tree mutation.
    assert "missing" not in saved[0] and "exists" not in saved[0]


def test_missing_clears_once_target_recreated(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    f = _make_file(tmp_path, "reborn.csv")
    _write_tree(home, [{**_bm("1", "a", 1), "url": _view_url(f)}])
    f.unlink()
    assert client.get("/api/bookmarks").json()["missing"] == ["1"]
    _make_file(tmp_path, "reborn.csv")
    assert client.get("/api/bookmarks").json()["missing"] == []


def test_missing_recurses_into_nested_folders(tmp_path, monkeypatch):
    # D121: a missing bookmark several folders deep is still found.
    client, home = _client(tmp_path, monkeypatch)
    keep = _make_file(tmp_path, "keep.csv")
    inner = _folder("inner", "inner", [{**_bm("gone", "gone", 1), "url": "/view/never-existed"}])
    outer = _folder("f", "F", [inner, {**_bm("keep", "keep", 2), "url": _view_url(keep)}])
    _write_tree(home, [outer])
    assert client.get("/api/bookmarks").json()["missing"] == ["gone"]


def test_missing_never_flags_folders_or_sentinel_urls(tmp_path, monkeypatch):
    # A folder has no url at all; a layout/tab sentinel resolves to no fs
    # target — neither names a real path to confirm gone, so neither is ever
    # flagged, even though nothing on disk backs them.
    client, home = _client(tmp_path, monkeypatch)
    sentinel = {**_bm("panel", "panel", 1), "url": "/view/_panel?_layout=(a,b)"}
    outer = _folder("f", "F", [sentinel])
    _write_tree(home, [outer])
    assert client.get("/api/bookmarks").json()["missing"] == []


def test_missing_never_flags_any_underscore_sentinel_route(tmp_path, monkeypatch):
    # Regression (Bugbot finding on PR #253): every shell sentinel route is
    # genuinely bookmarkable via StaticBreadcrumb — not just _panel/_tab — and
    # _account bookmarks must keep working per D125. Narrowly checking against
    # a `("_panel", "_tab")` allowlist let the rest decode to fake paths like
    # "/_prefs" and get falsely flagged missing; the fix treats ANY `_`-prefixed
    # top-level segment as a sentinel (mirrors recents._decoded_fs_path).
    client, home = _client(tmp_path, monkeypatch)
    tree = [
        {**_bm("prefs", "prefs", 1), "url": "/view/_prefs"},
        {**_bm("templates", "templates", 2), "url": "/view/_templates"},
        {**_bm("mounts", "mounts", 3), "url": "/view/_mounts"},
        {**_bm("account", "account", 4), "url": "/view/_prefs?tab=account"},
        {**_bm("bookmark", "bookmark", 5), "url": "/view/_bookmark?file=%2Ftmp%2Fx.bookmark"},
    ]
    _write_tree(home, tree)
    assert client.get("/api/bookmarks").json()["missing"] == []


def test_missing_check_is_bounded_when_hung(tmp_path, monkeypatch):
    # A stale bookmark sitting on a slow/hung mount must never stall the
    # sidebar's poll. Fail open: a check that outlives the budget is NOT
    # flagged, and the endpoint stays well under the hang duration.
    client, home = _client(tmp_path, monkeypatch)
    urls = [_view_url(_make_file(tmp_path, f"hang{i}.parquet")) for i in range(3)]
    _write_tree(home, [_bm(str(i), str(i), i) | {"url": u} for i, u in enumerate(urls)])

    def _hang(path):
        time.sleep(10)
        return True  # would flag missing if it ever completed within budget

    monkeypatch.setattr(bookmarks_mod.pathops.os.path, "exists", _hang)

    start = time.monotonic()
    resp = client.get("/api/bookmarks")
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert elapsed < 3.0, f"GET /api/bookmarks took {elapsed:.1f}s — not bounded"
    assert resp.json()["missing"] == []  # fail open: nothing confirmed gone


def test_missing_checks_run_concurrently_not_serially(tmp_path, monkeypatch):
    # N bookmarks each with a ~0.5s existence check must complete in ~one
    # sleep (concurrent fan-out), not N sleeps (serial).
    client, home = _client(tmp_path, monkeypatch)
    tree = [_bm(str(i), str(i), i) | {"url": _view_url(tmp_path / f"slow{i}.parquet")}
            for i in range(5)]
    _write_tree(home, tree)

    def _slow(path):
        time.sleep(0.5)
        return False

    monkeypatch.setattr(bookmarks_mod.pathops.os.path, "exists", _slow)

    start = time.monotonic()
    resp = client.get("/api/bookmarks")
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert set(resp.json()["missing"]) == {"0", "1", "2", "3", "4"}
    # Serial would be ~2.5s+; a generous 2.0s ceiling still discriminates
    # cleanly while leaving headroom against scheduling noise under load.
    assert elapsed < 2.0, f"GET took {elapsed:.1f}s — checks not concurrent"


def test_missing_mount_backed_paths_route_through_rc_not_os_path(tmp_path, monkeypatch):
    # Mount safety: a mount-backed bookmark target must be checked via the
    # rclone rc API (rc_stat_for), NEVER a kernel os.path.exists — a raw
    # GETATTR on a hung NFS mount is the exact call that wedges it. Unlike
    # recents (files-only), a directory target is also legitimately "present".
    client, home = _client(tmp_path, monkeypatch)
    live = tmp_path / "live_mount.parquet"
    a_dir = tmp_path / "dir_mount"
    indet = tmp_path / "indet_mount.parquet"
    gone = tmp_path / "gone_mount.parquet"
    tree = [_bm(name, name, i) | {"url": _view_url(p)}
            for i, (name, p) in enumerate(
                [("live", live), ("dir", a_dir), ("indet", indet), ("gone", gone)])]
    _write_tree(home, tree)

    monkeypatch.setattr(mounts_mod, "is_mount_backed", lambda p: True)

    def _no_os_path_exists(path):
        raise AssertionError("os.path.exists called on a mount-backed path")

    monkeypatch.setattr(bookmarks_mod.pathops.os.path, "exists", _no_os_path_exists)

    def _stat(path, **kw):
        if path.endswith("live_mount.parquet"):
            return "exists"
        if path.endswith("dir_mount"):
            return "exists"  # a directory listing is a valid bookmark target
        if path.endswith("gone_mount.parquet"):
            return "missing"  # healthy rcd, item null -> trustworthy negative
        return "indeterminate"  # rcd down / timeout / error

    monkeypatch.setattr(mounts_mod, "rc_stat_for", _stat)

    resp = client.get("/api/bookmarks")
    assert resp.status_code == 200
    # confirmed-missing only; file, dir, and indeterminate all stay unflagged
    # (fail open on indeterminate).
    assert resp.json()["missing"] == ["gone"]
