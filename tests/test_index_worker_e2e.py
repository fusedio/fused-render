"""End-to-end coverage for the real scan path: a registered kind, a real
`python -m fused_render.index.worker` subprocess (not a mocked `Popen`, not a
direct `run_scan` call), an incremental rescan of the same root, and the
result read back through the HTTP `/api/index/rank` route.

Review findings 1, 2 and 3 all shipped past 240 unit-shaped tests because
none of them exercised this real path: finding 1 only manifests when
`_kind_obj` runs in a fresh interpreter that never imported
`server/routers/index.py` (every unit test imports the router first, so
"apps" was always already registered by the time `scan.py` ran in-process);
finding 2 only manifests across two separate `compact()` calls where the
SAME directory is scanned once and then reused ("u") on the next pass, which
a real incremental rescan of an unchanged app folder actually produces; and
finding 3 is only observable by reading `covered` off the wire, not off the
in-process return value most `search_apps_ranked` tests assert against
directly. This file is the test the review said would have caught all three.
"""
import os
import time

from fastapi.testclient import TestClient

from fused_render.index import runner
from fused_render.index.config import IndexConfig, index_dir
from fused_render.server import create_app

FUSED_APP_META = (
    '<!doctype html><html><head>'
    '<meta name="fused-app" content="1">'
    '<title>Solo</title></head><body></body></html>'
)


def _make_app_folder(root, name):
    """A real on-disk app: a folder whose `index.html` carries the
    `<meta name="fused-app">` marker `app_listing.app_entry` looks for —
    the same shape `apps_kind.extract` requires to produce a row at all."""
    folder = os.path.join(root, name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index.html"), "w") as f:
        f.write(FUSED_APP_META)
    return folder


def _wait_for_run(cfg, run_id, timeout=30):
    """Poll the real run's event log until `run_end`, the same way the
    status endpoint's client does — no direct access to the worker's
    internals, since the whole point of this test is to go through the
    process boundary, not around it."""
    run_dir = os.path.join(cfg.runs_dir, run_id)
    deadline = time.time() + timeout
    since = 0
    while time.time() < deadline:
        ended, since = runner.has_ended(run_dir, since)
        if ended:
            events, _, _ = runner.read_events(run_dir)
            end = next(e for e in events if e.get("type") == "run_end")
            return end
        time.sleep(0.1)
    raise TimeoutError(f"worker for run {run_id} did not finish in {timeout}s")


def test_a_real_worker_scan_survives_an_incremental_rescan_and_is_rankable(
    tmp_path, monkeypatch
):
    """Registers the built-in "apps" kind (already registered by importing
    this test's own `create_app`/`apps_kind` dependencies, exactly as a real
    server does), runs a REAL scan of a workspace folder through the actual
    `python -m fused_render.index.worker` subprocess entrypoint, rescans the
    same workspace incrementally (the app folder is reused, not rewalked),
    and reads the surviving row back through `/api/index/rank`."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))

    workspace = tmp_path / "Fused"
    workspace.mkdir()
    _make_app_folder(str(workspace), "solo")

    cfg = IndexConfig(dir=index_dir("apps"), kind="apps")

    # -- full scan, through the real detached worker process -----------------
    started = runner.start(cfg, str(workspace), full=True)
    end = _wait_for_run(cfg, started["run_id"])
    assert end.get("error") is None, (
        f"worker run failed: {end.get('error')!r} — this is exactly how "
        f"finding 1's KeyError('no IndexKind registered as \\'apps\\'') "
        f"showed up: a run that ends immediately, with no rows, and no "
        f"traceback anywhere but the worker's own log")

    client = TestClient(create_app(start_dir=str(tmp_path)))
    resp = client.get("/api/index/rank", params={"kind": "apps", "q": "solo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["covered"] is True, "finding 3: covered must be on the wire"
    rels = [h["rel"] for h in body["hits"]]
    assert any(r.endswith("/solo") for r in rels), (
        f"expected the scanned app's folder among the hits, got {rels!r}")

    # -- incremental rescan of the SAME root: the app folder is unchanged
    #    and gets reused ("u"), not rewalked ("s") ------------------------
    started2 = runner.start(cfg, str(workspace), full=False)
    end2 = _wait_for_run(cfg, started2["run_id"])
    assert end2.get("error") is None

    resp2 = client.get("/api/index/rank", params={"kind": "apps", "q": "solo"})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["covered"] is True
    rels2 = [h["rel"] for h in body2["hits"]]
    assert any(r.endswith("/solo") for r in rels2), (
        "finding 2: the app's row must survive an incremental rescan that "
        f"reuses its own folder instead of rewalking it; got {rels2!r}")
