"""Tests that the recursive /api/fs/walk enumeration is BOUNDED — depth cap,
entry-count cap enforced inside the generator, and abort on client disconnect —
so a search-as-you-type over a big (esp. mount) root can't kick off an
unbounded, uncancelled walk. Companion to test_server_walk.py (behaviour)."""

import asyncio

from fastapi.testclient import TestClient

from fused_render import server
from fused_render.server import _walk_bfs, create_app


def _client(tmp_path):
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app)


def _make_deep_chain(root, depth):
    """root/d0/d1/.../d{depth-1}, each level holding a marker file."""
    cur = root
    for i in range(depth):
        cur = cur / f"d{i}"
        cur.mkdir()
        (cur / f"file{i}.txt").write_text("x", encoding="utf-8")
    return cur


# --- (a) depth cap: the walk stops DESCENDING -------------------------------


def test_walk_stops_at_depth_cap(tmp_path, monkeypatch):
    # A deep, low-fan-out chain (the NAIP state/year/quad/tile shape) never
    # trips the entry-count cap, so only the depth cap bounds it.
    _make_deep_chain(tmp_path, 8)
    monkeypatch.setattr(server, "WALK_MAX_DEPTH_LOCAL", 2)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    assert data["truncated"] is True  # depth cap flags partial coverage
    rels = {e["rel"] for e in data["entries"]}
    # rel slash-count == entry depth; a cap of 2 yields depths 0,1,2 and no more.
    assert rels  # something came back
    assert max(r.count("/") for r in rels) == 2
    assert "d0" in rels and "d0/d1" in rels and "d0/d1/d2" in rels
    assert not any(r.count("/") > 2 for r in rels)  # never descended past the cap


def test_walk_bfs_depth_cap_direct(tmp_path):
    # Same, exercised directly on the generator (bypasses the main-checkout
    # import caveat for the wiring, pins the sentinel contract).
    _make_deep_chain(tmp_path, 6)
    items = list(_walk_bfs(str(tmp_path), include_hidden=False, max_entries=None, max_depth=1))
    # Identify entries vs the truncation sentinel STRUCTURALLY (real entries are
    # dicts; the sentinel is not), never by `is _WALK_TRUNCATED`: an
    # importlib.reload of fused_render.server elsewhere in the suite
    # (test_branch_runtime's autouse teardown) rebinds the module-level
    # _WALK_TRUNCATED to a fresh object(), so the sentinel the reloaded generator
    # yields is no longer the object THIS module imported — an identity filter
    # would then fail to drop it (the CI-only failure this test file had).
    entries = [e for e in items if isinstance(e, dict)]
    rels = {e["rel"] for e in entries}
    assert max(r.count("/") for r in rels) == 1  # depths 0 and 1 only
    assert any(not isinstance(e, dict) for e in items)  # partial-coverage signalled


# --- (b) entry-count cap: the generator stops early, un-exhausted -----------


def test_walk_bfs_entry_cap_stops_generator(tmp_path):
    # 100 top-level files plus a subtree that BFS reaches only after them. With
    # a cap of 5 the generator must yield exactly 5 entries then a truncation
    # sentinel and STOP — never descending into (or listing) the later subtree.
    for i in range(100):
        (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    deep = tmp_path / "zzz_deep"
    deep.mkdir()
    for i in range(100):
        (deep / f"g{i:03d}.txt").write_text("x", encoding="utf-8")

    gen = _walk_bfs(str(tmp_path), include_hidden=False, max_entries=5, max_depth=None)
    collected = list(gen)  # generator terminates on its own — no manual break
    # Real entries are dicts; the truncation sentinel is the sole non-dict.
    # Filter STRUCTURALLY, not by `is _WALK_TRUNCATED`: an importlib.reload of
    # fused_render.server elsewhere in the suite (test_branch_runtime's autouse
    # teardown) rebinds the module-level _WALK_TRUNCATED to a fresh object(), so
    # the sentinel the reloaded generator yields is no longer the object THIS
    # module imported — an identity `is not` filter then leaves it in `entries`,
    # which is exactly why this assertion counted 6 instead of 5 on CI.
    entries = [e for e in collected if isinstance(e, dict)]
    sentinels = [e for e in collected if not isinstance(e, dict)]
    assert len(entries) == 5  # stopped at the cap, did NOT drain 200+ entries
    assert len(sentinels) == 1  # exactly one truncation signal, no double-yield
    assert collected[-1] is sentinels[0]  # and it's the FINAL item emitted
    # Proof it never exhausted the tree: nothing from the later subtree leaked.
    assert not any(e["rel"].startswith("zzz_deep/") for e in entries)


def test_walk_entry_cap_via_endpoint(tmp_path, monkeypatch):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES", 4)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": str(tmp_path)}).json()
    assert data["truncated"] is True
    assert len(data["entries"]) == 4


# --- (c) client disconnect aborts the walk ----------------------------------


class _DisconnectedRequest:
    """Minimal stand-in for a Starlette Request whose client has gone away."""

    async def is_disconnected(self):
        return True


def _walk_endpoint(app):
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/fs/walk")


def test_walk_aborts_on_disconnect(tmp_path):
    # Enough entries that the disconnect poll (every 64) fires before the tree
    # is drained; a disconnected request must break out with a partial result
    # rather than enumerating all 200.
    for i in range(200):
        (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    app = create_app(start_dir=str(tmp_path))
    endpoint = _walk_endpoint(app)
    result = asyncio.run(
        endpoint(request=_DisconnectedRequest(), path=str(tmp_path), hidden="0", stream="0")
    )
    # Stopped at the first disconnect check (seen==64) instead of returning 200.
    assert len(result["entries"]) < 200
