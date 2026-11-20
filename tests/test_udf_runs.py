"""fused.workbench.udf()'s server half (SPEC §54): the result store, the run
service (references, run keys, the saved copy, the gates, joined runs), the
/api/udf/ws socket and the results route, and the worker's result shaping.

The warm worker itself needs the fused SDK and a Fused login, so the runner
is replaced by a fake here (`fake_runner`); `udf/shaping.py` is the worker's
pure half and is tested directly."""
import asyncio
import base64
import json
import os
import struct
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
from fused_render.server import create_app
from fused_render.shell import prefs as prefs_mod
from fused_render.shell import storage
from fused_render.udf import runner, service, shaping, store

ENV = "unstable"
CANVAS_TOML = """\
type = "canvas"
version = 2
name = "demo"

[canvas]

[[canvas.nodes]]
udfName = "udf_1"
title = "Load data"
x = 0.0
y = 0.0
"""
UDF_SOURCE = "@fused.udf\ndef udf():\n    return 1\n"


def _jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _sign_in(tmp_path, sub="user-a"):
    (tmp_path / "creds.json").write_text(json.dumps({"access_token": _jwt(sub)}), encoding="utf-8")


@pytest.fixture
def base(tmp_path, monkeypatch):
    root = tmp_path / "canvases"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(root))
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "creds.json"))
    data = storage.read_json(prefs_mod._path())
    data = data if isinstance(data, dict) else {}
    data["canvases_enabled"] = True
    os.makedirs(os.path.dirname(prefs_mod._path()), exist_ok=True)
    with open(prefs_mod._path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    canvases_mod._cli_cache_clear()
    service._FLIGHTS.clear()
    yield root
    canvases_mod._cli_cache_clear()


@pytest.fixture
def clone(base):
    folder = base / ENV / "user" / "demo"
    folder.mkdir(parents=True)
    (folder / "canvas.toml").write_text(CANVAS_TOML, encoding="utf-8")
    (folder / "udf_1.py").write_text(UDF_SOURCE, encoding="utf-8")
    return folder


@pytest.fixture
def signed_in(tmp_path, base):
    _sign_in(tmp_path)


class FakeRunner:
    """Stands in for runner.run: records requests, writes `body` to the
    request's `out`, answers with `final` (a dict merged into the done event,
    or a callable(request) -> final event)."""

    def __init__(self):
        self.requests = []
        self.cancels = []
        self.body = b"<p>hi</p>"
        self.final = {"ok": True, "content_type": "text/html", "fused_cached": False, "seconds": 0.4}
        self.kind = "none"
        self.gate: asyncio.Event | None = None
        self.on_run = None

    async def run(self, env, request, on_event=None):
        self.requests.append((env, request))
        if request.get("op") == "schema":
            return {"id": request["id"], "event": "schema", "ok": True, "schema": {"parameters": []}}
        if self.kind != "none" and not request.get("confirmed"):
            return {"id": request["id"], "event": "needs_confirmation", "kind": self.kind}
        if on_event is not None:
            on_event({"id": request["id"], "event": "started", "kind": self.kind})
        if self.gate is not None:
            await self.gate.wait()
        if self.on_run is not None:
            self.on_run(request)
        final = self.final(request) if callable(self.final) else self.final
        if final.get("ok"):
            with open(request["out"], "wb") as fh:
                fh.write(self.body)
            final = dict(final, size=len(self.body))
        return dict(final, id=request["id"], event="done")

    def cancel(self, env, rid):
        self.cancels.append((env, rid))


@pytest.fixture
def fake_runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(runner, "run", fake.run)
    monkeypatch.setattr(runner, "cancel", fake.cancel)
    return fake


def run_frames(msg, cancelled=None):
    frames = []

    async def send(frame):
        frames.append(frame)

    async def go():
        await service.run(dict({"id": "r1"}, **msg), send, cancelled)

    asyncio.run(go())
    return frames


# ------------------------------------------------------------------ store


def test_store_keeps_a_body_under_its_key_and_reads_it_back(base, tmp_path):
    token, path = store.new_body_path()
    with open(path, "wb") as fh:
        fh.write(b"abc")
    key = "a" * 64
    kept = store.keep(path, {"content_type": "text/plain", "size": 3}, key)
    assert kept["token"] == key and not os.path.exists(path)
    got = store.get(key)
    assert got["content_type"] == "text/plain" and got["token"] == key
    body, meta = store.open_result(key)
    assert open(body, "rb").read() == b"abc" and meta["size"] == 3


def test_store_leaves_an_oversize_body_temporary(base, monkeypatch):
    monkeypatch.setattr(store, "MAX_ENTRY_BYTES", 2)
    token, path = store.new_body_path()
    with open(path, "wb") as fh:
        fh.write(b"abc")
    kept = store.keep(path, {"size": 3}, "b" * 64)
    assert kept["token"] == token and token.startswith("t")
    assert store.get("b" * 64) is None
    assert store.open_result(token) is not None


def test_store_without_a_key_is_temporary(base):
    token, path = store.new_body_path()
    open(path, "wb").close()
    assert store.keep(path, {}, None)["token"] == token


def test_store_drops_an_entry_past_its_age(base, monkeypatch):
    token, path = store.new_body_path()
    open(path, "wb").close()
    key = "c" * 64
    store.keep(path, {}, key)
    later = time.time() + store.MAX_AGE_S + 10
    monkeypatch.setattr(store.time, "time", lambda: later)
    assert store.get(key) is None
    assert store.open_result(key) is None


def test_store_evicts_the_least_recently_read(base, monkeypatch):
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 10)
    keys = ["d" * 64, "e" * 64, "f" * 64]
    for i, key in enumerate(keys):
        token, path = store.new_body_path()
        with open(path, "wb") as fh:
            fh.write(b"12345")
        store.keep(path, {}, key)
        body = os.path.join(store._results_dir(), key + ".body")
        os.utime(body, (1000 + i, 1000 + i))
        if i == 1:
            # Read the first so it is the most recently used.
            os.utime(os.path.join(store._results_dir(), keys[0] + ".body"), (5000, 5000))
    assert store.get(keys[0]) is not None
    assert store.get(keys[1]) is None
    assert store.get(keys[2]) is not None


def test_store_refuses_tokens_that_are_not_its_own(base):
    for token in ("../etc", "x" * 64, "t123", "", "T" + "0" * 32):
        assert store.open_result(token) is None
    assert store.get("t" + "0" * 32) is None


# --------------------------------------------------------- references, keys


def test_a_node_file_runs_in_its_canvas_and_environment(clone):
    target = service.resolve({"file": str(clone / "udf_1.py")})
    assert (target.env, target.collection, target.load_as) == (ENV, "demo", str(clone / "udf_1.py"))
    assert target.file_sha


def test_any_other_py_needs_an_environment(base, tmp_path):
    path = tmp_path / "loose.py"
    path.write_text(UDF_SOURCE, encoding="utf-8")
    with pytest.raises(service.RunError) as err:
        service.resolve({"file": str(path)})
    assert err.value.code == "bad_ref"
    target = service.resolve({"file": str(path), "env": "prod"})
    assert (target.env, target.collection) == ("prod", None)


def test_a_bad_environment_is_refused(base, tmp_path):
    path = tmp_path / "loose.py"
    path.write_text(UDF_SOURCE, encoding="utf-8")
    with pytest.raises(service.RunError) as err:
        service.resolve({"file": str(path), "env": "nowhere"})
    assert err.value.code == "bad_env"


@pytest.mark.parametrize("ref", [None, "x", {"file": "rel.py"}, {"file": "/a/b.txt"},
                                 {"canvas": "c"}, {"canvas": "c", "name": "a/b"},
                                 {"canvas": "c", "name": "n.py"}])
def test_bad_references(base, ref):
    with pytest.raises(service.RunError) as err:
        service.resolve(ref)
    assert err.value.code == "bad_ref"


def test_a_saved_udf_by_name(base):
    target = service.resolve({"canvas": "demo", "name": "udf_1", "env": "prod"})
    assert (target.env, target.collection, target.load_as) == ("prod", "demo", "udf_1")
    assert target.file_sha is None


def test_the_run_key_follows_content_params_format_and_account(clone):
    path = clone / "udf_1.py"
    t1 = service.resolve({"file": str(path)})
    k = service.run_key(t1, "acct", {}, "auto")
    assert k == service.run_key(service.resolve({"file": str(path)}), "acct", {}, "auto")
    assert k != service.run_key(t1, "other", {}, "auto")
    assert k != service.run_key(t1, "acct", {"n": 1}, "auto")
    assert k != service.run_key(t1, "acct", {}, "json")
    path.write_text(UDF_SOURCE + "# edit\n", encoding="utf-8")
    assert k != service.run_key(service.resolve({"file": str(path)}), "acct", {}, "auto")


# ------------------------------------------------------------------ runs


def test_a_run_answers_and_is_saved_for_next_time(clone, signed_in, fake_runner):
    ref = {"file": str(clone / "udf_1.py")}
    frames = run_frames({"ref": ref})
    assert [f["type"] for f in frames] == ["started", "done"]
    done = frames[-1]["result"]
    assert done["source"] == "fresh" and done["content_type"] == "text/html" and done["size"] == 9
    env, request = fake_runner.requests[0]
    assert env == ENV and request["collection"] == "demo" and request["cache"] is True
    assert request["format"] == "auto" and request["confirmed"] is False

    frames = run_frames({"ref": ref})
    assert [f["type"] for f in frames] == ["saved", "started", "done"]
    assert frames[0]["result"]["source"] == "saved"
    assert frames[0]["result"]["url"].startswith(f"/api/udf/results/{frames[0]['result']['token']}?v=")


def test_prefer_saved_ends_at_the_saved_copy(clone, signed_in, fake_runner):
    ref = {"file": str(clone / "udf_1.py")}
    run_frames({"ref": ref})
    frames = run_frames({"ref": ref, "cache": "prefer-saved"})
    assert [f["type"] for f in frames] == ["saved", "done"]
    assert len(fake_runner.requests) == 1


def test_fresh_skips_the_saved_copy_and_fuseds_cache(clone, signed_in, fake_runner):
    ref = {"file": str(clone / "udf_1.py")}
    run_frames({"ref": ref})
    frames = run_frames({"ref": ref, "cache": "fresh"})
    assert [f["type"] for f in frames] == ["started", "done"]
    assert fake_runner.requests[-1][1]["cache"] is False


def test_an_edit_is_a_different_saved_copy(clone, signed_in, fake_runner):
    ref = {"file": str(clone / "udf_1.py")}
    run_frames({"ref": ref})
    (clone / "udf_1.py").write_text(UDF_SOURCE + "# edit\n", encoding="utf-8")
    assert run_frames({"ref": ref})[0]["type"] == "started"


def test_another_account_does_not_see_the_saved_copy(clone, tmp_path, fake_runner):
    ref = {"file": str(clone / "udf_1.py")}
    _sign_in(tmp_path, "user-a")
    run_frames({"ref": ref})
    _sign_in(tmp_path, "user-b")
    assert run_frames({"ref": ref})[0]["type"] == "started"


def test_a_file_changed_during_the_run_is_not_saved_under_the_new_key(clone, signed_in, fake_runner):
    ref = {"file": str(clone / "udf_1.py")}
    fake_runner.on_run = lambda req: (clone / "udf_1.py").write_text("# changed\n", encoding="utf-8")
    frames = run_frames({"ref": ref})
    assert frames[-1]["type"] == "done"
    assert frames[-1]["result"]["token"].startswith("t")


def test_a_batch_or_map_udf_asks_first_and_runs_once_confirmed(clone, signed_in, fake_runner):
    fake_runner.kind = "map"
    ref = {"file": str(clone / "udf_1.py")}
    frames = run_frames({"ref": ref})
    assert frames == [{"id": "r1", "type": "error", "code": "needs_confirmation",
                       "error": "Auto-execution is disabled for map operations",
                       "execution_type": "map", "title": "Confirm map operation",
                       "reason": "Auto-execution is disabled for map operations"}]
    frames = run_frames({"ref": ref, "confirmed": True})
    assert frames[-1]["type"] == "done"


def test_a_udf_error_carries_its_line_and_output(clone, signed_in, fake_runner):
    fake_runner.final = {"ok": False, "error": "boom", "line": 3, "stdout": "o", "stderr": "e"}
    frames = run_frames({"ref": {"file": str(clone / "udf_1.py")}})
    err = frames[-1]
    assert (err["code"], err["error"], err["line"], err["stdout"], err["stderr"]) == \
        ("udf_error", "boom", 3, "o", "e")
    assert os.listdir(store._tmp_dir()) == []
    assert not os.path.isdir(store._results_dir()) or os.listdir(store._results_dir()) == []


def test_a_result_past_the_limit_is_refused(clone, signed_in, fake_runner, monkeypatch):
    monkeypatch.setattr(service, "MAX_BYTES", 4)
    frames = run_frames({"ref": {"file": str(clone / "udf_1.py")}})
    assert frames[-1]["code"] == "too_large" and frames[-1]["size"] == 9


def test_signed_out_runs_nothing(clone, fake_runner):
    frames = run_frames({"ref": {"file": str(clone / "udf_1.py")}})
    assert frames[-1]["code"] == "signed_out" and fake_runner.requests == []


@pytest.mark.parametrize("bad", [{"format": "csv"}, {"cache": "never"}, {"params": [1]}])
def test_bad_options_are_refused(clone, signed_in, fake_runner, bad):
    frames = run_frames(dict({"ref": {"file": str(clone / "udf_1.py")}}, **bad))
    assert frames[-1]["code"] == "bad_ref" and fake_runner.requests == []


def test_two_identical_runs_in_flight_share_one(clone, signed_in, fake_runner):
    fake_runner.gate = asyncio.Event()
    ref = {"file": str(clone / "udf_1.py")}
    frames_a, frames_b = [], []

    async def go():
        async def send_a(f):
            frames_a.append(f)

        async def send_b(f):
            frames_b.append(f)

        a = asyncio.ensure_future(service.run({"id": "a", "ref": ref}, send_a))
        await asyncio.sleep(0.05)
        b = asyncio.ensure_future(service.run({"id": "b", "ref": ref}, send_b))
        await asyncio.sleep(0.05)
        fake_runner.gate.set()
        await asyncio.gather(a, b)

    asyncio.run(go())
    assert len(fake_runner.requests) == 1
    assert frames_a[-1]["type"] == frames_b[-1]["type"] == "done"
    assert frames_a[-1]["result"]["token"] == frames_b[-1]["result"]["token"]


def test_cancel_stops_the_worker_run_for_the_last_waiter(clone, signed_in, fake_runner):
    fake_runner.gate = asyncio.Event()
    frames = []

    async def go():
        async def send(f):
            frames.append(f)

        stop = asyncio.Event()
        task = asyncio.ensure_future(service.run(
            {"id": "a", "ref": {"file": str(clone / "udf_1.py")}}, send, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        fake_runner.gate.set()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert frames[-1]["code"] == "cancelled"
    assert fake_runner.cancels and fake_runner.cancels[0][0] == ENV


def test_a_runner_that_cannot_start_is_unavailable(clone, signed_in, monkeypatch):
    async def refuse(env, request, on_event=None):
        raise runner.RunnerUnavailable("running a UDF needs the bundled fused SDK")

    monkeypatch.setattr(runner, "run", refuse)
    frames = run_frames({"ref": {"file": str(clone / "udf_1.py")}})
    assert frames[-1]["code"] == "unavailable"


def test_schema_answers_through_the_worker(clone, signed_in, fake_runner):
    frames = []

    async def send(f):
        frames.append(f)

    asyncio.run(service.schema({"id": "s", "ref": {"file": str(clone / "udf_1.py")}}, send))
    assert frames == [{"id": "s", "type": "schema", "schema": {"parameters": []}}]
    assert fake_runner.requests[0][1]["op"] == "schema"


# ------------------------------------------------------------ the routes

ORIGIN = "http://127.0.0.1:1777"


@pytest.fixture
def client(base, tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)), base_url=ORIGIN)


def test_the_socket_refuses_a_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1:1777/api/udf/ws",
                                      headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()


def test_a_run_over_the_socket_and_its_bytes(client, clone, signed_in, fake_runner):
    with client.websocket_connect("ws://127.0.0.1:1777/api/udf/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"op": "run", "id": "x1", "ref": {"file": str(clone / "udf_1.py")}}))
        frames = []
        while not frames or frames[-1]["type"] not in ("done", "error"):
            frames.append(json.loads(ws.receive_text()))
    done = frames[-1]
    assert done["id"] == "x1" and done["type"] == "done"
    res = client.get(done["result"]["url"])
    assert res.status_code == 200 and res.content == b"<p>hi</p>"
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["content-security-policy"].startswith("sandbox")


def test_an_unknown_result_is_404(client):
    assert client.get("/api/udf/results/" + "0" * 64).status_code == 404
    assert client.get("/api/udf/results/..%2Fetc").status_code == 404


# ------------------------------------------------------- the worker's shaping


class _Result:
    def __init__(self, content=b"", headers=None, data=None):
        self.original_content = content
        self.original_headers = headers or {}
        self.data = data


def test_wkb_points_lines_polygons_and_multis():
    point = struct.pack("<BIdd", 1, 1, 1.5, 2)
    assert shaping.wkb_to_wkt(point) == "POINT (1.5 2)"
    assert shaping.wkb_to_wkt(struct.pack(">BIdd", 0, 1, 3, 4)) == "POINT (3 4)"
    line = struct.pack("<BII", 1, 2, 2) + struct.pack("<4d", 0, 0, 1, 1)
    assert shaping.wkb_to_wkt(line) == "LINESTRING (0 0, 1 1)"
    poly = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", 4) + struct.pack("<8d", 0, 0, 1, 0, 1, 1, 0, 0)
    assert shaping.wkb_to_wkt(poly) == "POLYGON ((0 0, 1 0, 1 1, 0 0))"
    multi = struct.pack("<BII", 1, 4, 2) + point + struct.pack("<BIdd", 1, 1, 3, 4)
    assert shaping.wkb_to_wkt(multi) == "MULTIPOINT ((1.5 2), (3 4))"
    assert shaping.wkb_to_wkt(struct.pack("<BIddd", 1, 1001, 1, 2, 3)) == "POINT (1 2 3)"
    with pytest.raises(ValueError):
        shaping.wkb_to_wkt(b"\x01\x02")
    with pytest.raises(ValueError):
        shaping.wkb_to_wkt(point + b"x")


def test_json_body_writes_a_table_as_rows_with_geometry_as_wkt():
    pd = pytest.importorskip("pandas")
    point = struct.pack("<BIdd", 1, 1, 1, 2)
    frame = pd.DataFrame({"id": [1, 2], "geometry": [point, b"\xff"], "v": [1.5, float("nan")]})
    rows = json.loads(shaping.json_body(frame))
    assert rows == [{"id": 1, "geometry": "POINT (1 2)", "v": 1.5},
                    {"id": 2, "geometry": base64.b64encode(b"\xff").decode(), "v": None}]
    assert json.loads(shaping.json_body({"a": [1, "x"]})) == {"a": [1, "x"]}
    assert json.loads(shaping.json_body("hi")) == "hi"


def test_body_of_each_format():
    par = _Result(b"PAR1....", data=None)
    assert shaping.body_of(par, "parquet") == (b"PAR1....", shaping.PARQUET_TYPE)
    js = _Result(b'"hi"', data="hi")
    assert shaping.body_of(js, "parquet")[1] == "application/json"
    assert shaping.body_of(js, "json") == (b'"hi"', "application/json")
    html = _Result(b"<p>", headers={"Content-Type": "text/html"})
    assert shaping.body_of(html, "auto") == (b"<p>", "text/html")
    batch = _Result(b"", data="value")
    assert shaping.body_of(batch, "auto") == (b"value", "text/plain; charset=utf-8")


def test_data_page_shapes():
    pd = pytest.importorskip("pandas")
    assert shaping.data_page(None) == (b"", "")
    assert shaping.data_page("hi") == (b"hi", "text/plain; charset=utf-8")
    assert shaping.data_page({"a": 1}) == (b'{"a": 1}', "application/json")
    assert shaping.data_page(b"\x00") == (b"\x00", "application/octet-stream")
    rows = shaping.DATA_PAGE_MAX_ROWS + 5
    body, kind = shaping.data_page(pd.DataFrame({"a": range(rows)}))
    assert kind.startswith("text/html")
    assert f"Showing the first {shaping.DATA_PAGE_MAX_ROWS} of {rows} rows." in body.decode()
