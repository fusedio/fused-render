"""The /api/index/* routes and the startup scan scheduler.

See fused_render/index/specs/server-api.md.
"""
import asyncio
import json
import logging
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.index import runner
from fused_render.index.cancel import Cancelled
from fused_render.index.config import IndexConfig, load_config
from fused_render.server import create_app
from fused_render.server.routers import index as index_router
from fused_render.server.routers.index import (
    note_folder_opened as _real_note_folder_opened,
)


class _FakePopen:
    """Stands in for a detached worker: the scan never actually runs."""

    pid = 4242


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway shell home, so the index store lands under it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def _tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.txt").write_text("a", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "beta.md").write_text("b", encoding="utf-8")
    return src


def _point_home_at(monkeypatch, path):
    """Make `os.path.expanduser("~")` — and any `~/...`-prefixed path —
    answer under `path`, on every platform.

    `monkeypatch.setenv("HOME", ...)` alone only works on POSIX: Windows'
    `ntpath.expanduser` reads `USERPROFILE` (falling back to
    `HOMEDRIVE`+`HOMEPATH`) and never consults `HOME` at all, so a test that
    only sets `HOME` silently keeps pointing `warm_root()`/`config.home` at
    the real machine's profile instead of the tree it built.

    D882 (index-search-wedge FIX round, CI finding 2): the previous version
    of this helper only special-cased the literal string `"~"` and fell
    through to the REAL `os.path.expanduser` for anything else, including a
    compound path like `"~/.fused-render"` — exactly what
    `ignore.default_home_dirs()` passes it directly (not
    `os.path.join(expanduser("~"), ...)`). On POSIX that fallthrough happened
    to work anyway, because `posixpath.expanduser` re-reads `os.environ["HOME"]`
    at call time regardless of which function object is bound to
    `os.path.expanduser` — but on Windows `ntpath.expanduser` reads
    `USERPROFILE`/`HOMEDRIVE`+`HOMEPATH`, which this helper never set, so the
    real function silently resolved against the CI runner's actual profile
    instead of the test's `path`. That made `MountGuard`'s guarded-roots list
    (built via `default_home_dirs()`) not include the directory a test
    expected it to guard, purely as an artifact of this test shim — not of
    `MountGuard`/`_walk_from` logic, which never runs on Windows-only code
    (see `test_rank_reason_is_mount_for_a_typed_path_the_guarded_walk_stopped_short_of`'s
    failure on the Windows CI lane). Fixed by handling every `~`-prefixed
    path directly, with a plain string join, instead of delegating compound
    forms to the real (platform-varying) implementation; `USERPROFILE` is
    also set so any OTHER code path that calls the real `expanduser` without
    going through this monkeypatch (there is none today, but nothing
    guarantees that forever) still lands on `path` on Windows too."""
    real_expanduser = os.path.expanduser
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))

    def _expanduser(p):
        if p == "~":
            return str(path)
        if p.startswith("~/") or p.startswith("~\\"):
            return os.path.join(str(path), p[2:])
        return real_expanduser(p)

    monkeypatch.setattr(os.path, "expanduser", _expanduser)


# -- guards --------------------------------------------------------------------

@pytest.mark.parametrize("path,body", [
    ("/api/index/scan", {"root": "."}),
    ("/api/index/cancel", {"run_id": "x"}),
    ("/api/index/config", {"roots": []}),
])
def test_mutating_routes_require_the_fused_header(home, tmp_path, path, body):
    resp = _client(tmp_path).post(path, json=body)
    assert resp.status_code == 403
    assert "X-Fused" in resp.json()["error"]


def test_scan_rejects_a_path_that_is_not_a_directory(home, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    resp = _client(tmp_path).post("/api/index/scan", json={"root": str(f)},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["error"]


def test_scan_409s_while_indexing_is_off(home, tmp_path, monkeypatch):
    monkeypatch.setattr(index_router.index_gate.prefs, "indexing_enabled", lambda: False)
    resp = _client(tmp_path).post("/api/index/scan", json={"root": str(tmp_path)},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 409
    assert "disabled" in resp.json()["error"]


def test_cancel_of_an_unknown_run_is_a_400(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/cancel", json={"run_id": "nope"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


# -- the scan lifecycle, for real ---------------------------------------------

def test_scan_status_and_stats_over_a_real_tree(home, tmp_path):
    """One end-to-end pass: POST a scan, poll status until the detached worker
    finishes, then read the index back through stats."""
    src = _tree(tmp_path)
    client = _client(tmp_path)
    started = client.post("/api/index/scan", json={"root": str(src)},
                          headers={"X-Fused": "1"})
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    deadline = time.time() + 120
    state = None
    while time.time() < deadline:
        state = client.get("/api/index/status",
                           params={"run_id": run_id}).json()
        if not state["running"]:
            break
        time.sleep(0.2)
    assert state is not None and state["running"] is False, state
    assert state["error"] is None, state["error"]
    # the real `runner.start` records `canonical_root(root)`, not the
    # caller's raw spelling (platform.md §1) — a no-op of that on POSIX.
    assert state["root"] == runner.canonical_root(str(src))

    stats = client.get("/api/index/stats", params={"root": str(src)}).json()
    assert stats["rows"] == 2
    assert stats["empty"] is False


def test_status_without_a_run_id_reports_the_latest_run(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "20260101-000000-aa")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r"}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        f.write(json.dumps({"type": "progress", "dirs": 2, "files": 7,
                            "current": "/r/x"}) + "\n")
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["running"] is True
    assert body["files"] == 7
    assert body["root"] == "/r"
    assert body["run_id"] == "20260101-000000-aa"


def _write_run(cfg, run_id, root, events):
    d = os.path.join(cfg.runs_dir, run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": root}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return d


def test_status_without_a_run_id_reports_a_RUNNING_run(home, tmp_path):
    """With several roots the newest run is not the interesting one: a small
    root can finish while the big one still walks, and the newest-first pick
    then froze the panel on the finished run's counts for minutes while
    `scanning` stayed true. The run reported must be one that is running."""
    cfg = load_config()
    _write_run(cfg, "20260101-000000-aa", "/big",
               [{"type": "progress", "dirs": 2, "files": 7, "current": "/big/x"}])
    _write_run(cfg, "20260101-000100-bb", "/small",
               [{"type": "progress", "dirs": 1, "files": 1},
                {"type": "run_end", "summary": {"rows": 1}}])
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is True
    assert body["running"] is True
    assert body["run_id"] == "20260101-000000-aa"
    assert body["root"] == "/big"
    assert body["files"] == 7


def test_status_without_a_run_id_falls_back_to_the_latest_when_none_run(home, tmp_path):
    cfg = load_config()
    _write_run(cfg, "20260101-000000-aa", "/old",
               [{"type": "run_end", "summary": {}}])
    _write_run(cfg, "20260101-000100-bb", "/new",
               [{"type": "progress", "files": 4},
                {"type": "run_end", "summary": {}}])
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is False
    assert body["run_id"] == "20260101-000100-bb"
    assert body["root"] == "/new"


def test_status_with_no_runs_at_all_is_a_quiet_idle(home, tmp_path):
    body = _client(tmp_path).get("/api/index/status").json()
    assert body == {"ok": True, "running": False, "run_id": None, "root": None,
                    "phase": "", "dirs": 0, "files": 0, "reused": 0,
                    "current": "", "summary": None, "cancelled": False,
                    "error": None, "indexed": False, "updated": None,
                    "has_index": False, "scanning": False,
                    "files_indexed": 0, "last_completed_at": None}


def test_status_of_an_unknown_run_id_is_a_400(home, tmp_path):
    resp = _client(tmp_path).get("/api/index/status", params={"run_id": "nope"})
    assert resp.status_code == 400


def test_cancel_writes_the_flag(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "r1")
    os.makedirs(d)
    open(os.path.join(d, "spec.json"), "w").close()
    resp = _client(tmp_path).post("/api/index/cancel", json={"run_id": "r1"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert os.path.exists(os.path.join(d, "cancel"))


# -- stats on an empty index ---------------------------------------------------

def test_stats_on_a_never_built_index(home, tmp_path):
    body = _client(tmp_path).get("/api/index/stats").json()
    assert body["empty"] is True
    assert body["rows"] == 0


# -- the read-concurrency semaphores ---------------------------------------------
#
# `_interactive_read_concurrency()` and `_query_read_concurrency()`
# (routers/index.py) bound how many of the five expensive read routes may be
# running their duckdb call at once — without it, `asyncio.to_thread`'s
# default executor (min(32, cpu+4) workers) would let up to 32
# `search_threads()`-capped pools run simultaneously, most of the
# whole-machine exposure `search_threads` exists to remove.
#
# Two lanes (D701 correction / D706), not one shared semaphore: `stats`,
# `search`, and `rank` are per-keystroke and share the tight, width-2
# interactive lane; `query` and `ask` run a caller-authored statement bounded
# only by `guarded_query.TIMEOUT_S` (10s) and share their own narrower,
# width-1 lane, so a slow SQL-panel query can no longer block the home search
# box behind it.

def test_read_routes_bound_how_many_run_their_duckdb_call_at_once(home, tmp_path,
                                                                   monkeypatch):
    """Six concurrent `/api/index/stats` requests, each blocked on a real
    `threading.Event` inside the (monkeypatched) duckdb call: peak concurrency
    must never exceed the semaphore's width, and every request must still
    eventually complete rather than deadlock behind it.

    Driven through `httpx.AsyncClient` + `ASGITransport` on ONE event loop
    (`asyncio.run`, `asyncio.gather`) rather than a thread pool of sync
    `TestClient` calls: `asyncio.Semaphore` binds to whichever loop first
    awaits it, and a thread pool of separately-looped sync clients trips that
    the moment two of them touch the same module-level semaphore."""
    import threading

    import httpx

    lock = threading.Lock()
    state = {"concurrent": 0, "peak": 0}
    release = threading.Event()

    def fake_stats(cfg, root="", breakdown=False, token=None):
        with lock:
            state["concurrent"] += 1
            state["peak"] = max(state["peak"], state["concurrent"])
        release.wait(timeout=5)
        with lock:
            state["concurrent"] -= 1
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}

    monkeypatch.setattr(index_router, "index_stats", fake_stats)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            tasks = [asyncio.create_task(client.get("/api/index/stats"))
                     for _ in range(6)]
            # Give the pile-up a moment to reach its ceiling before releasing —
            # long enough on any CI box, short enough not to matter if it isn't.
            for _ in range(50):
                with lock:
                    if state["concurrent"] >= 2:
                        break
                await asyncio.sleep(0.02)
            assert state["peak"] <= 2
            release.set()
            return await asyncio.gather(*tasks)

    responses = asyncio.run(run())
    assert all(r.status_code == 200 for r in responses)
    assert state["peak"] == 2  # the ceiling was actually reached, not just respected


def test_a_slow_query_does_not_block_the_interactive_lane(home, tmp_path, monkeypatch):
    """The regression the two-lane split fixes: with one shared semaphore, a
    slow `/api/index/query` could hold both of its slots for up to 10s, and
    every keystroke in the home search box queued behind it. `/api/index/stats`
    (the interactive lane) must answer immediately while a query is still
    running."""
    import threading

    import httpx

    query_started = threading.Event()
    query_release = threading.Event()

    def fake_guarded(cfg, sql, limit, token=None):
        query_started.set()
        query_release.wait(timeout=5)
        return {"columns": [], "rows": []}

    def fake_stats(cfg, root="", breakdown=False, token=None):
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}

    monkeypatch.setattr(index_router, "_guarded", fake_guarded)
    monkeypatch.setattr(index_router, "index_stats", fake_stats)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            query_task = asyncio.create_task(
                client.post("/api/index/query", json={"sql": "select 1"},
                           headers={"X-Fused": "1"}))
            for _ in range(50):
                if query_started.is_set():
                    break
                await asyncio.sleep(0.02)
            assert query_started.is_set()
            t0 = time.monotonic()
            stats_resp = await client.get("/api/index/stats")
            elapsed = time.monotonic() - t0
            query_release.set()
            query_resp = await query_task
            return stats_resp, elapsed, query_resp

    stats_resp, elapsed, query_resp = asyncio.run(run())
    assert stats_resp.status_code == 200
    # `fake_stats` is a pure Python stub with no real I/O, so a serialised
    # lane would show up as (near-)instant, not merely "under 2s" — tightened
    # from 2.0 (SPEC-index-search-wedge.md's "Also:" note: that bound was
    # part of the blind spot that let a serialised interactive lane ship
    # unnoticed).
    assert elapsed < 0.5, elapsed
    assert query_resp.status_code == 200


def test_a_breakdown_request_does_not_consume_an_interactive_slot(home, tmp_path, monkeypatch):
    """Code review finding: `stats`/`search`/`rank` share the interactive
    lane on the premise that every member is "bounded, cheap, and
    known-shaped" -- true of a plain `/api/index/stats`, but not of
    `?breakdown=true`, an unbounded `GROUP BY ext` over every partition under
    the root that can take seconds on a large index. Two PLAIN stats calls
    filling the whole width-2 interactive lane must not delay a breakdown
    request -- if breakdown still shared that lane, it would queue behind
    them for as long as they run, the exact head-of-line blocking the
    two-lane split (D706) was introduced to eliminate, reintroduced inside
    the lane meant to be safe from it."""
    import threading

    import httpx

    lock = threading.Lock()
    plain_concurrent = 0
    plain_started = threading.Event()
    plain_release = threading.Event()

    def fake_stats(cfg, root="", breakdown=False, token=None):
        nonlocal plain_concurrent
        if not breakdown:
            with lock:
                plain_concurrent += 1
                if plain_concurrent >= 2:
                    plain_started.set()
            plain_release.wait(timeout=5)
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}

    monkeypatch.setattr(index_router, "index_stats", fake_stats)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            plain_tasks = [
                asyncio.create_task(client.get("/api/index/stats"))
                for _ in range(2)
            ]
            for _ in range(50):
                if plain_started.is_set():
                    break
                await asyncio.sleep(0.02)
            assert plain_started.is_set()  # both interactive-lane slots taken
            t0 = time.monotonic()
            breakdown_resp = await client.get(
                "/api/index/stats", params={"breakdown": "true"})
            elapsed = time.monotonic() - t0
            plain_release.set()
            plain_resps = await asyncio.gather(*plain_tasks)
            return breakdown_resp, elapsed, plain_resps

    breakdown_resp, elapsed, plain_resps = asyncio.run(run())
    assert breakdown_resp.status_code == 200
    # Same tightening as above and for the same reason: `fake_stats` is a
    # pure stub, so this bound should catch a serialised lane, not merely a
    # multi-second one.
    assert elapsed < 0.5, elapsed
    assert all(r.status_code == 200 for r in plain_resps)


# -- SPEC-index-search-wedge.md: a parked read must not permanently halve or
# zero the interactive lane -------------------------------------------------

def test_a_wedged_rank_request_does_not_permanently_hold_its_lane_slot(
        home, tmp_path, monkeypatch):
    """The regression this whole spec exists to fix: `/api/index/rank` used
    to hold one of the two interactive-lane permits across an
    `asyncio.to_thread` call that could never be killed, so a worker thread
    parked on an uninterruptible syscall (a wedged NFS/rclone mount, in
    practice) wedged the lane for the life of the process. Stub the rank
    worker so the FIRST call blocks forever on a `threading.Event` that is
    never set (exactly that: an un-killable, permanently parked thread);
    every later call answers immediately. `ABANDON_S` (item 2) is
    monkeypatched small so the test does not have to wait out the real
    5-second default to see the permit actually get released."""
    import threading

    import httpx

    monkeypatch.setattr(index_router, "ABANDON_S", 0.05)
    lock = threading.Lock()
    calls = {"n": 0}
    first_entered = threading.Event()
    never = threading.Event()

    def fake_rank_worker(cfg, root, q, limit, token, ranked):
        with lock:
            calls["n"] += 1
            is_first = calls["n"] == 1
        if is_first:
            first_entered.set()
            never.wait()  # the un-killable, permanently-parked worker thread
        return {"covered": True, "reason": "", "scanned_partitions": 0,
                "of_partitions": 0, "base": root, "mode": "substring",
                "hits": [], "truncated": False, "total": 0}

    monkeypatch.setattr(index_router, "_rank_worker", fake_rank_worker)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            first_task = asyncio.create_task(
                client.get("/api/index/rank",
                          params={"root": str(tmp_path), "q": "x"}))
            for _ in range(50):
                if first_entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert first_entered.is_set()

            # A later request must not queue behind the wedged one — today
            # (pre-fix) it would block for the life of the process.
            t0 = time.monotonic()
            second_resp = await client.get(
                "/api/index/rank", params={"root": str(tmp_path), "q": "y"})
            second_elapsed = time.monotonic() - t0

            # Both lane permits available afterwards: two further concurrent
            # requests must both proceed, not serialise behind a lane the
            # wedged permit never gave back.
            t0 = time.monotonic()
            more = await asyncio.gather(*[
                client.get("/api/index/rank",
                          params={"root": str(tmp_path), "q": "z"})
                for _ in range(2)])
            more_elapsed = time.monotonic() - t0

            first_resp = await first_task

            # Review finding F: `never` is left unset by the test body on
            # purpose (the whole point is a REAL, un-killable OS thread
            # parked in `_INDEX_READ_POOL` forever) — but "forever" left
            # unset for the rest of the pytest WORKER PROCESS degrades every
            # later test that touches the real pool or `_abandoned_reads`:
            # one fewer real pool thread, and a permanently non-empty
            # `_abandoned_reads` (exactly what
            # `test_index_read_pool_exhaustion_is_a_fast_503` works around by
            # monkeypatching its OWN isolated set rather than asserting the
            # real one starts empty). Draining must happen HERE, inside
            # `run()`, while this event loop is still running: the abandoned
            # future's done-callback (`_reap_abandoned`) is chained via
            # `call_soon` on this specific loop, which never fires once
            # `asyncio.run` has torn it down — waiting after `asyncio.run`
            # returns would spin until the timeout with the future stuck at
            # "pending" forever, exactly what happened before this loop was
            # moved in here.
            never.set()
            deadline = time.monotonic() + 2.0
            while index_router._abandoned_reads and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            return second_resp, second_elapsed, more, more_elapsed, first_resp

    (second_resp, second_elapsed, more, more_elapsed,
     first_resp) = asyncio.run(run())
    assert second_resp.status_code == 200, second_resp.text
    assert second_elapsed < 0.3, second_elapsed
    assert all(r.status_code == 200 for r in more)
    assert more_elapsed < 0.3, more_elapsed
    # The wedged first request itself eventually gets the abandon-timeout
    # 503, once ABANDON_S elapses — it just never blocks anything ELSE.
    assert first_resp.status_code == 503
    assert first_resp.json() == {"error": "index read timed out"}
    assert not index_router._abandoned_reads, (
        "the wedged worker's thread never drained; a later test in this "
        "process would inherit a poisoned _abandoned_reads")


def test_a_burst_of_overlapping_rank_requests_keeps_bounded_latency(
        home, tmp_path, monkeypatch):
    """The path a fast typist actually takes, and which nothing above tests
    under real concurrency: a stream of overlapping `/api/index/rank`
    requests, roughly half abandoned by the client shortly after being
    fired (exactly what a fast typist's per-keystroke search does — each
    request superseded by the next before it finishes), plus a periodic
    truly-wedged worker (one un-killable thread every 4th call, reachable
    only via `ABANDON_S`). None of that may leave the interactive lane
    (width 2) short a permit, or `_abandoned_reads` non-empty, once the
    dust settles — and a final, ordinary request fired afterwards must come
    back fast, not queued behind any of it.

    Cancelling a client is driven by monkeypatching
    `starlette.requests.Request.is_disconnected` (keyed on the request's own
    `q` value) rather than `task.cancel()` on the httpx call: a throwaway
    experiment (this session, not checked in) proved that cancelling the
    asyncio task wrapping an in-process `ASGITransport` call propagates
    `asyncio.CancelledError` straight through the whole call chain instead
    of ever making `request.is_disconnected()` observe anything — so
    `task.cancel()` cannot exercise the real `cancellable()`/
    `_watch_disconnect()` code path this route depends on in production.
    Monkeypatching `is_disconnected` does exercise that real path."""
    import threading

    import httpx
    import starlette.requests as starlette_requests

    monkeypatch.setattr(index_router, "ABANDON_S", 0.15)

    lock = threading.Lock()
    never = threading.Event()
    # Finding 4: record every `q` for which the worker THREAD itself observed
    # `token.cancelled` and raised `Cancelled` — not merely "got a 499",
    # which a request can also get pre-submission (the route's own
    # `if token.cancelled: return Response(status_code=499)`, before
    # `_rank_worker` is ever called). Asserting on this set instead of on
    # `499 in statuses` is what actually proves cancellation reached the
    # worker thread. `cancel_proof_entered` is the synchronisation for the
    # dedicated, non-racy proof request below (`q == "cancel-proof"`).
    worker_cancelled: set = set()
    cancel_proof_entered = threading.Event()

    def fake_rank_worker(cfg, root, q, limit, token, ranked):
        empty = {"covered": True, "reason": "", "scanned_partitions": 0,
                 "of_partitions": 0, "base": root, "mode": "substring",
                 "hits": [], "truncated": False, "total": 0}

        if q == "cancel-proof":
            # Finding 4's fix: proving `Cancelled` propagates out of a
            # worker thread ALREADY IN FLIGHT (not merely a pre-submission
            # queue check) needs to not depend on winning a wall-clock race
            # against 19 other overlapping requests and the lane's own
            # contention — that dependency is exactly what made the old
            # `499 in statuses` assertion unable to prove what its comment
            # claimed (see the docstring for `test`, review finding 4). This
            # branch is reached by ONE dedicated, otherwise-ordinary
            # request, fired only after the racy burst below has fully
            # settled: `cancel_proof_entered` tells `run()` this thread is
            # now inside the worker (so the client-cancel signal that
            # follows can only be observed here, from inside, never
            # pre-submission), and the generous poll window (up to 0.8s,
            # against the 1.0s `ABANDON_S` `run()` raises just for this
            # request) leaves comfortable headroom over `DISCONNECT_POLL_S`
            # (0.1s) even on a loaded machine.
            cancel_proof_entered.set()
            for _ in range(80):
                if token is not None and token.cancelled:
                    with lock:
                        worker_cancelled.add(q)
                    raise Cancelled()
                time.sleep(0.01)
            return empty

        # Finding 2: which calls are "truly wedged" is keyed on the `q`
        # value itself (`q0`, `q4`, `q8`, ... every 4th burst request), not
        # on a shared call counter incremented in whatever order requests
        # happen to reach this function. A counter's order depends on how
        # many client-cancelled requests got cancelled before vs. after the
        # route's pre-submission check — which moves with machine load — so
        # it could silently shift which call (including the unrelated final
        # "clean" request below, which never reaches this function under
        # its own `q`) landed on the wedge branch. Keying on `q` makes the
        # wedge assignment fixed regardless of arrival order or load.
        n = int(q[1:]) if q.startswith("q") and q[1:].isdigit() else -1
        if n >= 0 and n % 4 == 0:
            never.wait()  # the un-killable, permanently-parked worker thread
            return empty
        # Finding 3: this poll loop must stay MATERIALLY shorter than
        # `ABANDON_S` (0.15s above) — 5 * 0.01s = 0.05s, not the old
        # 15 * 0.01s = 0.15s, which tied it and made every non-cancelled
        # call lose the abandon race, so no burst request could ever answer
        # 200. It still gives the disconnect watcher (polling every
        # `DISCONNECT_POLL_S`) a real chance to have already cancelled this
        # token by the time the worker would otherwise finish, but (per
        # finding 4 above) landing inside this window is now a bonus, not
        # the thing being asserted on.
        for _ in range(5):
            if token is not None and token.cancelled:
                with lock:
                    worker_cancelled.add(q)
                raise Cancelled()
            time.sleep(0.01)
        return empty

    monkeypatch.setattr(index_router, "_rank_worker", fake_rank_worker)

    to_cancel: set = set()

    async def fake_is_disconnected(self):
        return self.query_params.get("q") in to_cancel

    monkeypatch.setattr(starlette_requests.Request, "is_disconnected",
                        fake_is_disconnected)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            tasks = []
            for i in range(20):
                qval = f"q{i}"
                if i % 2 == 0:
                    to_cancel.add(qval)
                tasks.append(asyncio.create_task(
                    client.get("/api/index/rank",
                              params={"root": str(tmp_path), "q": qval})))
                # Staggered starts, not one big gather: this is what a burst
                # of real keystrokes looks like, and it is what lets the
                # is_disconnected patch (checked every DISCONNECT_POLL_S)
                # actually catch some of these mid-flight.
                await asyncio.sleep(0.007)
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            # Let every wedged (n % 4 == 0) worker thread finally return, and
            # drain `_abandoned_reads` HERE, inside `run()` — see the wedge
            # test above for why this must happen before `asyncio.run`
            # returns and tears the loop down.
            never.set()
            deadline = time.monotonic() + 2.0
            while index_router._abandoned_reads and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

            # Finding 4's dedicated, deterministic proof: fire ONE more
            # request, wait (via a real thread Event, not a wall-clock
            # guess) until its worker thread has actually entered
            # `_rank_worker`, and only THEN flip `is_disconnected` for it.
            # The watcher can only observe that after this point, so any
            # `Cancelled` it raises can only have come from inside the
            # worker — never the pre-submission `if token.cancelled` check,
            # which already ran (and passed) before this request's worker
            # thread could possibly have started.
            monkeypatch.setattr(index_router, "ABANDON_S", 1.0)
            cancel_proof_task = asyncio.create_task(client.get(
                "/api/index/rank",
                params={"root": str(tmp_path), "q": "cancel-proof"}))
            for _ in range(200):
                if cancel_proof_entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert cancel_proof_entered.is_set()
            to_cancel.add("cancel-proof")
            cancel_proof_resp = await cancel_proof_task

            t0 = time.monotonic()
            final_resp = await client.get(
                "/api/index/rank",
                params={"root": str(tmp_path), "q": "final-clean-request"})
            final_elapsed = time.monotonic() - t0

            loop = asyncio.get_running_loop()
            sem = index_router._interactive_lane_loops.get(loop)
            sem_value = sem._value if sem is not None else None

            return (responses, final_resp, final_elapsed, sem_value,
                    cancel_proof_resp)

    (responses, final_resp, final_elapsed, sem_value,
     cancel_proof_resp) = asyncio.run(run())

    # Finding 2's fix made this deterministic: "final-clean-request" never
    # matches the `q{N}` shape `fake_rank_worker` keys its wedge decision on,
    # so it always takes the short (0.05s) poll branch and always answers
    # 200 — regardless of how many burst calls actually reached the worker
    # or in what order, which is what made this flake under load before.
    assert final_resp.status_code == 200, final_resp.text
    assert final_elapsed < 0.5, final_elapsed
    assert not index_router._abandoned_reads
    # Direct, non-timing proof the lane gave every permit back: not merely
    # "requests eventually returned" but the semaphore itself is at full
    # width again.
    assert sem_value == 2

    # Finding 4: the dedicated, event-synchronised proof — not a race
    # against the burst — that cancellation reaches a worker thread already
    # in flight.
    assert cancel_proof_resp.status_code == 499, cancel_proof_resp.text
    assert "cancel-proof" in worker_cancelled, worker_cancelled

    statuses = [r.status_code for r in responses if not isinstance(r, Exception)]
    # Every response is one of: answered normally, abandoned by the client
    # (499), or hit the abandon-timeout backstop (503) for one of the truly
    # wedged (n % 4 == 0) calls — nothing else is a legitimate outcome here.
    assert all(s in (200, 499, 503) for s in statuses), statuses
    # Finding 3: with the poll loop now materially shorter than ABANDON_S, a
    # non-cancelled, non-wedged burst request must actually be able to
    # answer normally — this was structurally impossible before (every
    # burst response was 499 or 503, never 200).
    assert 200 in statuses, statuses
    # The client-cancel path is also exercised within the racy burst itself
    # (a bonus, not the proof — see the dedicated `cancel-proof` assertions
    # above for that): most of these 499s come from the route's
    # pre-submission `if token.cancelled` check, before `_rank_worker` is
    # ever called, which is exactly why this alone cannot prove the
    # worker-thread path (finding 4).
    assert 499 in statuses, statuses


def test_reap_abandoned_retrieves_the_exception():
    """Review finding E: the comment the old `_abandoned_reads.discard`
    done-callback carried claimed holding a reference to an abandoned future
    prevents asyncio's "exception was never retrieved" error log — it only
    DELAYS it until the future is actually garbage-collected, since nothing
    called `.exception()` on it. `token.cancel()` on the timeout path makes
    the abandoned worker likely raise `Cancelled`, so an abandoned future
    ends up carrying an unretrieved exception in the overwhelmingly common
    case, not a rare one.

    `fut._log_traceback` is asyncio's own internal flag for exactly this: it
    starts `True` the moment `set_exception` runs on an exception nobody has
    fetched yet, and only `.exception()` clears it — pinning it here is a
    direct check that `_reap_abandoned` actually retrieves, not merely a
    behavioural proxy for it."""
    async def _run():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        index_router._abandoned_reads.add(fut)
        fut.set_exception(RuntimeError("boom"))
        assert fut._log_traceback is True
        index_router._reap_abandoned(fut)
        assert fut not in index_router._abandoned_reads
        assert fut._log_traceback is False
        # Retrieved, so a second read is safe too — proves this isn't
        # accidentally leaving the future in a half-consumed state.
        assert isinstance(fut.exception(), RuntimeError)

    asyncio.run(_run())


def test_index_read_pool_exhaustion_is_a_fast_503(home, tmp_path, monkeypatch):
    """Item 3's loud-failure-on-exhaustion check. Seeds `_abandoned_reads`
    directly to the pool's own size rather than driving real timeouts: doing
    this end to end would mean actually parking `_INDEX_READ_POOL_SIZE` real,
    un-killable OS threads forever (that is the whole point of an abandoned
    read — nothing can stop it), which would permanently exhaust the
    process-wide pool for the rest of this test session, not just this one
    test. The exhaustion CHECK and its fast, explicit 503 is what this test
    is about; item 2's own timeout-and-abandon path is covered by
    `test_a_wedged_rank_request_does_not_permanently_hold_its_lane_slot`
    above.

    Isolates `_abandoned_reads` behind a fresh set via monkeypatch rather
    than asserting it starts empty: a *real* abandoned future (as created by
    the wedge test above) never completes by design, so if that test runs
    first in the same worker process its leftover future is still sitting in
    the real module-level set when this one starts — that is not a bug,
    it's the literal meaning of "abandoned"."""
    fake_pool = {object() for _ in range(index_router._INDEX_READ_POOL_SIZE)}
    monkeypatch.setattr(index_router, "_abandoned_reads", fake_pool)

    # The property under test is "refused before ever being submitted to the
    # pool" — not merely "fast". A wall-clock bound tight enough to catch a
    # regression to the unbounded queue (which would hang for the life of the
    # request, or at best ABANDON_S=15.0s) is also tight enough to flake on a
    # loaded CI runner with no real bug present (see D882: 0.3s tripped at
    # 0.366-0.658s on shared runners). Assert the property directly instead:
    # if this 503 is really produced pre-submission, `_submit_index_read`
    # (the only path onto the pool) must never be called.
    def _fail_if_submitted(*a, **kw):
        pytest.fail("exhaustion check did not short-circuit before submission")
    monkeypatch.setattr(index_router, "_submit_index_read", _fail_if_submitted)

    t0 = time.monotonic()
    resp = _client(tmp_path).get(
        "/api/index/rank", params={"root": str(tmp_path), "q": "x"})
    elapsed = time.monotonic() - t0

    assert resp.status_code == 503
    assert resp.json() == {"error": "index read pool exhausted"}
    # Belt-and-suspenders sanity bound, well under ABANDON_S (15.0s) with
    # real headroom for a loaded runner — not the property assertion itself.
    assert elapsed < 3.0, elapsed


@pytest.mark.parametrize("path,params", [
    ("/api/index/stats", {"root": ""}),
    ("/api/index/search", {"root": ""}),
])
def test_stats_and_search_also_get_the_fast_exhaustion_503(
        home, tmp_path, path, params, monkeypatch):
    """Review finding D: `api_index_rank` was the only one of the three
    `/api/index/*` read routes with a pool-exhaustion check, even though
    `api_index_stats` and `api_index_search` submit to the very same
    `_INDEX_READ_POOL` and share the very same interactive lane. Same
    isolated-set technique as `test_index_read_pool_exhaustion_is_a_fast_503`
    above, parametrized over the two routes that used to lack this."""
    params = dict(params, root=str(tmp_path))
    fake_pool = {object() for _ in range(index_router._INDEX_READ_POOL_SIZE)}
    monkeypatch.setattr(index_router, "_abandoned_reads", fake_pool)

    # See the sibling rank test above (D882): assert the property — refused
    # pre-submission — directly, rather than trusting a wall-clock bound
    # tight enough to catch an unbounded-queue regression not to also flake
    # on a loaded CI runner.
    def _fail_if_submitted(*a, **kw):
        pytest.fail("exhaustion check did not short-circuit before submission")
    monkeypatch.setattr(index_router, "_submit_index_read", _fail_if_submitted)

    t0 = time.monotonic()
    resp = _client(tmp_path).get(path, params=params)
    elapsed = time.monotonic() - t0

    assert resp.status_code == 503
    assert resp.json() == {"error": "index read pool exhausted"}
    # Belt-and-suspenders sanity bound, well under ABANDON_S (15.0s) with
    # real headroom for a loaded runner — not the property assertion itself.
    assert elapsed < 3.0, elapsed


def _make_fake_index_stats(never, calls, lock):
    def fake(cfg, root, breakdown, token=None):
        with lock:
            calls["n"] += 1
            is_first = calls["n"] == 1
        if is_first:
            never.wait()  # the un-killable, permanently-parked worker thread
        return {"ok": True}
    return fake


def _make_fake_index_search(never, calls, lock):
    def fake(cfg, root, q="", limit=0, token=None):
        with lock:
            calls["n"] += 1
            is_first = calls["n"] == 1
        if is_first:
            never.wait()  # the un-killable, permanently-parked worker thread
        return {"covered": False, "entries": []}
    return fake


@pytest.mark.parametrize("path,fake_target,make_fake", [
    ("/api/index/stats", "index_stats", _make_fake_index_stats),
    ("/api/index/search", "index_search", _make_fake_index_search),
])
def test_a_wedged_stats_or_search_request_does_not_permanently_hold_its_lane_slot(
        home, tmp_path, path, fake_target, make_fake, monkeypatch):
    """Review finding D, item 2's half: `api_index_rank` was the only route
    with a *bounded* wait — a wedged `stats` or `search` request could still
    permanently consume a pool thread and, since all three share the
    width-2 interactive lane, two such wedges parked forever would exhaust
    it for the life of the process with no rank request ever involved. Same
    shape as `test_a_wedged_rank_request_does_not_permanently_hold_its_lane_slot`
    above, parametrized over the other two routes, via the same shared
    `_bounded_index_read` helper both now use. Only the FIRST call blocks
    (a call counter, exactly like the rank version of this test) — otherwise
    the second, supposedly-healthy request would hit the very same
    `never.wait()` and time out too, since the fake is a plain function
    shared across every call, not a per-request stub."""
    import threading

    import httpx

    monkeypatch.setattr(index_router, "ABANDON_S", 0.05)
    never = threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()
    monkeypatch.setattr(index_router, fake_target, make_fake(never, calls, lock))

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            wedged_task = asyncio.create_task(
                client.get(path, params={"root": str(tmp_path)}))
            await asyncio.sleep(0.15)  # let the wedged request time out

            t0 = time.monotonic()
            second_resp = await client.get(path, params={"root": str(tmp_path)})
            second_elapsed = time.monotonic() - t0

            wedged_resp = await wedged_task

            # Same draining discipline as the rank version of this test
            # (finding F), and for the same reason: a real, un-killable OS
            # thread is parked in `never.wait()` until this fires, and it
            # must happen HERE, inside `run()`, while this loop is still
            # running — the abandoned future's done-callback is chained via
            # `call_soon` on this loop and never fires once `asyncio.run`
            # has torn it down.
            never.set()
            deadline = time.monotonic() + 2.0
            while index_router._abandoned_reads and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            return second_resp, second_elapsed, wedged_resp

    second_resp, second_elapsed, wedged_resp = asyncio.run(run())
    # A later request must not queue behind the wedged one.
    assert second_resp.status_code == 200, second_resp.text
    assert second_elapsed < 0.3, second_elapsed
    assert wedged_resp.status_code == 503
    assert wedged_resp.json() == {"error": "index read timed out"}
    assert not index_router._abandoned_reads


def test_a_slow_mount_guard_check_does_not_stall_the_event_loop(
        home, tmp_path, monkeypatch):
    """Item 4: `_rank_reason`'s `MountGuard(...).blocks_root(root)` call —
    whose own docstring already warns that a stat under a wedged rclone
    mount can block the calling thread indefinitely — used to run directly
    on the event loop, in `api_index_rank`, after the worker thread had
    already finished. A slow check there could stall the WHOLE server, every
    request sharing the loop, not merely the one rank request that triggered
    it. Folding it into `_rank_worker` (item 4) puts it on a pool thread
    instead — an unrelated route must still answer promptly while it is in
    flight."""
    import threading

    import httpx

    entered = threading.Event()

    def slow_blocks_root(self, root):
        entered.set()
        import time as _time
        _time.sleep(1.0)
        return False

    monkeypatch.setattr(index_router.MountGuard, "blocks_root", slow_blocks_root)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            # A real (never-scanned) root: `covered` comes back False, which
            # is exactly the branch that consults MountGuard.
            rank_task = asyncio.create_task(
                client.get("/api/index/rank",
                          params={"root": str(tmp_path), "q": "x"}))
            for _ in range(50):
                if entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert entered.is_set()

            t0 = time.monotonic()
            status_resp = await client.get("/api/index/status")
            elapsed = time.monotonic() - t0
            rank_resp = await rank_task
            return status_resp, elapsed, rank_resp

    status_resp, elapsed, rank_resp = asyncio.run(run())
    assert status_resp.status_code == 200
    assert elapsed < 0.3, elapsed
    assert rank_resp.status_code == 200


def test_rank_reason_is_mount_for_a_typed_path_the_guarded_walk_stopped_short_of(
        home, tmp_path, monkeypatch):
    """Review finding C: `MountGuard.blocks()` (item B's fix) is pure string
    comparison, so `_walk_from` now stops ONE SEGMENT SHORT of a blocked
    mount instead of stat-ing its way into it — `base` therefore lands on
    the last UNBLOCKED ancestor, never on the mount itself. Before item 1's
    guard existed, the walk ran all the way down (paying `os.path.isdir` on
    the mount, the very syscall this feature exists to avoid) and `base`
    ended up AT the mount, so `_rank_reason`'s `MountGuard(...).blocks_root
    (base)` caught it directly. With the guard, that same check on the
    (now short) `base` alone would miss it entirely, silently reporting
    whatever `_rank_reason` falls through to (`covered`/`ignored`/empty)
    instead of `mount` — which is exactly what would send the frontend's
    live-walk fallback (`home-search.ts`, `listing/index-source.ts`) down
    the wrong path for a typed mount query. `_rank_body`'s
    `blocked_query_path` side channel is what closes that gap without a
    second syscall."""
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    _point_home_at(monkeypatch, os_home)
    cfg = load_config()
    cfg.roots = [str(os_home)]
    index_router.save_config(cfg)

    # `os_home` itself is an ordinary, unguarded folder (it never scanned, so
    # `covered` comes back False on its own) — the guard only blocks
    # `os_home/.fused-render` (via `default_home_dirs()`), one segment deeper.
    # `q` escapes to `os_home` via `~` and then asks to walk one more segment
    # into exactly that blocked subtree.
    out = index_router._rank_worker(cfg, str(os_home), "~/.fused-render/x",
                                    limit=10, token=None, ranked=True)
    assert out["base"] == index_router.norm(str(os_home))
    assert out["reason"] == "mount"
    # The internal side channel must never reach the wire.
    assert "blocked_query_path" not in out


def test_ask_shares_the_query_lane_with_query(home, tmp_path, monkeypatch):
    """Review finding: `/api/index/ask` ran the same guarded DuckDB call
    `/api/index/query` does, over model-generated SQL, on a worker thread, and
    was not bounded by any semaphore at all. It must now share the
    authored-SQL lane with `query` — concurrent `query` + `ask` calls must
    never both be running their duckdb call at once."""
    import threading

    import httpx
    from fastapi.responses import JSONResponse

    lock = threading.Lock()
    state = {"concurrent": 0, "peak": 0}
    release = threading.Event()

    def fake_guarded(cfg, sql, limit, token=None):
        with lock:
            state["concurrent"] += 1
            state["peak"] = max(state["peak"], state["concurrent"])
        release.wait(timeout=5)
        with lock:
            state["concurrent"] -= 1
        return {"columns": [], "rows": []}

    async def fake_relay(body, session=None):
        return JSONResponse({"ok": True, "result": {"text": "select 1"}})

    monkeypatch.setattr(index_router, "_guarded", fake_guarded)
    monkeypatch.setattr(index_router._server_ai, "_ai_relay", fake_relay)

    async def run():
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            tasks = [
                asyncio.create_task(client.post(
                    "/api/index/query", json={"sql": "select 1"},
                    headers={"X-Fused": "1"})),
                asyncio.create_task(client.post(
                    "/api/index/ask", json={"prompt": "how many files"},
                    headers={"X-Fused": "1"})),
            ]
            for _ in range(50):
                with lock:
                    if state["concurrent"] >= 1:
                        break
                await asyncio.sleep(0.02)
            assert state["peak"] <= 1
            release.set()
            return await asyncio.gather(*tasks)

    responses = asyncio.run(run())
    assert all(r.status_code == 200 for r in responses)
    assert state["peak"] == 1  # the ceiling was actually reached


def test_a_lane_survives_a_second_contending_event_loop(home, tmp_path, monkeypatch):
    """The bug the lazy per-loop semaphore fixes (review finding, D701
    correction / D706): a bare module-level `asyncio.Semaphore()` binds to
    whichever event loop first CONTENDS on it, and a second event loop that
    later contends on it raises `RuntimeError: ... is bound to a different
    event loop` — surfacing as a 500 instead of a queued request. Two separate
    `asyncio.run` calls (two separate loops), each with MORE concurrent
    requests than the lane's width so each one genuinely contends, must both
    succeed."""
    import threading

    import httpx

    release = threading.Event()

    def fake_stats(cfg, root="", breakdown=False, token=None):
        release.wait(timeout=5)
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}

    monkeypatch.setattr(index_router, "index_stats", fake_stats)

    async def run_three_concurrent():
        # 3 requests against a width-2 lane: the third genuinely contends,
        # rather than merely acquiring an uncontended semaphore (which never
        # exercised the binding bug in the first place).
        transport = httpx.ASGITransport(app=_client(tmp_path).app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            tasks = [asyncio.create_task(client.get("/api/index/stats"))
                     for _ in range(3)]
            await asyncio.sleep(0.2)
            release.set()
            return await asyncio.gather(*tasks)

    first = asyncio.run(run_three_concurrent())
    release.clear()
    second = asyncio.run(run_three_concurrent())  # a brand-new event loop
    for resp in first + second:
        assert resp.status_code == 200


# -- config --------------------------------------------------------------------

def test_config_round_trips_roots_and_ignore(home, tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/index/config",
                       json={"roots": [str(tmp_path)], "ignore": ["node_modules", ""]},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 200
    # "roots" answers with `scan_roots(cfg)` — canonical_root form, not the
    # caller's raw spelling (platform.md §1; routers/index.scan_roots).
    canon = [runner.canonical_root(str(tmp_path))]
    assert resp.json()["roots"] == canon
    assert resp.json()["ignore"] == ["node_modules", ""]  # verbatim
    body = client.get("/api/index/config").json()
    assert body["roots"] == canon
    assert body["defaults"]  # the starting list is reported for a Reset button


def test_saving_a_changed_ignore_list_reconciles_the_index(home, tmp_path, monkeypatch):
    """Editing the rules must not leave the index disagreeing with them. The
    engine's fingerprint turns the next scan into a full rebuild, which purges
    newly-ignored rows and picks up newly-unignored ones — so the save starts
    that scan rather than waiting for the next boot."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append((root, full))
                        or {"run_id": "r", "root": root})
    cfg = load_config()
    # pretend an index exists, built under the current rules
    index_router.save_applied_ignore(cfg, index_router.scan_roots(cfg)[0])
    body = _client(tmp_path).post(
        "/api/index/config", json={"ignore": ["node_modules", "target"]},
        headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is True
    assert body["rescan_run_id"] == "r"
    assert started and started[0][1] is False  # the fingerprint forces the full one


def test_saving_an_unchanged_ignore_list_starts_nothing(home, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    index_router.save_applied_ignore(cfg, index_router.scan_roots(cfg)[0])
    body = _client(tmp_path).post("/api/index/config",
                                  json={"ignore": list(cfg.ignore)},
                                  headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is False
    assert started == []


def test_saving_rules_with_no_index_yet_starts_nothing(home, tmp_path, monkeypatch):
    """Nothing to reconcile before a first scan — and the startup scheduler
    will pick the new rules up anyway."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    body = _client(tmp_path).post("/api/index/config", json={"ignore": ["x"]},
                                  headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is False
    assert started == []


def test_saving_the_config_answers_in_the_same_shape_as_reading_it(home, tmp_path):
    """The panel replaces its whole state with the save's response, so the two
    shapes must agree. They did not: GET reports `roots` as the roots actually
    scanned (home, when none are configured) plus `configured_roots`, while
    the save reported the raw configured list and omitted the second field —
    so with no configured roots the panel's "Covers …" line vanished on save
    even though coverage had not changed."""
    client = _client(tmp_path)
    read = client.get("/api/index/config").json()
    saved = client.post("/api/index/config", json={"ignore": ["node_modules"]},
                        headers={"X-Fused": "1"}).json()
    assert saved["roots"] == read["roots"]  # the effective (home) fallback
    assert saved["roots"]  # ...and it is not empty
    assert saved["configured_roots"] == read["configured_roots"] == []


def test_saving_the_ignore_list_preserves_comments_and_blank_lines(home, tmp_path):
    """The panel documents `#` comments and round-trips the textarea through
    this response, so cleaning on save silently deleted the user's
    annotations the first time they touched the field."""
    raw = ["# dependency caches", "node_modules", "", ".venv"]
    client = _client(tmp_path)
    saved = client.post("/api/index/config", json={"ignore": raw},
                        headers={"X-Fused": "1"}).json()
    assert saved["ignore"] == raw
    assert client.get("/api/index/config").json()["ignore"] == raw
    # the rules the engine runs still see only the two patterns
    assert load_config().rules.patterns == ["node_modules", ".venv"]


def test_a_comment_only_edit_needs_no_rescan(home, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "r", "root": root})
    client = _client(tmp_path)
    client.post("/api/index/config", json={"ignore": ["node_modules"]},
                headers={"X-Fused": "1"})
    cfg = load_config()
    index_router.save_applied_ignore(cfg, index_router.scan_roots(cfg)[0])
    started.clear()
    body = client.post("/api/index/config",
                       json={"ignore": ["# deps", "node_modules", ""]},
                       headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is False
    assert started == []


def test_saving_rules_mid_scan_supersedes_the_running_scan(home, tmp_path, monkeypatch):
    """The reported bug, end to end: a skip-rules save while a scan is in
    flight must not be answered by joining that scan. The running worker
    carries the OLD ignore list and stamps it as applied, so joining it means
    the reconciling rescan the save promised never happens — while the panel
    says the index is being rebuilt."""
    spawned = []
    monkeypatch.setattr(index_router.runner.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or _FakePopen())
    root = tmp_path / "proj"
    root.mkdir()
    cfg = load_config()
    cfg.roots = [str(root)]
    cfg.ignore = ["node_modules"]
    index_router.save_config(cfg)
    # A scan is running under rules A, and rules A are what the index claims.
    live = index_router.runner.start(load_config(), str(root))
    # `save_applied_ignore` (unlike `runner.start`) does NOT canonicalize its
    # own `root` — it trusts the caller, because its real caller (`scan.
    # run_scan`) only ever gets there with the already-canonical spelling
    # `runner.start` wrote into spec.json. Passing the raw `str(root)` here
    # instead would file the fingerprint under a key `applied_ignore_sig`
    # (looked up via the canonical `scan_roots(cfg)` entries) can never find,
    # reading as "unknown" — which the route's `(sig or current) != current`
    # check treats as "already reconciled", so `needs_rescan` comes back
    # False instead of True.
    index_router.save_applied_ignore(load_config(),
                                     index_router.runner.canonical_root(str(root)))

    body = _client(tmp_path).post(
        "/api/index/config", json={"ignore": ["node_modules", "target"]},
        headers={"X-Fused": "1"}).json()

    assert body["needs_rescan"] is True
    assert body["rescan_run_id"] is not None
    assert body["rescan_run_id"] != live["run_id"]  # not the joined old run
    runs_dir = load_config().runs_dir
    assert os.path.exists(os.path.join(runs_dir, live["run_id"], "cancel"))
    fresh = json.load(open(os.path.join(runs_dir, body["rescan_run_id"], "spec.json")))
    assert fresh["config"]["ignore"] == ["node_modules", "target"]


def test_config_rejects_a_non_list(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/config", json={"roots": "nope"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


def test_default_scan_roots_are_the_users_home(home, tmp_path, monkeypatch):
    """Home, not the project root: a whole-home scan costs seconds with the
    default ignore rules and is what makes search useful everywhere."""
    real_expanduser = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "userhome")
                        if p == "~" else real_expanduser(p))
    # `scan_roots` answers in `runner.canonical_root` form (platform.md §1),
    # not the raw native-separator string `expanduser`/`str(Path)` hand back.
    assert index_router.scan_roots(load_config(), start_dir=str(tmp_path)) == [
        runner.canonical_root(str(tmp_path / "userhome"))]


def test_configured_roots_win_over_the_default(home, tmp_path):
    cfg = load_config()
    cfg.roots = [str(tmp_path / "proj")]
    assert index_router.scan_roots(cfg, start_dir=str(tmp_path)) == [
        runner.canonical_root(str(tmp_path / "proj"))]


def test_scan_roots_are_canonical_so_store_lookups_hit(home, tmp_path, monkeypatch):
    """Roots are KEYS, not just paths: runner.start files every fingerprint,
    debounce entry and freshness record under runner.canonical_root(root), and
    scan_roots' output is compared against those keys (the stale-fingerprint
    rescan, routers/git_repos._usable). A raw configured spelling misses —
    `~/proj` is not `/home/me/proj`, and on Windows `expanduser("~")` gives
    `C:\\Users\\me` against a stored `C:/Users/me`, so every lookup misses there
    and the index reads as permanently unreconciled.

    Asserted as "identical to what runner.start would use", not against a
    hand-written string: the whole bug is two spellings drifting, so the test has
    to pin them together rather than restate one of them (and a separator
    assertion would only ever fire on Windows, where this suite does not run)."""
    real_expanduser = os.path.expanduser
    fake_home = str(tmp_path / "userhome")

    def expand(p):
        if p == "~":
            return fake_home
        if p.startswith("~/"):
            return fake_home + p[1:]
        return real_expanduser(p)

    monkeypatch.setattr(os.path, "expanduser", expand)
    cfg = load_config()
    cfg.roots = ["~/proj", str(tmp_path / "other") + "/"]
    assert index_router.scan_roots(cfg) == [
        runner.canonical_root(r) for r in cfg.roots]
    # ~ really was expanded, so this is not a tautology over two no-ops. Still
    # wrapped in canonical_root: the concatenation above (`fake_home + p[1:]`)
    # is native-separator on Windows, and canonical_root is what scan_roots
    # itself would produce from that same expansion.
    assert index_router.scan_roots(cfg)[0] == runner.canonical_root(
        str(tmp_path / "userhome" / "proj"))
    # and the default root gets the same treatment
    cfg.roots = []
    assert index_router.scan_roots(cfg) == [runner.canonical_root("~")]


# -- manual actions ------------------------------------------------------------

def test_scan_passes_the_full_flag_through(home, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: seen.append(full)
                        or {"run_id": "r", "root": root})
    _client(tmp_path).post("/api/index/scan",
                           json={"root": str(tmp_path), "full": True},
                           headers={"X-Fused": "1"})
    assert seen == [True]


def test_scan_with_no_root_uses_the_configured_one(home, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: seen.append(root)
                        or {"run_id": "r", "root": root})
    cfg = load_config()
    cfg.roots = [str(tmp_path)]
    index_router.save_config(cfg)
    _client(tmp_path).post("/api/index/scan", json={}, headers={"X-Fused": "1"})
    # the route resolves the missing root through `scan_roots`, which answers
    # in canonical_root form, not the configured raw spelling.
    assert seen == [runner.canonical_root(str(tmp_path))]


def test_scan_with_no_root_covers_EVERY_root(home, tmp_path, monkeypatch):
    """Re-index presents itself as rebuilding the index, so it has to mean all
    of it: scanning only roots[0] left every other root stale with nothing in
    the UI to say so."""
    seen = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: seen.append((root, full))
                        or {"run_id": "run-" + os.path.basename(root),
                            "root": root})
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = load_config()
    cfg.roots = [str(a), str(b)]
    index_router.save_config(cfg)
    body = _client(tmp_path).post("/api/index/scan", json={"full": True},
                                  headers={"X-Fused": "1"}).json()
    # every root the route hands to `start` came out of `scan_roots`, in
    # canonical_root form.
    ca, cb = runner.canonical_root(str(a)), runner.canonical_root(str(b))
    assert seen == [(ca, True), (cb, True)]
    assert [r["root"] for r in body["runs"]] == [ca, cb]
    # the single-run fields stay, for a caller that only knows about one
    assert body["run_id"] == "run-a"
    assert body["root"] == ca


def test_scan_with_no_root_skips_roots_that_no_longer_exist(home, tmp_path, monkeypatch):
    """The config outlives the folders it names — one dead root must not fail
    the whole fan-out, exactly as the startup scheduler treats it."""
    def fake_start(cfg, root, full=False):
        if root.endswith("gone"):
            raise ValueError("not a directory: " + root)
        return {"run_id": "r", "root": root}

    monkeypatch.setattr(index_router.runner, "start", fake_start)
    live = tmp_path / "live"
    live.mkdir()
    cfg = load_config()
    cfg.roots = [str(tmp_path / "gone"), str(live)]
    index_router.save_config(cfg)
    resp = _client(tmp_path).post("/api/index/scan", json={},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert [r["root"] for r in resp.json()["runs"]] == [
        runner.canonical_root(str(live))]


def test_scan_with_no_root_and_nothing_startable_is_an_error(home, tmp_path, monkeypatch):
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: (_ for _ in ()).throw(
                            ValueError("not a directory: " + root)))
    cfg = load_config()
    cfg.roots = [str(tmp_path / "gone")]
    index_router.save_config(cfg)
    resp = _client(tmp_path).post("/api/index/scan", json={},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


def test_delete_requires_the_fused_header(home, tmp_path):
    assert _client(tmp_path).post("/api/index/delete").status_code == 403


def test_delete_removes_the_store_and_search_reverts_to_the_walk(home, tmp_path):
    """After a delete the explorer must degrade, not break: no index means
    covered:false, which is the same silent walk fallback as 'not scanned
    yet'."""
    src = _tree(tmp_path)
    client = _client(tmp_path)
    run_id = client.post("/api/index/scan", json={"root": str(src)},
                         headers={"X-Fused": "1"}).json()["run_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        if not client.get("/api/index/status", params={"run_id": run_id}).json()["running"]:
            break
        time.sleep(0.2)
    assert client.get("/api/index/status").json()["has_index"] is True

    resp = client.post("/api/index/delete", headers={"X-Fused": "1"})
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    status = client.get("/api/index/status").json()
    assert status["has_index"] is False
    assert status["files_indexed"] == 0
    body = client.get("/api/index/search", params={"root": str(src)}).json()
    assert body["covered"] is False and body["entries"] == []


def test_delete_on_an_empty_store_is_not_an_error(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/delete", headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_delete_cancels_a_running_scan(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "20260101-000000-live")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r"}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        f.write(json.dumps({"type": "phase", "msg": "scanning"}) + "\n")
    _client(tmp_path).post("/api/index/delete", headers={"X-Fused": "1"})
    # the cancel flag has to survive the delete, or the worker would happily
    # compact a fresh index into the store the user just emptied
    assert os.path.exists(os.path.join(d, "cancel"))


# -- status --------------------------------------------------------------------

def test_status_reports_scanning_and_index_presence_independently(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "20260101-000000-aa")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r"}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        f.write(json.dumps({"type": "progress", "files": 12}) + "\n")
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is True
    assert body["has_index"] is False   # nothing compacted yet: walk fallback
    assert body["files"] == 12          # this run's progress
    assert body["files_indexed"] == 0   # rows in a completed index
    assert body["last_completed_at"] is None


# -- the startup scheduler -----------------------------------------------------

def test_startup_schedules_one_scan_per_root(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "x", "root": root})
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == [runner.canonical_root(str(src))]


def test_startup_scan_is_debounced(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    # `_record_scan` does not canonicalize its own `root` (only `last_scan`'s
    # read side does — see test_index_freshness's floor test for the same
    # asymmetry), so seeding the debounce record directly like this has to
    # canonicalize first or `run_startup_scan`'s debounce check never finds
    # it and starts a scan anyway.
    runner._record_scan(cfg, runner.canonical_root(str(src)))  # a scan just ran
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == []


def test_the_startup_debounce_is_a_few_minutes_not_fifteen(home):
    """Its stated job is stopping a dev-server reload loop (or three windows
    opening at once) from queueing scan after scan — a job five minutes does
    exactly as well as fifteen, at a quarter the cost to a machine that really
    was left on and reopened."""
    assert 4 * 60 <= index_router.SCAN_DEBOUNCE_S <= 6 * 60


def test_startup_scan_rescans_once_the_debounce_has_elapsed(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    runner._record_scan(cfg, str(src))
    monkeypatch.setattr(index_router, "SCAN_DEBOUNCE_S", 0)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == [runner.canonical_root(str(src))]


def test_startup_scan_never_raises(home, tmp_path, monkeypatch):
    """Housekeeping must not be able to stop the server from serving."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(index_router.runner, "start", boom)
    cfg = load_config()
    cfg.roots = [str(tmp_path)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))  # no exception


def test_startup_scan_skips_every_root_while_indexing_is_off(home, tmp_path,
                                                              monkeypatch):
    """No scan ever starts from any trigger, including the one every boot
    fires unconditionally."""
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    monkeypatch.setattr(index_router.index_gate.prefs, "indexing_enabled", lambda: False)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == []


def test_startup_scan_skips_a_root_that_is_gone(home, tmp_path, monkeypatch):
    """A missing root is runner.start's ValueError (raised after its mount
    guard) — the scheduler skips it quietly and still scans the remaining
    roots. Deliberately NO os.path.isdir in the scheduler itself: a kernel
    stat on a path under a wedged mount would hang the startup hook."""
    started = []

    def fake_start(cfg, root, full=False):
        if not os.path.isdir(root):
            raise ValueError(f"not a directory: {root}")
        started.append(root)
        return {"run_id": "r", "root": root}

    monkeypatch.setattr(index_router.runner, "start", fake_start)
    ok = tmp_path / "ok"
    ok.mkdir()
    cfg = load_config()
    cfg.roots = [str(tmp_path / "deleted"), str(ok)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == [runner.canonical_root(str(ok))]


# -- the compact corpus --------------------------------------------------------

def _corpus(n=3):
    """A corpus in the shape search_under answers with, nulls included."""
    entries = [{"rel": "d", "is_dir": True, "size": None, "mtime": None}]
    entries += [{"rel": f"d/f{i}.txt", "is_dir": False, "size": i,
                 "mtime": 1700000000.5 + i} for i in range(n)]
    return {"covered": True, "fresh": True, "updated": 1.0, "age_s": 2.0,
            "root": "/r", "entries": entries, "truncated": False,
            "total": len(entries), "scanned_partitions": 1,
            "of_partitions": 1}


def _stub_corpus(monkeypatch, out):
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: dict(out))


def test_search_answers_in_columns_when_asked(home, tmp_path, monkeypatch):
    """The corpus is the home page's whole ranking set — 25.7 MB of
    `{rel,is_dir,size,mtime}` objects on a 164k-entry home, most of it repeated
    key names. `fmt=columns` sends parallel arrays instead."""
    _stub_corpus(monkeypatch, _corpus())
    body = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "columns"}).json()
    assert body["fmt"] == "columns"
    assert "entries" not in body
    assert body["rels"] == ["d", "d/f0.txt", "d/f1.txt", "d/f2.txt"]
    assert body["dirs"] == [1, 0, 0, 0]
    # Nulls are entries a directory legitimately has, not missing data.
    assert body["sizes"] == [None, 0, 1, 2]
    assert body["mtimes"] == [None, 1700000000.5, 1700000001.5, 1700000002.5]


def test_the_two_formats_carry_the_same_corpus_and_metadata(home, tmp_path,
                                                            monkeypatch):
    """`fmt` changes the encoding of the entries and nothing else: every
    client decision (covered/fresh/truncated/…) reads the same fields."""
    _stub_corpus(monkeypatch, _corpus())
    client = _client(tmp_path)
    classic = client.get("/api/index/search", params={"root": "/r"}).json()
    columns = client.get("/api/index/search",
                         params={"root": "/r", "fmt": "columns"}).json()
    assert "fmt" not in classic and classic["entries"] == _corpus()["entries"]
    decoded = [{"rel": r, "is_dir": bool(d), "size": s, "mtime": m}
               for r, d, s, m in zip(columns["rels"], columns["dirs"],
                                     columns["sizes"], columns["mtimes"])]
    assert decoded == classic["entries"]
    assert ({k: v for k, v in classic.items() if k != "entries"}
            == {k: v for k, v in columns.items()
                if k not in ("fmt", "rels", "dirs", "sizes", "mtimes")})


def test_an_unknown_fmt_answers_in_the_classic_shape(home, tmp_path, monkeypatch):
    """The bridge (`fused.fileIndex.search`, static/runtime.js) and every other
    caller ask with no `fmt` at all, so anything but the one known value has to
    be the old shape rather than an error."""
    _stub_corpus(monkeypatch, _corpus())
    body = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "parquet"}).json()
    assert [e["rel"] for e in body["entries"]] == ["d", "d/f0.txt", "d/f1.txt",
                                                   "d/f2.txt"]


def test_columns_are_several_times_smaller_on_the_wire(home, tmp_path, monkeypatch):
    """The transfer is the third of the three costs of the first search (25.7 MB
    on a 164k-entry home). Content-Length, not the decoded body: the compact
    format is also gzipped, and both halves are the win."""
    _stub_corpus(monkeypatch, _corpus(n=2000))
    client = _client(tmp_path)
    classic = client.get("/api/index/search", params={"root": "/r"})
    columns = client.get("/api/index/search",
                         params={"root": "/r", "fmt": "columns"})
    assert columns.headers["content-encoding"] == "gzip"
    wire = (int(classic.headers["content-length"]),
            int(columns.headers["content-length"]))
    assert wire[0] / wire[1] >= 3.0, wire


@pytest.mark.parametrize("accept,gzipped", [
    ("gzip", True),
    ("gzip, deflate, br", True),
    ("x-gzip", True),
    ("gzip;q=0.5", True),
    ("*", True),
    # `q=0` is the explicit "I cannot take this encoding" spelling, and a
    # substring match read it as consent — handing a client a 5 MB body it
    # just said it could not decode.
    ("gzip;q=0", False),
    ("gzip; q=0.0", False),
    ("*;q=0", False),
    ("identity", False),
    ("", False),
])
def test_gzip_is_negotiated_by_q_value_not_by_substring(home, tmp_path,
                                                        monkeypatch, accept,
                                                        gzipped):
    _stub_corpus(monkeypatch, _corpus())
    resp = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "columns"},
                                 headers={"Accept-Encoding": accept})
    assert (resp.headers.get("content-encoding") == "gzip") is gzipped
    # Both bodies live at one URL, so an intermediary keyed on the URL alone
    # would otherwise serve either one to either client.
    assert resp.headers["vary"] == "Accept-Encoding"
    assert resp.json()["rels"] == ["d", "d/f0.txt", "d/f1.txt", "d/f2.txt"]


def test_a_client_that_cannot_gunzip_still_gets_the_columns(home, tmp_path,
                                                            monkeypatch):
    """Encoding is negotiated, not assumed: `Accept-Encoding` decides whether
    the body is compressed, and the document inside it is the same either way."""
    _stub_corpus(monkeypatch, _corpus())
    resp = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "columns"},
                                 headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in resp.headers
    assert resp.json()["rels"] == ["d", "d/f0.txt", "d/f1.txt", "d/f2.txt"]


# -- the startup warm ----------------------------------------------------------

def test_startup_warm_runs_the_home_pages_first_search(home, tmp_path, monkeypatch):
    """The warm must ask for exactly what the home page asks for.

    FilesHome searches `config.home` (routers/config.py — `expanduser("~")`),
    not the folder the app was opened on, so a warm aimed anywhere else fills
    a pool the first keystroke never reads."""
    _point_home_at(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: seen.append(("search", root))
                        or {"covered": True, "root": root, "entries": []})
    cfg = load_config()
    cfg.roots = [str(tmp_path)]
    index_router.save_config(cfg)
    index_router.run_startup_warm()
    # Same call the route makes: /api/index/search's corpus, aimed at home.
    assert seen == [("search", index_router.runner.canonical_root("~"))]


def test_startup_warm_never_raises(home, tmp_path, monkeypatch):
    """It runs on a background thread nobody joins: a raise here would be an
    unhandled exception in the log and a permanently cold pool."""
    def boom(*a, **k):
        raise RuntimeError("duckdb on fire")

    monkeypatch.setattr(index_router, "index_search", boom)
    index_router.run_startup_warm()  # no exception


def test_startup_warm_refuses_a_mount_backed_home(home, tmp_path, monkeypatch):
    """The index refuses to scan mounts, so a warm aimed at one could only
    ever answer `covered: false` — after paying kernel I/O on a mount path,
    which is the one thing this codebase never does speculatively."""
    monkeypatch.setattr(index_router.MountGuard, "blocks_root",
                        lambda self, root: True)
    called = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: called.append(root) or {})
    index_router.run_startup_warm()
    assert called == []


def test_the_warm_wait_ceiling_still_sits_just_past_the_abandoned_threshold():
    """The ceiling exists so a worker killed mid-walk (never writes `run_end`)
    is spotted by the ABANDONED_RUN_S mtime check before the warm gives up for
    the pathological reason (a worker alive but wedged) instead. That ordering
    breaks if the two ever drift apart — a ceiling shorter than the threshold
    would give up before a merely-slow-but-live worker's death could even be
    detected."""
    assert index_router.WARM_WAIT_DEADLINE_S > runner.ABANDONED_RUN_S
    assert index_router.WARM_WAIT_DEADLINE_S - runner.ABANDONED_RUN_S <= 60


def test_startup_scan_records_the_run_the_warm_waits_on(home, tmp_path, monkeypatch):
    """The warm waits on the run THIS process started, so the scheduler has to
    hand it over — `run_startup_scan` used to drop `runner.start`'s run id on
    the floor."""
    src = _tree(tmp_path)
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: {"run_id": "r-42",
                                                       "root": root})
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert index_router._startup_runs == {runner.canonical_root(str(src)): "r-42"}


def test_startup_warm_waits_for_the_scan_it_started(home, tmp_path, monkeypatch):
    """The first-ever boot, end to end — the case the warm exists for.

    On a fresh index the warm's first search answers `covered: false` cheaply
    and there is nothing to sweep; the scan this process just spawned finishes
    seconds later. Sampling once left the user's first keystroke paying the
    whole cold cost anyway (2.3 s, observed). Waiting for that one run and
    searching again after it is what fills the corpus before the user's first
    keystroke."""
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    # HOME is what the warm aims at, and what the scheduler scans.
    _point_home_at(monkeypatch, src)
    index_router.run_startup_scan(start_dir=str(tmp_path))

    # Force the uncovered branch rather than racing the worker: the first
    # search must answer `covered: false` for this to be the boot being tested.
    real_search = index_router.index_search
    calls = []

    def uncovered_once(cfg, root, **kw):
        calls.append(root)
        if len(calls) == 1:
            return {"covered": False, "root": root}
        return real_search(cfg, root, **kw)

    monkeypatch.setattr(index_router, "index_search", uncovered_once)
    monkeypatch.setattr(index_router, "WARM_WAIT_POLL_S", 0.05)
    monkeypatch.setattr(index_router, "WARM_WAIT_DEADLINE_S", 60.0)
    index_router.run_startup_warm()

    assert len(calls) == 2, "the warm did not search again after the scan"


def test_startup_warm_gives_up_when_the_scan_never_finishes(home, tmp_path,
                                                             monkeypatch):
    """A worker alive but wedged — writing nothing, dying never — must not
    leave a thread polling for the process lifetime: the wait is
    deadline-bounded and the warm simply does not happen."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / "wedged-run"
    run_dir.mkdir()
    calls = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: calls.append(root)
                        or {"covered": False, "root": root})
    monkeypatch.setattr(index_router.runner, "_run_dir",
                        lambda cfg, run_id: str(run_dir))
    # Alive, so the dead-worker exit cannot fire: only the deadline can end it.
    monkeypatch.setattr(index_router.runner, "_looks_abandoned",
                        lambda run_dir, now, threshold_s: False)
    monkeypatch.setattr(index_router, "WARM_WAIT_DEADLINE_S", 0.05)
    monkeypatch.setattr(index_router, "WARM_WAIT_POLL_S", 0.01)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-that-hangs")
    began = time.monotonic()
    index_router.run_startup_warm()
    assert time.monotonic() - began < 5, "the wait is not deadline-bounded"
    # It still searches: how the wait ended says nothing about whether the
    # index covers the root, and an uncovered one costs a cheap `covered:
    # false` — exactly what the original single-shot warm already paid.
    assert len(calls) == 2


def test_startup_warm_stops_waiting_on_a_worker_that_died(home, tmp_path,
                                                          monkeypatch):
    """A worker killed mid-walk never writes `run_end`, so the log alone would
    keep the wait going until the deadline. runner's own mtime liveness check
    is what ends it in seconds instead."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / "dead-run"
    run_dir.mkdir()
    calls = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: calls.append(root)
                        or {"covered": False, "root": root})
    monkeypatch.setattr(index_router.runner, "_run_dir",
                        lambda cfg, run_id: str(run_dir))
    monkeypatch.setattr(index_router.runner, "_looks_abandoned",
                        lambda run_dir, now, threshold_s: True)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-that-died")
    began = time.monotonic()
    index_router.run_startup_warm()
    assert time.monotonic() - began < 5
    assert len(calls) == 2


def test_startup_warm_searches_again_when_the_run_dir_vanishes_mid_wait(
        home, tmp_path, monkeypatch):
    """`prune_runs` reclaims run DIRS; it never touches the store. So a run
    dir that disappears while the warm is waiting on it says nothing about
    whether the index covers the root — reading it as "the scan died, skip the
    warm" silently left the pool cold for an index that was complete."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / "pruned-run"
    run_dir.mkdir()
    calls, ranked = [], []

    def search(cfg, root, **kw):
        calls.append(root)
        if len(calls) == 1:
            run_dir.rmdir()  # pruned out from under the wait
            return {"covered": False, "root": root}
        return {"covered": True, "root": root, "entries": []}

    monkeypatch.setattr(index_router, "index_search", search)
    monkeypatch.setattr(index_router, "_rank_body",
                        lambda cfg, root, q, **kw: ranked.append(root) or {})
    monkeypatch.setattr(index_router.runner, "_run_dir",
                        lambda cfg, run_id: str(run_dir))
    monkeypatch.setattr(index_router, "WARM_WAIT_POLL_S", 0.01)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-that-was-pruned")
    index_router.run_startup_warm()
    assert len(calls) == 2
    # The unconditional rank warm still runs, over the SECOND (covered) search.
    assert len(ranked) == 1


def test_startup_warm_does_not_wait_when_the_root_is_covered(home, tmp_path,
                                                             monkeypatch):
    """The overwhelmingly common boot: an index is already there, so the warm
    is the same two calls it always was, with no wait in the way."""
    monkeypatch.setenv("HOME", str(tmp_path))
    waited, ranked = [], []
    monkeypatch.setattr(index_router, "_wait_for_scan",
                        lambda cfg, run_id: waited.append(run_id) or True)
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: {"covered": True, "root": root,
                                                 "entries": []})
    monkeypatch.setattr(index_router, "_rank_body",
                        lambda cfg, root, q, **kw: ranked.append(root) or {})
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-1")
    index_router.run_startup_warm()
    assert waited == []
    assert len(ranked) == 1


def test_startup_warm_does_not_wait_when_the_scan_was_debounced(home, tmp_path,
                                                                monkeypatch):
    """No run was started, so there is nothing to wait for — and a debounced
    root was scanned within SCAN_DEBOUNCE_S, so the index is already there.
    Waiting here would block the warm on a run that never comes."""
    src = _tree(tmp_path)
    monkeypatch.setenv("HOME", str(src))
    monkeypatch.setattr(
        index_router.runner, "start",
        lambda cfg, root, full=False: pytest.fail("debounced root was scanned"))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    runner._record_scan(cfg, runner.canonical_root(str(src)))
    index_router.run_startup_scan(start_dir=str(tmp_path))

    waited, calls = [], []
    monkeypatch.setattr(index_router, "_wait_for_scan",
                        lambda cfg, run_id: waited.append(run_id) or True)
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: calls.append(root)
                        or {"covered": False, "root": root})
    index_router.run_startup_warm()
    assert waited == []
    assert len(calls) == 1


def test_a_root_of_slash_survives_the_config_write(home, tmp_path):
    """Roots are paths, not ignore patterns: clean_patterns rstrips '/' into
    the empty string and silently drops the root."""
    body = _client(tmp_path).post("/api/index/config", json={"roots": ["/"]},
                                  headers={"X-Fused": "1"}).json()
    # canonical_root("/") is "/" on POSIX but the drive root ("D:/") on
    # Windows — still a bare root either way, just not the literal "/".
    assert body["roots"] == [runner.canonical_root("/")]


def test_scanning_reflects_every_run_not_just_the_newest(home, tmp_path):
    """With several roots, a quick second scan can finish (and become the
    newest run) while the first root's is still walking — the status bit
    must keep saying scanning until they all settle."""
    cfg = load_config()
    for rid, events in (("20260101-000000-aa", [{"type": "phase", "msg": "scanning"}]),
                        ("20260102-000000-bb", [{"type": "run_end", "msg": "complete"}])):
        d = os.path.join(cfg.runs_dir, rid)
        os.makedirs(d)
        with open(os.path.join(d, "spec.json"), "w") as f:
            json.dump({"root": "/r"}, f)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is True


def test_a_rules_edit_rescans_every_stale_root_not_just_the_first(home, tmp_path, monkeypatch):
    """The reconciling rescan used to go to roots[0] only; the first root's
    scan then stamped the (global) fingerprint and every other root looked
    reconciled forever — re-included folders stayed permanently missing from
    their slices. Every root whose per-root sig differs gets its own scan."""
    started = []

    def fake_start(cfg, root, full=False):
        started.append(root)
        return {"run_id": f"r{len(started)}", "root": root}

    monkeypatch.setattr(index_router.runner, "start", fake_start)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = load_config()
    cfg.roots = [str(a), str(b)]
    index_router.save_config(cfg)
    # `save_applied_ignore` trusts its `root` verbatim (only `runner.start`
    # canonicalizes before calling it in production) — a raw native-separator
    # literal here would file the fingerprint under a key the route's
    # `scan_roots`-keyed staleness check can never find, reading as "already
    # reconciled" instead of stale.
    ca, cb = runner.canonical_root(str(a)), runner.canonical_root(str(b))
    index_router.save_applied_ignore(cfg, ca)
    index_router.save_applied_ignore(cfg, cb)
    body = _client(tmp_path).post(
        "/api/index/config", json={"ignore": ["node_modules", "target"]},
        headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is True
    assert started == [ca, cb]
    assert body["rescan_run_ids"] == ["r1", "r2"]


# -- open-folder freshness -----------------------------------------------------

def test_listing_a_folder_notes_it_for_the_freshness_check(home, tmp_path,
                                                           monkeypatch):
    """The hook is on /api/fs/list because that is what "opened a folder"
    actually is; the check itself must never run on the request thread."""
    seen = []
    monkeypatch.setattr(index_router, "note_folder_opened",
                        lambda p: seen.append(p) or True)
    src = _tree(tmp_path)
    resp = _client(tmp_path).get("/api/fs/list", params={"path": str(src)})
    assert resp.status_code == 200
    assert seen == [str(src)]


@pytest.fixture
def instant_freshness_delay(monkeypatch):
    """FRESHNESS_DELAY_S off the clock. These tests drive `_run_freshness_check`
    synchronously, and the wait is not what they are about — paying it for real
    would add three seconds per call to the suite.

    The CONSTANT is what gets patched, not `time.sleep`: patching the stdlib
    would void every sleep in the process for the duration of the test,
    including store.py's NT-lock poll and this module's warm wait, turning them
    into hot spins."""
    monkeypatch.setattr(index_router, "FRESHNESS_DELAY_S", 0.0)


def _freshness_root(tmp_path):
    """A tree that is a configured scan root, with the check clock cleared."""
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    return src


def test_the_freshness_check_defers_before_it_stamps_the_check_clock(
        home, tmp_path, monkeypatch):
    """The check waits out the page's opening burst before it commits.

    /home lists one folder per Claude session card, so a plain refresh lands a
    handful of /api/fs/list inside the first second and the scan one of them may
    start piles onto the paint.

    The ordering against the STAMP is the assertion. `_freshness_due` records the
    check the moment it decides one is due, and stamping and then waiting would
    record a check three seconds before it actually happened — and, worse, would
    make the wait unskippable for checks that are about to refuse. So the wait
    must sit after the free gates and before the stamp. Nothing here waits on a
    real clock; the seam is patched, not the stdlib."""
    events = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    # The stamp map, snapshotted as the wait begins. Still empty is what says
    # the wait ran BEFORE _freshness_due stamped.
    monkeypatch.setattr(index_router, "_freshness_wait", lambda s: events.append(
        ("waited", s, dict(index_router._freshness_checked))))
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots, now=None: events.append(("checked", path))
                        or index_router.freshness.FreshnessCheck())
    src = _freshness_root(tmp_path)
    index_router._run_freshness_check(str(src))
    assert events == [("waited", index_router.FRESHNESS_DELAY_S, {}),
                      ("checked", str(src))]
    # ...and the stamp did land, on the far side of the wait. Stamped under
    # `enclosing_root`'s canonical match, not the raw `path` the check was
    # called with (that raw form is what `note_folder_opened` sees above).
    assert runner.canonical_root(str(src)) in index_router._freshness_checked


def test_a_check_that_will_refuse_anyway_never_waits(home, tmp_path,
                                                     monkeypatch):
    """The wait holds the one-at-a-time slot, so only a check that is going to do
    something may pay it.

    Both refusals here are the common case, not the corner: a folder on screen
    re-lists about once a second, so a wait before the interval gate would mean
    the slot is held essentially forever, dropping listings of other roots that
    ARE stale. And on /home, a card sitting under no configured root would burn
    the window doing nothing while every sibling card is turned away — the page
    load would get no check at all, which is the opposite of the point."""
    waits = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router, "_freshness_wait", waits.append)
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots, now=None: index_router.freshness.FreshnessCheck())
    src = _freshness_root(tmp_path)
    # Under no configured root at all.
    index_router._run_freshness_check(str(tmp_path.parent))
    assert waits == []
    # Under a root that was checked moments ago. `_freshness_checked` is
    # keyed on `enclosing_root`'s canonical match, not the raw path the check
    # is called with — seeding it under the raw literal would always miss and
    # every check would read as newly-due.
    canon_src = runner.canonical_root(str(src))
    index_router._freshness_checked[canon_src] = time.time()
    index_router._run_freshness_check(str(src))
    assert waits == []
    # ...and the wait IS paid once the root is genuinely due, so the two
    # assertions above are about due-ness and not about a wait that never runs.
    index_router._freshness_checked[canon_src] -= index_router.FRESHNESS_CHECK_S + 1
    index_router._run_freshness_check(str(src))
    assert waits == [index_router.FRESHNESS_DELAY_S]


def test_the_freshness_slot_is_freed_after_a_deferred_check(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The slot is held across the wait, so it must still be released on every
    way out of it — including the early refusals, which are the common case.
    A leak would block the check for the rest of the process's life, so the next
    refresh a minute later would silently never check anything."""
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots, now=None: index_router.freshness.FreshnessCheck())
    src = _freshness_root(tmp_path)
    # The slot is a module global: a failed assertion below must not leave it
    # held, or every later test that touches the real hook fails for an
    # unrelated reason and hides this one.
    for target in (str(tmp_path.parent), str(src)):
        assert index_router._freshness_slot.acquire(blocking=False)
        try:
            # Two ways out: `tmp_path.parent` is under no root and refuses
            # before the gates, `src` runs the check all the way through.
            index_router._run_freshness_check(target)
            assert not index_router._freshness_slot.locked()
        finally:
            if index_router._freshness_slot.locked():
                index_router._freshness_slot.release()


def test_the_freshness_check_runs_at_most_one_at_a_time(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """A folder being watched re-lists on every mtime tick, so the hook fires
    far more often than a check costs. Overlapping checks would each open
    duckdb over dirs.parquet for nothing."""
    threads = []
    monkeypatch.setattr(index_router.threading, "Thread",
                        lambda **kw: threads.append(kw) or _FakeThread())
    # The REAL function, bound at import: conftest's _no_startup_index_scan
    # replaces the module attribute for every test, so that this hook cannot
    # spawn a home scan from a suite that merely lists a directory.
    assert _real_note_folder_opened(str(tmp_path)) is True
    assert _real_note_folder_opened(str(tmp_path)) is False
    assert len(threads) == 1
    # The slot frees once the check finishes, so the next open is checked again.
    threads[0]["target"](*threads[0]["args"])
    assert _real_note_folder_opened(str(tmp_path)) is True


def test_note_folder_opened_starts_nothing_while_indexing_is_off(
        home, tmp_path, monkeypatch):
    """The one gate for every caller of this function (fs_read.py,
    git_repos.py, exported_apps.py): a listing must never turn into a check,
    let alone a scan, while the pref is off."""
    threads = []
    monkeypatch.setattr(index_router.threading, "Thread",
                        lambda **kw: threads.append(kw) or _FakeThread())
    monkeypatch.setattr(index_router.index_gate.prefs, "indexing_enabled", lambda: False)
    assert _real_note_folder_opened(str(tmp_path)) is False
    assert threads == []


class _FakeThread:
    def start(self):
        pass


def test_a_stale_open_folder_gets_its_configured_root_rescanned(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The glue end to end, synchronously: the persisted config supplies the
    roots and the check fires the ordinary incremental scan of the enclosing
    one."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "r1", "root": root})
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    sub = src / "sub"
    # An index that recorded `sub` long before its current mtime.
    _write_dirs_index(load_config(), {str(src): 1, str(sub): 1})
    monkeypatch.setattr(index_router.freshness, "QUIET_S", 0.0)
    index_router._run_freshness_check(str(sub))
    assert started == [runner.canonical_root(str(src))]


def test_a_root_checked_moments_ago_is_not_checked_again(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The in-flight lock drops OVERLAPPING checks, not the ones that follow —
    so browsing folder to folder checked (and could rescan) on every open, and
    every scan that completed invalidated every corpus the client had fetched.
    A root is now checked at most once per FRESHNESS_CHECK_S."""
    checked = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots, now=None: checked.append(path)
                        or index_router.freshness.FreshnessCheck())
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router._run_freshness_check(str(src / "sub"))
    # A second folder UNDER THE SAME ROOT is the same question: a scan is per
    # root, so checking again buys nothing.
    index_router._run_freshness_check(str(src))
    assert checked == [str(src / "sub")]
    # ...and it comes back round once the window has passed. Keyed on
    # `enclosing_root`'s canonical match, same as the neighbouring tests —
    # the raw literal was never a key here to begin with.
    index_router._freshness_checked[runner.canonical_root(str(src))] -= (
        index_router.FRESHNESS_CHECK_S + 1)
    index_router._run_freshness_check(str(src))
    assert checked == [str(src / "sub"), str(src)]


def test_a_folder_outside_every_root_never_reaches_the_index(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """Cheapest gate first: no enclosing root means no scan is possible, so the
    duckdb lookup behind note_folder_opened must not be paid at all."""
    checked = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots, now=None: checked.append(path)
                        or index_router.freshness.FreshnessCheck())
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router._run_freshness_check(str(tmp_path.parent))
    assert checked == []


def test_a_folder_that_goes_quiet_after_the_check_refused_it_still_gets_scanned(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The reported bug. A file lands in a watched folder; the watcher's
    debounce means the check that change wakes runs a few seconds later,
    while the folder is still within its quiet window, and is refused. If the
    user just sits there, nothing else ever asks again — no later listing is
    coming to happen to land past the window. The check must leave a retry
    behind that fires once the folder has actually gone quiet, with no
    further listing in between."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "r1", "root": root})
    scheduled = []
    monkeypatch.setattr(index_router, "_schedule_freshness_retry",
                        lambda path, root, delay: scheduled.append(
                            (path, root, delay)))
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    sub = src / "sub"
    _write_dirs_index(load_config(), {str(src): 1, str(sub): 1})
    disk_mtime = os.stat(str(sub)).st_mtime
    # The watcher's own debounce: the check runs ~1s after the change, still
    # inside the quiet window (freshness.QUIET_S).
    check_now = disk_mtime + 1.0
    index_router._run_freshness_check(str(sub), now=check_now)
    assert started == []  # refused, exactly as reported
    assert len(scheduled) == 1
    retry_path, retry_root, retry_delay = scheduled[0]
    assert retry_root == runner.canonical_root(str(src))
    # Nobody lists anything again. The retry alone, firing once the folder has
    # actually been quiet for freshness.QUIET_S, is what must find the change.
    quiet_now = check_now + retry_delay + 0.01
    index_router._run_freshness_retry(retry_path, retry_root, now=quiet_now)
    assert started == [runner.canonical_root(str(src))]


def test_the_freshness_retry_is_coalesced_per_root(monkeypatch):
    """A folder touched fifty times while a retry is pending must not queue
    fifty timers — only the latest deadline for the root is kept."""
    made = []

    class _FakeTimer:
        def __init__(self, delay, fn, args=()):
            self.delay, self.fn, self.args = delay, fn, args
            self.cancelled = False

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    def fake_timer(delay, fn, args=()):
        t = _FakeTimer(delay, fn, args)
        made.append(t)
        return t

    monkeypatch.setattr(index_router.threading, "Timer", fake_timer)
    monkeypatch.setattr(index_router, "_freshness_retries", {})
    for i in range(50):
        index_router._schedule_freshness_retry("/some/path", "/some/root",
                                               10.0 + i)
    assert len(index_router._freshness_retries) == 1
    assert [t.cancelled for t in made].count(False) == 1
    live = index_router._freshness_retries["/some/root"]
    assert live.delay == 10.0 + 49


def test_the_freshness_retry_does_not_chain_a_second_one(
        home, tmp_path, monkeypatch):
    """One honest retry, not an unbounded chain: if the folder is somehow
    still churning when the retry fires, nothing schedules another."""
    scheduled = []
    monkeypatch.setattr(index_router, "_schedule_freshness_retry",
                        lambda path, root, delay: scheduled.append(delay))
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots, now=None:
                        index_router.freshness.FreshnessCheck(
                            retry_after=5.0))
    src = _tree(tmp_path)
    index_router._freshness_retries.pop(str(src), None)
    index_router._run_freshness_retry(str(src), str(src))
    assert scheduled == []
    assert str(src) not in index_router._freshness_retries


# -- guarded SQL ---------------------------------------------------------------

def _indexed_client(tmp_path, dirs=None):
    """A client whose index holds one real dirs row, so SQL has something to
    read."""
    cfg = load_config()
    _write_dirs_index(cfg, dirs or {str(tmp_path): 1})
    return _client(tmp_path)


@pytest.mark.parametrize("path,body", [
    ("/api/index/query", {"sql": "SELECT 1"}),
    ("/api/index/ask", {"prompt": "how many files"}),
])
def test_the_query_routes_require_the_fused_header(home, tmp_path, path, body):
    """Read-only, but they execute a caller-shaped statement — so they are
    POST-only and guarded, unlike GET /search."""
    resp = _client(tmp_path).post(path, json=body)
    assert resp.status_code == 403


def test_query_answers_a_select_over_the_index(home, tmp_path):
    client = _indexed_client(tmp_path)
    body = client.post("/api/index/query",
                       json={"sql": "SELECT count(*) AS n FROM dirs"},
                       headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert body["columns"] == ["n"]
    assert body["rows"] == [[1]]
    assert body["truncated"] is False


def test_query_rejects_a_mutation_with_a_400(home, tmp_path):
    resp = _indexed_client(tmp_path).post(
        "/api/index/query", json={"sql": "DELETE FROM files"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "read-only" in resp.json()["error"]


def test_query_rejects_a_missing_or_non_string_sql(home, tmp_path):
    client = _indexed_client(tmp_path)
    for body in ({}, {"sql": 5}, {"sql": "   "}):
        resp = client.post("/api/index/query", json=body,
                           headers={"X-Fused": "1"})
        assert resp.status_code == 400


def test_query_enforces_the_row_cap_server_side(home, tmp_path):
    """The client's `limit` is a request, not an instruction."""
    client = _indexed_client(tmp_path)
    body = client.post("/api/index/query",
                       json={"sql": "SELECT * FROM range(50) t(i)",
                             "limit": 10 ** 9},
                       headers={"X-Fused": "1"}).json()
    assert len(body["rows"]) == 50  # answered, and under the server cap
    body = client.post("/api/index/query",
                       json={"sql": "SELECT * FROM range(50) t(i)",
                             "limit": 3},
                       headers={"X-Fused": "1"}).json()
    assert len(body["rows"]) == 3
    assert body["truncated"] is True


def test_a_duckdb_runtime_error_is_a_400_not_a_500(home, tmp_path):
    resp = _indexed_client(tmp_path).post(
        "/api/index/query", json={"sql": "SELECT nope FROM dirs"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "nope" in resp.json()["error"]


# -- natural language ----------------------------------------------------------

def _fake_relay(answer, seen=None):
    """Stands in for server.ai._ai_relay: one non-streaming completion."""
    from fastapi.responses import JSONResponse

    async def relay(body, session=None):
        if seen is not None:
            seen.append(body)
        return JSONResponse({"ok": True, "result": {"text": answer,
                                                    "model": "m", "usage": {}}})

    return relay


def test_ask_runs_the_sql_the_model_returned_and_echoes_it(home, tmp_path,
                                                           monkeypatch):
    seen = []
    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("```sql\nSELECT count(*) AS n FROM dirs;\n```",
                                    seen))
    body = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "how many folders"},
        headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert body["sql"] == "SELECT count(*) AS n FROM dirs;"
    assert body["rows"] == [[1]]
    # The schemas have to reach the model, or it cannot write a valid statement.
    assert "files" in seen[0]["system_prompt"]
    assert "mtime_ns" in seen[0]["system_prompt"]
    assert seen[0]["stream"] is False


def test_ask_does_not_execute_a_mutation_the_model_wrote(home, tmp_path,
                                                        monkeypatch):
    """The guard is the boundary, not the prompt: a model that answers with a
    DELETE is refused by the same gate a user's DELETE hits."""
    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("DELETE FROM files"))
    resp = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "delete everything"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "read-only" in resp.json()["error"]
    # The SQL is echoed even when refused, so the user can see what was tried.
    assert resp.json()["sql"] == "DELETE FROM files"


def test_ask_passes_an_ai_failure_through_unchanged(home, tmp_path, monkeypatch):
    from fastapi.responses import JSONResponse

    async def broken(_body, session=None):
        return JSONResponse({"ok": False, "error": {"type": "ai_unavailable",
                                                    "message": "no claude"}},
                            status_code=502)

    monkeypatch.setattr(index_router._server_ai, "_ai_relay", broken)
    resp = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "anything"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "ai_unavailable"


def test_ask_runs_the_guarded_query_off_the_event_loop(home, tmp_path, monkeypatch):
    """`ask` is an async handler, so anything it calls directly runs ON the
    event loop — and the guarded query is duckdb plus disk, bounded only by
    TIMEOUT_S (10s). Blocking there freezes every other request in the server,
    including the scan-status polling the same panel is doing. `query` next
    door is safe only because it is a plain `def` handler, which FastAPI runs
    in a threadpool; this one has to ask for that explicitly."""
    import asyncio

    seen = {}
    real = index_router.run_guarded

    def spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(*a, **kw)

    monkeypatch.setattr(index_router, "run_guarded", spy)
    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("SELECT count(*) AS n FROM dirs"))
    body = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "how many folders"},
        headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert seen["on_loop"] is False


def test_ask_rejects_an_empty_prompt(home, tmp_path):
    resp = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "  "}, headers={"X-Fused": "1"})
    assert resp.status_code == 400


@pytest.mark.parametrize("answer,expected", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("```\nSELECT 1\n```", "SELECT 1"),
    ("SELECT 1", "SELECT 1"),
    ("Here you go:\n```sql\nSELECT 1\n```\nHope that helps.", "SELECT 1"),
])
def test_the_models_fencing_is_stripped(answer, expected):
    assert index_router._sql_from_answer(answer) == expected


def _write_dirs_index(cfg, dirs):
    """A minimal real index whose dirs.parquet holds {dir: mtime_ns}.

    Keys go through `canonical_root`: `freshness.indexed_mtime_ns` (and
    `enclosing_root`) look a directory up by their OWN `norm(abspath(...))`
    of it, so a row filed under a raw `str(Path)` literal — native-separator
    on Windows — would silently miss every such lookup."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from fused_render.index.runner import canonical_root
    from fused_render.index.store import Sink, compact
    dirs = {canonical_root(d): mtime_ns for d, mtime_ns in dirs.items()}
    shards = os.path.join(cfg.dir, "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    for d, mtime_ns in dirs.items():
        sink.add(d, "s", ("sig", [], 0, mtime_ns, 0))
    sink.close()
    compact(cfg, next(iter(dirs)), shards, pa, pq)


# -- cancellation: an abandoned request gets a quiet 499 -----------------------
#
# Same end-to-end shape /api/index/rank already covers (test_index_search.py's
# test_rank_route_answers_a_disconnected_client_with_a_quiet_499): the worker
# function is monkeypatched to a controllable stand-in — sleeping briefly on
# its OWN thread, never the event loop — that then consults the very token
# the route handed it, exactly as the real query.py/guarded_query.py
# functions do. A real (tiny, near-instant) index would make the outcome a
# coin flip on scheduling order against `_watch_disconnect`'s poll.

class _DisconnectedRequest:
    """`app.state` is only there for `api_index_ask`'s `ai_session` lookup —
    the other three routes never touch it."""

    class app:
        class state:
            pass

    async def is_disconnected(self):
        return True


def test_stats_route_answers_a_disconnected_client_with_a_quiet_499(
    home, tmp_path, monkeypatch, caplog,
):
    from fused_render.index.cancel import Cancelled

    def slow_cancellable_stats(cfg, root="", breakdown=False, token=None):
        import time as _time

        _time.sleep(0.05)
        if token is not None and token.cancelled:
            raise Cancelled()
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}

    monkeypatch.setattr(index_router, "index_stats", slow_cancellable_stats)
    with caplog.at_level(logging.DEBUG):
        resp = asyncio.run(
            index_router.api_index_stats(request=_DisconnectedRequest()))
    assert resp.status_code == 499
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("abandoned by the client" in r.message for r in caplog.records)


def test_search_route_answers_a_disconnected_client_with_a_quiet_499(
    home, tmp_path, monkeypatch, caplog,
):
    from fused_render.index.cancel import Cancelled

    def slow_cancellable_search(cfg, root, q="", limit=None, token=None):
        import time as _time

        _time.sleep(0.05)
        if token is not None and token.cancelled:
            raise Cancelled()
        return {"covered": False, "fresh": False, "updated": None, "age_s": None,
                "root": root, "entries": [], "truncated": False, "total": 0,
                "scanned_partitions": 0, "of_partitions": 0}

    monkeypatch.setattr(index_router, "index_search", slow_cancellable_search)
    with caplog.at_level(logging.DEBUG):
        resp = asyncio.run(
            index_router.api_index_search(
                request=_DisconnectedRequest(), root=str(tmp_path)))
    assert resp.status_code == 499
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("abandoned by the client" in r.message for r in caplog.records)


def test_query_route_answers_a_disconnected_client_with_a_quiet_499(
    home, tmp_path, monkeypatch, caplog,
):
    from fused_render.index.cancel import Cancelled

    def slow_cancellable_guarded(cfg, sql, limit, token=None):
        import time as _time

        _time.sleep(0.05)
        if token is not None and token.cancelled:
            raise Cancelled()
        return {"columns": [], "rows": [], "truncated": False}

    monkeypatch.setattr(index_router, "_guarded", slow_cancellable_guarded)
    with caplog.at_level(logging.DEBUG):
        resp = asyncio.run(
            index_router.api_index_query(
                request=_DisconnectedRequest(),
                body={"sql": "SELECT 1"}, x_fused="1"))
    assert resp.status_code == 499
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("abandoned by the client" in r.message for r in caplog.records)


def test_ask_route_answers_a_disconnected_client_with_a_quiet_499(
    home, tmp_path, monkeypatch, caplog,
):
    from fused_render.index.cancel import Cancelled

    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("SELECT 1"))

    def slow_cancellable_guarded(cfg, sql, limit, token=None):
        import time as _time

        _time.sleep(0.05)
        if token is not None and token.cancelled:
            raise Cancelled()
        return {"columns": [], "rows": [], "truncated": False}

    monkeypatch.setattr(index_router, "_guarded", slow_cancellable_guarded)
    with caplog.at_level(logging.DEBUG):
        resp = asyncio.run(
            index_router.api_index_ask(
                request=_DisconnectedRequest(),
                body={"prompt": "anything"}, x_fused="1"))
    assert resp.status_code == 499
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("abandoned by the client" in r.message for r in caplog.records)
