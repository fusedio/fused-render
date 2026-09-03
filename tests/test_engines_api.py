"""The two status-bar routes on the engines router (D591):
`GET /api/engines/running` and `POST /api/engines/{id}/stop`.

Its own file because no existing engines test carries a TestClient — the
router's other endpoints are exercised through the template/daemon paths in
test_daemon_lifetime.py and test_background_apps.py, neither of which is about
the control plane these two belong to. The fixtures mirror
test_background_apps.py's, which is the closest sibling.
"""
import sys

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app, engine_host

HDRS = {"X-Fused": "1"}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _child(engine_id, **over):
    """A Child with no real process behind it — `_alive` is monkeypatched in
    every test below, so nothing here spawns or polls."""
    fields = dict(
        engine_id=engine_id, python=sys.executable, daemon="/tmp/daemon.py",
        cache="c", version="v1", pid=4242,
    )
    fields.update(over)
    return engine_host.Child(**fields)


def test_running_reports_every_live_child_with_its_labelling_fields(client, monkeypatch):
    """One row per live child, whatever brought it up, carrying the fields the
    status bar needs to LABEL it: `folder` for a background app, `module` for a
    `main =` daemon (empty for a `daemon =` one), and the id itself for a
    template engine (those are already readable — `map`, `geotiff`)."""
    children = {
        "map": _child("map"),
        "bg:widget": _child("bg:widget", folder="/w/widget", module="widget.main",
                           idle_timeout_s=900.0),
        "bg:demo": _child("bg:demo", folder="/w/demo"),
    }
    monkeypatch.setattr(engine_host, "_children", children)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    engines = client.get("/api/engines/running").json()["engines"]

    by_id = {e["engine_id"]: e for e in engines}
    assert set(by_id) == {"map", "bg:widget", "bg:demo"}
    assert by_id["map"]["folder"] == ""
    assert by_id["bg:widget"]["module"] == "widget.main"
    assert by_id["bg:demo"]["folder"] == "/w/demo"
    # The pid is reported so a user can find the process outside the app.
    assert by_id["map"]["pid"] == 4242


def test_running_reports_the_lifetime_fields_the_activity_panel_reads(client, monkeypatch):
    """Uptime, the manifest's idle policy, how long the child has been idle and
    whether a call is in flight — what the Activity panel turns into "up 5m ·
    retires in 10m if idle". A resident child reports `idle_timeout_s == 0`,
    which is what the panel renders as "no idle timeout"."""
    now = engine_host.time.monotonic()
    resident = _child("map")
    resident.started_at = now - 300.0
    warm = _child("bg:widget", folder="/w/widget", module="widget.main",
                  idle_timeout_s=900.0)
    warm.started_at = now - 600.0
    warm.last_used = now - 60.0
    monkeypatch.setattr(engine_host, "_children", {"map": resident, "bg:widget": warm})
    monkeypatch.setattr(engine_host, "_busy", {"bg:widget": 1})
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    by_id = {e["engine_id"]: e
             for e in client.get("/api/engines/running").json()["engines"]}

    assert by_id["map"]["idle_timeout_s"] == 0
    assert by_id["map"]["busy"] is False
    assert by_id["map"]["uptime_s"] == pytest.approx(300.0, abs=5)
    assert by_id["bg:widget"]["idle_timeout_s"] == 900.0
    assert by_id["bg:widget"]["uptime_s"] == pytest.approx(600.0, abs=5)
    assert by_id["bg:widget"]["idle_for_s"] == pytest.approx(60.0, abs=5)
    # A call in flight is why idle-retire is skipping this child, so the panel
    # is told rather than left drawing a countdown that is not running.
    assert by_id["bg:widget"]["busy"] is True


def test_running_reports_busy_for_a_child_reap_is_skipping_on_inflight_alone(
        client, monkeypatch):
    """A call that outran the 60s proxy budget gets a 504, whose `finally`
    calls `mark_idle` (routers/engines.py): `_busy` drops to 0 and `last_used`
    is stamped as if the call had ended. But `main()` keeps running in the
    worker, and `reap_idle_children` knows that — it skips the child whenever
    `_inflight` (a ping to the worker itself) is still nonzero, past the local
    `_busy` gate. The wire's `busy` has to agree with the thing that is
    actually keeping this child alive, or the panel reads "retiring now" for
    the one state its detail line exists to explain, for as long as the call
    keeps running past the timeout."""
    now = engine_host.time.monotonic()
    worker = _child("bg:slow", folder="/w/slow", module="slow.main", idle_timeout_s=60.0)
    worker.started_at = now - 900.0
    worker.last_used = now - 61.0  # idle_for_s > idle_timeout_s: a reap candidate
    monkeypatch.setattr(engine_host, "_children", {"bg:slow": worker})
    monkeypatch.setattr(engine_host, "_busy", {})  # cleared by the 504's mark_idle
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)
    monkeypatch.setattr(engine_host, "_inflight", lambda c: 1)  # main() still running

    by_id = {e["engine_id"]: e
             for e in client.get("/api/engines/running").json()["engines"]}

    assert by_id["bg:slow"]["busy"] is True


def test_running_omits_a_child_whose_process_has_exited(client, monkeypatch):
    """`_children` holds the pointer until something reaps it, so liveness is a
    `Popen.poll()` — a dead child must not be offered a Stop button."""
    children = {"alive": _child("alive"), "dead": _child("dead")}
    monkeypatch.setattr(engine_host, "_children", children)
    monkeypatch.setattr(engine_host, "_alive", lambda c: c.engine_id == "alive")

    engines = client.get("/api/engines/running").json()["engines"]

    assert [e["engine_id"] for e in engines] == ["alive"]


def test_running_is_an_unguarded_read(client, monkeypatch):
    """No `X-Fused`, matching this module's stated posture for reads and
    `GET /api/apps/background/running`."""
    monkeypatch.setattr(engine_host, "_children", {})
    assert client.get("/api/engines/running").status_code == 200


def test_stop_removes_the_child_so_running_no_longer_lists_it(client, monkeypatch):
    """The endpoint's actual job, asserted through the SAME snapshot the UI
    reads rather than by inspecting internals: after a stop, the engine is gone
    from `/running`."""
    killed = []
    children = {"map": _child("map"), "keep": _child("keep")}
    monkeypatch.setattr(engine_host, "_children", children)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)
    # Only the teardown of the OS process is faked; `stop`'s own bookkeeping
    # (popping `_children`/`_reinit`/`_busy`) is the thing under test.
    monkeypatch.setattr(engine_host, "_terminate", lambda c: killed.append(c.engine_id))

    resp = client.post("/api/engines/map/stop", headers=HDRS)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert killed == ["map"]
    engines = client.get("/api/engines/running").json()["engines"]
    assert [e["engine_id"] for e in engines] == ["keep"]


def test_stop_requires_the_fused_header(client, monkeypatch):
    """Guarded like its `ensure`/`reinit`/`forget` siblings — it reaches the
    child's executing side."""
    children = {"map": _child("map")}
    monkeypatch.setattr(engine_host, "_children", children)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)
    monkeypatch.setattr(engine_host, "_terminate", lambda c: None)

    assert client.post("/api/engines/map/stop").status_code != 200
    # ...and the child is untouched by the refused call.
    assert "map" in children


def test_stop_on_an_unknown_id_is_a_no_op_not_an_error(client, monkeypatch):
    """`engine_host.stop` pops with a default, so a stale row the user clicks
    after the engine already exited must not 500."""
    monkeypatch.setattr(engine_host, "_children", {})
    resp = client.post("/api/engines/nope/stop", headers=HDRS)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
