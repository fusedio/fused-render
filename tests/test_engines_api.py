"""The two status-bar routes on the engines router (D591):
`GET /api/engines/running` and `POST /api/engines/{id}/stop`.

Its own file because no existing engines test carries a TestClient — the
router's other endpoints are exercised through the template/app paths in
test_engine_app.py and test_background_apps.py, neither of which is about the
control plane these two belong to. The fixtures mirror
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


def test_running_reports_every_live_kind_with_its_labelling_fields(client, monkeypatch):
    """One row per live child, whatever its kind, carrying the fields the
    status bar needs to LABEL it: `folder` for a background app, `module` for a
    warm app worker, and the id itself for a template engine (those are already
    readable — `map`, `geotiff`)."""
    children = {
        "map": _child("map", kind="template"),
        "app:widget": _child("app:widget", kind="app", module="widget.main"),
        "bg:demo": _child("bg:demo", kind="background", folder="/w/demo"),
    }
    monkeypatch.setattr(engine_host, "_children", children)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    engines = client.get("/api/engines/running").json()["engines"]

    by_id = {e["engine_id"]: e for e in engines}
    assert set(by_id) == {"map", "app:widget", "bg:demo"}
    assert by_id["map"]["kind"] == "template"
    assert by_id["app:widget"]["module"] == "widget.main"
    assert by_id["bg:demo"]["folder"] == "/w/demo"
    # The kind is reported so three similarly-named rows stay distinguishable,
    # and the pid so a user can find the process outside the app.
    assert by_id["bg:demo"]["kind"] == "background"
    assert by_id["map"]["pid"] == 4242


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
