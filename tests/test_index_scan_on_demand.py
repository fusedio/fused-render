"""Searching a folder the index has never covered asks for it to be scanned.

The in-folder search box used to answer an uncovered folder with a live
streamed walk. That walk is gone for everything but the folders no scan can
ever reach (mounts, packages), so "uncovered" has to become "covered soon"
instead: the box asks the server to scan the folder, and polls the ranked
route while it runs.

What these tests police is the REFUSAL path, because that is where an
on-demand scan turns into a retry loop: the route never fails, it says what it
did, and every "no" is a durable no — a mount is refused structurally, and a
folder scanned recently is debounced by exactly the scheduler's own floor
rather than a second one invented here.
"""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.index import runner
from fused_render.index.config import load_config
from fused_render.server import create_app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "mounts").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


@pytest.fixture()
def client(home, tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _started(monkeypatch):
    """Record runner.start calls instead of spawning a detached worker."""
    calls = []

    def fake_start(cfg, root, full=False):
        calls.append(root)
        return {"run_id": "run-1", "root": root}

    monkeypatch.setattr(runner, "start", fake_start)
    return calls


def _ask(client, path):
    return client.post("/api/index/scan-folder", json={"path": str(path)},
                       headers={"X-Fused": "1"}).json()


def test_an_uncovered_folder_is_scanned_on_demand(client, tmp_path, monkeypatch):
    calls = _started(monkeypatch)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    body = _ask(client, folder)
    assert body["ok"] is True and body["started"] is True
    assert body["why"] == "started" and body["run_id"] == "run-1"
    # The FOLDER, not some enclosing root: an uncovered folder is uncovered
    # precisely because no configured root covers it.
    assert calls == [runner.canonical_root(str(folder))]


def test_a_recent_scan_of_the_folder_debounces_the_next_ask(client, tmp_path,
                                                            monkeypatch):
    """The scheduler's own floor, reused. Without it, a folder that stays
    uncovered after a scan (ignored by the rules, say) would be rescanned on
    every keystroke for ever."""
    calls = _started(monkeypatch)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    import time
    monkeypatch.setattr(runner, "last_scan", lambda cfg, root: time.time() - 5)
    body = _ask(client, folder)
    assert body["started"] is False and body["why"] == "debounced"
    assert calls == []


def test_a_mount_backed_folder_is_refused_and_says_so(client, home, tmp_path):
    """runner.start already refuses; what matters here is that the refusal
    comes back as a durable "no" rather than a 500 the client would retry."""
    folder = home / "mounts" / "s3"
    os.makedirs(folder, exist_ok=True)
    body = _ask(client, folder)
    assert body["ok"] is True and body["started"] is False
    assert body["why"] == "refused" and body["error"]


def test_a_folder_that_is_gone_is_refused_not_an_error(client, tmp_path):
    body = _ask(client, tmp_path / "no-such-folder")
    assert body["ok"] is True and body["started"] is False
    assert body["why"] == "refused"


def test_the_route_is_guarded(client, tmp_path):
    resp = client.post("/api/index/scan-folder", json={"path": str(tmp_path)})
    assert resp.status_code == 403


def test_a_missing_path_is_a_bad_request(client):
    resp = client.post("/api/index/scan-folder", json={},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 400


def test_joining_a_live_run_is_not_a_second_scan(client, tmp_path, monkeypatch):
    """runner.start joins a run already scanning the same root; the client is
    told so, so it polls instead of asking again."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    monkeypatch.setattr(runner, "start", lambda cfg, root, full=False: {
        "run_id": "live", "root": root, "already_running": True})
    body = _ask(client, folder)
    assert body["started"] is True and body["why"] == "joined"


def test_the_scan_floor_is_the_scheduler_s_own(client, tmp_path, monkeypatch):
    """A second debounce constant here would drift from the one the startup
    scheduler uses; there is exactly one."""
    from fused_render.server.routers import index as index_routes

    calls = _started(monkeypatch)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    import time
    monkeypatch.setattr(
        runner, "last_scan",
        lambda cfg, root: time.time() - index_routes.SCAN_DEBOUNCE_S - 1)
    assert _ask(client, folder)["started"] is True
    assert calls and load_config() is not None
