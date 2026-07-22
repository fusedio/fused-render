"""Tests for mount-backed GET /api/fs/list and /api/fs/walk
(fused_render/server.py).

A directory under the mounts dir must be listed via the rclone rcd rc API
(operations/list), never a kernel os.scandir: a READDIR on a flat S3 prefix
with millions of keys forces rclone's VFS to enumerate the whole directory
before the kernel gets its first entry, blows past the macOS NFS deadman, and
kills the mount (the mur-sst incident). A too-huge directory must become a
failed HTTP request, never a dead mount.

Real rclone is never invoked — the StubRcd from test_shell_mounts answers the
rc calls, and FUSED_RENDER_HOME is redirected per test.
"""

import os

import pytest
from fastapi.testclient import TestClient
from test_shell_mounts import StubRcd

import fused_render.server as server
import fused_render.shell.mounts as mounts_mod
from fused_render.server import create_app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "mounts").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    # These tests add a mount BEFORE create_app, so create_app's startup would
    # spawn an automount daemon thread with real work to do. That thread reads
    # FUSED_RENDER_HOME lazily, so if it outlives the test it corrupts the next
    # test's home — and the endpoints under test don't need automount anyway.
    monkeypatch.setattr(mounts_mod, "startup", lambda: None)
    return h


@pytest.fixture()
def rcd(home):
    stub = StubRcd()
    mounts_mod.write_rcd_state(stub.port, 4242)
    yield stub
    stub.close()


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _entry(name, is_dir=False, size=0, mtime="2024-01-02T03:04:05Z"):
    return {"Name": name, "IsDir": is_dir, "Size": -1 if is_dir else size, "ModTime": mtime}


# -- fs/list -----------------------------------------------------------------


def test_list_mount_backed_routes_through_rc_not_kernel(home, rcd, tmp_path, monkeypatch):
    c = mounts_mod.add_mount("s3demo", "remote:bucket/prefix")
    sub = os.path.join(mounts_mod.mountpoint(c), "data")
    rcd.responses["operations/list"] = {
        "list": [
            _entry("zeta.txt", size=3),
            _entry("Alpha", is_dir=True),
            _entry("beta.parquet", size=10),
        ]
    }

    # The mount path must never be touched through the kernel — record every
    # scandir/stat and assert none landed under the mountpoint.
    mp = mounts_mod.mountpoint(c)
    scanned, statted = [], []
    real_scandir, real_stat = os.scandir, os.stat
    monkeypatch.setattr(
        os,
        "scandir",
        lambda p, *a, **k: (scanned.append(os.fspath(p)), real_scandir(p, *a, **k))[1],
    )
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **k: (statted.append(os.fspath(p)), real_stat(p, *a, **k))[1]
    )

    data = _client(tmp_path).get("/api/fs/list", params={"path": sub}).json()

    assert not any(str(p).startswith(mp) for p in scanned)
    assert not any(str(p).startswith(mp) for p in statted)
    # Dirs group first, then case-insensitive by name — same as a local listing.
    assert [e["name"] for e in data["entries"]] == ["Alpha", "beta.parquet", "zeta.txt"]
    by = {e["name"]: e for e in data["entries"]}
    assert by["Alpha"]["is_dir"] is True and by["Alpha"]["size"] is None
    assert by["beta.parquet"]["is_dir"] is False and by["beta.parquet"]["size"] == 10
    assert isinstance(by["beta.parquet"]["mtime"], float)
    assert all(e["ignored"] is False for e in data["entries"])
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/list"]
    assert body["fs"] == "remote:bucket/prefix" and body["remote"] == "data"


def test_list_mount_root_normalizes_rel_to_empty(home, rcd, tmp_path):
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    # Non-empty listing so the empty-listing broken-mount guard (see
    # test_list_mount_empty_*) doesn't fire — this test pins rel normalization.
    rcd.responses["operations/list"] = {"list": [_entry("x.txt", size=1)]}
    resp = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)})
    assert resp.status_code == 200
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/list"]
    assert body["remote"] == ""  # "." normalized to the fs root


def test_list_mounts_root_enumerates_records_not_rc(home, tmp_path, monkeypatch):
    # The mounts CONTAINER (the parent that holds every mountpoint as a subdir)
    # is is_mount_backed too — is_mount_backed's `ap == root` clause keeps it off
    # the kernel — but it sits under no single mount record, so the rc/S3 routes
    # would 503 ("cannot list directory") on it. It must instead be listed by
    # enumerating the mount records directly: 200, each mount as a dir entry,
    # sorted, with zero rc I/O and no sidecar files (mounts.json, per-mount
    # *.json) leaking in from a raw readdir. No rcd needed — the record
    # enumeration precedes any remote call.
    mounts_mod.add_mount("zebra", "remote:z")
    mounts_mod.add_mount("alpha", "remote:a")
    root = mounts_mod.mounts_dir()
    # A stray sidecar file in the container must NOT appear in the listing.
    with open(os.path.join(root, "alpha.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    called = []
    monkeypatch.setattr(mounts_mod, "rc_list_dir", lambda *a, **k: called.append(1) or [])
    resp = _client(tmp_path).get("/api/fs/list", params={"path": root})
    assert resp.status_code == 200
    data = resp.json()
    assert [e["name"] for e in data["entries"]] == ["alpha", "zebra"]  # sorted
    assert all(e["is_dir"] is True and e["size"] is None for e in data["entries"])
    assert data["truncated"] is False and data["cursor"] is None
    assert called == []  # never fell through to the rc listing route


def test_list_mount_file_is_not_a_directory(home, rcd, tmp_path, monkeypatch):
    # The mount is HEALTHY but operations/list errors on a file remote (stub
    # 404s the unset method) -> 400, the mount-safe stand-in for os.path.isdir.
    # (A broken mount takes the 503 branch instead — see the dead-mount test in
    # test_shell_mounts.)
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp, exist_ok=True)
    rcd.responses["mount/listmounts"] = {"mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    resp = _client(tmp_path).get("/api/fs/list", params={"path": mp + "/f.parquet"})
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["error"]


def test_list_mount_rcd_down_returns_503_broken(home, tmp_path):
    # No live rcd -> the mount can't be trusted; surface the broken-mount 503
    # ("reconnect from the Mounts page"), never a kernel fallback.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    resp = _client(tmp_path).get(
        "/api/fs/list", params={"path": mounts_mod.mountpoint(c) + "/data"}
    )
    assert resp.status_code == 503
    assert "reconnect" in resp.json()["error"].lower()


def test_list_mount_timeout_returns_503(home, rcd, tmp_path, monkeypatch):
    # A directory too large to enumerate hits the hard timeout -> 503, not a
    # wedged mount.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    rcd.responses["operations/list"] = {"list": []}
    rcd.delay["operations/list"] = 1.0
    monkeypatch.setattr(mounts_mod, "RC_LIST_TIMEOUT_S", 0.2)
    resp = _client(tmp_path).get(
        "/api/fs/list", params={"path": mounts_mod.mountpoint(c) + "/huge"}
    )
    assert resp.status_code == 503
    assert "timed out" in resp.json()["error"]


# -- fs/walk -----------------------------------------------------------------


def test_walk_mount_backed_lists_each_dir_via_rc(home, rcd, tmp_path):
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    # BFS: the root is listed first, then its "sub" child — a per-call response
    # sequence (last repeats) hands each its own listing.
    rcd.responses["operations/list"] = [
        {"list": [_entry("a.txt", size=1), _entry("sub", is_dir=True)]},
        {"list": [_entry("b.txt", size=2)]},
    ]
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    assert data["truncated"] is False
    rels = {e["rel"] for e in data["entries"]}
    assert rels == {"a.txt", "sub", "sub/b.txt"}  # descended into the subdir
    by = {e["rel"]: e for e in data["entries"]}
    assert by["sub"]["is_dir"] is True and by["sub"]["size"] is None
    assert by["a.txt"]["size"] == 1


def test_walk_mount_skips_failing_subdir_and_continues(home, tmp_path, monkeypatch):
    # A NON-root subdir that times out (or otherwise fails to list) is skipped —
    # its entry is still emitted, but the walk neither descends it nor aborts.
    # The skip marks the walk truncated, though: coverage is partial (the
    # subdir's contents were never listed), so the client must know (1.3/2.2).
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    root = [_entry("a.txt", size=1), _entry("big", is_dir=True), _entry("ok", is_dir=True)]

    def fake_list(path, timeout=None):
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail == "big":
            raise mounts_mod.RcListTimeout("too many entries")
        if tail == "ok":
            return [_entry("c.txt", size=1)]
        return root

    monkeypatch.setattr(mounts_mod, "rc_list_dir", fake_list)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert {"a.txt", "big", "ok", "ok/c.txt"} <= rels  # walk continued past "big"
    assert not any(r.startswith("big/") for r in rels)  # timed-out dir not descended
    assert data["truncated"] is True  # a skipped subdir means partial coverage


def test_walk_mount_clamped_to_remote_cap(home, tmp_path, monkeypatch):
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    entries = [_entry(f"f{i}.txt", size=1) for i in range(10)]
    monkeypatch.setattr(mounts_mod, "rc_list_dir", lambda p, timeout=None: entries)
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES_REMOTE", 3)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    assert data["truncated"] is True
    assert len(data["entries"]) == 3


# -- fs/list truncation contract + S3-direct route ---------------------------
#
# Phase 2: fs/list gains `truncated`/`cursor` across all three routes, and a
# mount on anonymous AWS S3 is listed by paging S3's own ListObjectsV2
# (s3_list_page) rather than the un-paginatable rc listing. Fallback ladder:
# S3-direct -> rc -> 503.

ANON_S3 = {"type": "s3", "provider": "AWS", "env_auth": "false"}


@pytest.fixture()
def fresh_cfg_cache():
    """s3_direct_capable memoizes config/get in a module global; clear it so a
    remote's anon-ness doesn't leak between tests."""
    mounts_mod._upstream_cfg.clear()
    yield
    mounts_mod._upstream_cfg.clear()


def test_list_local_under_cap_shape_unchanged(tmp_path):
    # A plain local listing under the cap: entries as before, plus the two new
    # fields (truncated False, cursor None).
    d = tmp_path / "proj"
    d.mkdir()
    (d / "b.txt").write_text("x", encoding="utf-8")
    (d / "a").mkdir()
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(d)}).json()
    assert data["truncated"] is False
    assert data["cursor"] is None
    assert [e["name"] for e in data["entries"]] == ["a", "b.txt"]
    assert all({"name", "is_dir", "size", "mtime", "ignored"} <= set(e) for e in data["entries"])


def test_list_local_over_cap_truncated(tmp_path, monkeypatch):
    d = tmp_path / "big"
    d.mkdir()
    for i in range(7):
        (d / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "LIST_MAX_ENTRIES", 3)
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(d)}).json()
    assert data["truncated"] is True
    assert data["cursor"] is None
    assert len(data["entries"]) == 3


def test_list_rc_route_over_cap_truncated(home, rcd, tmp_path, monkeypatch):
    # Non-S3 mount whose rc listing exceeds the cap: capped + truncated, cursor
    # None (rclone can't resume).
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    rcd.responses["operations/list"] = {
        "list": [_entry(f"f{i:03d}.txt", size=1) for i in range(10)]
    }
    monkeypatch.setattr(server, "LIST_MAX_ENTRIES", 4)
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    assert data["truncated"] is True
    assert data["cursor"] is None
    assert len(data["entries"]) == 4


def test_list_non_s3_mount_never_hits_s3(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    # config/get unset -> 404 -> not anonymous S3 -> rc route; the S3 pager is
    # never called.
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    rcd.responses["operations/list"] = {"list": [_entry("f.txt", size=1)]}
    called = []
    monkeypatch.setattr(mounts_mod, "s3_list_page", lambda *a, **k: called.append(1) or ([], None))
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    assert [e["name"] for e in data["entries"]] == ["f.txt"]
    assert called == []


def test_list_s3_direct_multipage_and_cursor(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    monkeypatch.setattr(server, "S3_LIST_MAX_ENTRIES", 2000)
    seen = []

    def fake(path, *, max_keys, continuation=None, timeout=None):
        seen.append(continuation)
        n = len(seen)
        if n == 1:
            return ([_entry(f"a{i:04d}.txt", size=1) for i in range(1000)], "TOK1")
        if n == 2:
            return ([_entry(f"b{i:04d}.txt", size=1) for i in range(1000)], "TOK2")
        return ([_entry("z.txt", size=1)], None)

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    # Two whole 1000-key pages hit the 2000 cap; stop with the 2nd page's token.
    assert len(data["entries"]) == 2000
    assert data["truncated"] is True
    assert data["cursor"] == "TOK2"
    assert seen == [None, "TOK1"]  # started with no cursor, threaded TOK1


def test_list_s3_direct_cursor_param_resumes(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    got = {}

    def fake(path, *, max_keys, continuation=None, timeout=None):
        got["continuation"] = continuation
        return ([_entry("x.txt", size=1)], None)

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    data = (
        _client(tmp_path)
        .get("/api/fs/list", params={"path": mounts_mod.mountpoint(c), "cursor": "RESUME"})
        .json()
    )
    assert got["continuation"] == "RESUME"
    assert [e["name"] for e in data["entries"]] == ["x.txt"]
    assert data["truncated"] is False
    assert data["cursor"] is None


def test_list_s3_failure_falls_back_to_rc(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")

    def boom(path, *, max_keys, continuation=None, timeout=None):
        raise mounts_mod.S3ListError("kaboom")

    monkeypatch.setattr(mounts_mod, "s3_list_page", boom)
    rcd.responses["operations/list"] = {"list": [_entry("fromrc.txt", size=5)]}
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    assert [e["name"] for e in data["entries"]] == ["fromrc.txt"]
    assert data["truncated"] is False
    assert data["cursor"] is None
    assert any(x[0] == "operations/list" for x in rcd.calls)


ANON_GCS = {"type": "google cloud storage", "anonymous": "true"}


def test_list_gcs_direct_routes_through_gcs_pager(
    home, rcd, tmp_path, monkeypatch, fresh_cfg_cache
):
    # An anonymous GCS mount takes the direct fast path exactly as anonymous S3
    # does — the unified dispatcher routes it to the GCS pager, not rc.
    rcd.responses["config/get"] = ANON_GCS
    c = mounts_mod.add_mount("gopen", "gcs-open:mur-sst/zarr-v1")
    got = {}

    def fake(path, *, max_keys, continuation=None, timeout=None):
        got["continuation"] = continuation
        return ([_entry("x.txt", size=1)], None)

    monkeypatch.setattr(mounts_mod, "gcs_list_page", fake)
    monkeypatch.setattr(
        mounts_mod, "rc_list_dir", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rc used"))
    )
    data = (
        _client(tmp_path)
        .get("/api/fs/list", params={"path": mounts_mod.mountpoint(c), "cursor": "RESUME"})
        .json()
    )
    assert got["continuation"] == "RESUME"
    assert [e["name"] for e in data["entries"]] == ["x.txt"]
    assert data["truncated"] is False
    assert data["cursor"] is None


# -- fs/walk on S3-capable mounts --------------------------------------------


def test_walk_s3_capable_uses_pages(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    mp = mounts_mod.mountpoint(c)

    calls = []

    def fake(path, *, max_keys, continuation=None, timeout=None):
        calls.append(path)
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail == "sub":
            return ([_entry("b.txt", size=2)], None)
        return ([_entry("a.txt", size=1), _entry("sub", is_dir=True)], None)

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    # rc must NOT be used when the S3 pager succeeds.
    monkeypatch.setattr(
        mounts_mod,
        "rc_list_dir",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rc_list_dir used")),
    )
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert rels == {"a.txt", "sub", "sub/b.txt"}
    assert data["truncated"] is False
    assert calls  # the S3 pager did the listing


def test_list_s3_cursored_failure_returns_retryable_503(
    home, rcd, tmp_path, monkeypatch, fresh_cfg_cache
):
    # 1.1: a page failure on a CURSORED request must return a retryable 503, NOT
    # fall through to rc — rc can't resume (it re-serves page 1 with cursor=None,
    # which the frontend dedupes to zero rows, or 503s on a huge dir).
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")

    monkeypatch.setattr(
        mounts_mod,
        "s3_list_page",
        lambda *a, **k: (_ for _ in ()).throw(mounts_mod.S3ListError("x")),
    )
    monkeypatch.setattr(
        mounts_mod, "rc_list_dir", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rc used"))
    )
    resp = _client(tmp_path).get(
        "/api/fs/list", params={"path": mounts_mod.mountpoint(c), "cursor": "RESUME"}
    )
    assert resp.status_code == 503
    assert "retry" in resp.json()["error"].lower()


def test_list_s3_first_page_failure_still_falls_back_to_rc(
    home, rcd, tmp_path, monkeypatch, fresh_cfg_cache
):
    # 1.1 corollary: a CURSOR-LESS first request keeps the S3 -> rc -> 503 ladder.
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    monkeypatch.setattr(
        mounts_mod,
        "s3_list_page",
        lambda *a, **k: (_ for _ in ()).throw(mounts_mod.S3ListError("x")),
    )
    rcd.responses["operations/list"] = {"list": [_entry("fromrc.txt", size=1)]}
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    assert [e["name"] for e in data["entries"]] == ["fromrc.txt"]


def test_list_s3_cap_never_overshoots(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    # 1.4: each page requests only min(1000, remaining) keys, so the response
    # never exceeds S3_LIST_MAX_ENTRIES (a whole extra 1000-key page could push
    # a 1500 cap to 2000). cap=1500 -> page1 asks 1000, page2 asks 500.
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    monkeypatch.setattr(server, "S3_LIST_MAX_ENTRIES", 1500)
    asked = []

    def fake(path, *, max_keys, continuation=None, timeout=None):
        asked.append(max_keys)
        n = len(asked)
        return (
            [_entry(f"f{n}_{i}.txt", size=1) for i in range(max_keys)],
            "TOK" if n < 2 else "MORE",
        )

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    assert len(data["entries"]) == 1500
    assert asked == [1000, 500]
    assert data["truncated"] is True
    assert data["cursor"] == "MORE"


def test_list_s3_overall_budget_returns_resumable_page(
    home, rcd, tmp_path, monkeypatch, fresh_cfg_cache
):
    # 1.2: page COUNT is bounded by an overall time budget; on exhaustion the
    # accumulator returns what it fetched plus the last token — a valid resumable
    # page (truncated True + cursor), NOT an error.
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    monkeypatch.setattr(server, "S3_LIST_MAX_ENTRIES", 100_000)  # cap won't bite
    monkeypatch.setattr(server, "S3_LIST_OVERALL_TIMEOUT_S", 0.0)  # budget bites after page 1
    calls = []

    def fake(path, *, max_keys, continuation=None, timeout=None):
        calls.append(continuation)
        return ([_entry(f"a{i}.txt", size=1) for i in range(10)], "NEXT")

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    data = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)}).json()
    assert len(data["entries"]) == 10  # only the first page before the budget
    assert data["truncated"] is True
    assert data["cursor"] == "NEXT"
    assert len(calls) == 1  # stopped after one page


def test_list_s3_midlisting_failure_returns_partial(
    home, rcd, tmp_path, monkeypatch, fresh_cfg_cache
):
    # A page failure AFTER at least one page returns the accumulated entries
    # with the failed page's token as the resume cursor — not a 503, and not
    # an rc-restart that would discard everything fetched (the last page
    # routinely times out when the per-page timeout shrinks to the budget).
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    calls = []

    def fake(path, *, max_keys, continuation=None, timeout=None):
        calls.append(continuation)
        if len(calls) == 2:
            raise mounts_mod.S3ListError("read timed out")
        return ([_entry(f"p{len(calls)}_{i}.txt", size=1) for i in range(10)], "TOK1")

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    resp = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 10
    assert data["truncated"] is True
    assert data["cursor"] == "TOK1"  # resume from the FAILED page
    assert calls == [None, "TOK1"]  # no rc fallback, no third try


def test_list_s3_budget_never_passes_nonpositive_timeout(
    home, rcd, tmp_path, monkeypatch, fresh_cfg_cache
):
    # Bugbot: post-page budget check can pass with ~nothing left; the next
    # page must not reach urlopen with a timeout <= 0 (ValueError, not
    # S3ListError -> unhandled 500). First page always runs (progress
    # guarantee); later pages stop cleanly when the remainder hits zero.
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    seen_timeouts = []

    def fake(path, *, max_keys, continuation=None, timeout=None):
        seen_timeouts.append(timeout)
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")  # what urlopen does
        return ([_entry(f"g{len(seen_timeouts)}_{i}.txt", size=1) for i in range(10)], "NEXT")

    monkeypatch.setattr(mounts_mod, "s3_list_page", fake)
    monkeypatch.setattr(server, "S3_LIST_OVERALL_TIMEOUT_S", 1e-9)
    resp = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c)})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 10  # exactly the guaranteed page
    assert data["truncated"] is True and data["cursor"] == "NEXT"
    assert seen_timeouts[0] is None  # first page: full page timeout


def test_list_empty_rc_listing_on_broken_mount_returns_503(home, rcd, tmp_path):
    # 2.3: rcd alive but the kernel mount gone -> rc lists empty; an empty
    # listing under a broken mount must 503, not render as an empty folder.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")  # never attached -> broken
    rcd.responses["operations/list"] = {"list": []}
    resp = _client(tmp_path).get("/api/fs/list", params={"path": mounts_mod.mountpoint(c) + "/sub"})
    assert resp.status_code == 503
    assert "reconnect" in resp.json()["error"].lower()


def test_list_empty_rc_listing_on_healthy_mount_returns_200(home, rcd, tmp_path, monkeypatch):
    # 2.3: a HEALTHY mount with a genuinely empty directory still returns 200.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp, exist_ok=True)
    rcd.responses["operations/list"] = {"list": []}
    rcd.responses["mount/listmounts"] = {"mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    resp = _client(tmp_path).get("/api/fs/list", params={"path": mp})
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_walk_root_listing_timeout_surfaces_503(home, tmp_path, monkeypatch):
    # 2.2: fs/walk used to return 200-empty when the ROOT listing failed; now it
    # surfaces the same status codes fs/list does.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    monkeypatch.setattr(
        mounts_mod,
        "rc_list_dir",
        lambda p, timeout=None: (_ for _ in ()).throw(mounts_mod.RcListTimeout("too many")),
    )
    resp = _client(tmp_path).get("/api/fs/walk", params={"path": mounts_mod.mountpoint(c)})
    assert resp.status_code == 503
    assert "timed out" in resp.json()["error"]


def test_walk_root_rcd_down_surfaces_503(home, tmp_path):
    # 2.2: no live rcd -> RcListUnavailable at the root -> broken-mount 503.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    resp = _client(tmp_path).get("/api/fs/walk", params={"path": mounts_mod.mountpoint(c)})
    assert resp.status_code == 503


def test_walk_dir_cut_marks_truncated_even_when_few_entries_yielded(
    home, rcd, tmp_path, monkeypatch
):
    # 1.3: the per-dir cap cuts the listing, but dotfile filtering yields FEWER
    # entries than the cap — truncated must still be True (the yielded count
    # alone would report False while thousands of keys went unlisted).
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES_REMOTE", 3)
    # 5 entries > cap 3 -> cut; the first 3 include two dotfiles -> 1 yielded.
    listed = [
        _entry(".h1"),
        _entry(".h2"),
        _entry("v.txt", size=1),
        _entry("x.txt", size=1),
        _entry("y.txt", size=1),
    ]
    monkeypatch.setattr(mounts_mod, "rc_list_dir", lambda p, timeout=None: listed)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert rels == {"v.txt"}  # only the non-dotfile among the first 3
    assert data["truncated"] is True  # via the dir-cut sentinel, not the count


def test_walk_s3_failure_falls_back_to_rc(home, rcd, tmp_path, monkeypatch, fresh_cfg_cache):
    rcd.responses["config/get"] = ANON_S3
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    mp = mounts_mod.mountpoint(c)

    def s3_boom(path, *, max_keys, continuation=None, timeout=None):
        raise mounts_mod.S3ListError("kaboom")

    monkeypatch.setattr(mounts_mod, "s3_list_page", s3_boom)
    monkeypatch.setattr(
        mounts_mod, "rc_list_dir", lambda p, timeout=None: [_entry("fromrc.txt", size=1)]
    )
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert rels == {"fromrc.txt"}
    assert data["truncated"] is False
