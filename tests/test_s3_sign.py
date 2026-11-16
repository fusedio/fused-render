"""`fused.s3.sign` — a read URL for an `s3://` object, so a template streams it.

Three layers, one contract:

* `remote_access.sign_read_url` — the authorization point: only the supported
  bucket, only an object (never a prefix), no network. Today a no-op that
  returns the public URL with no expiry.
* `POST /api/remote/sign` — the route every caller goes through: X-Fused,
  the Canvases gate, the `{error, code}` shape, and the call log naming the
  OBJECT signed, never the signed URL.
* the two clients — `fused.s3` in runtime.js (its block run under node against
  a stubbed fetch) and `templates/shared/fused_s3.py` (a real urllib round
  trip against the real router on a local uvicorn).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import fused_render
from fused_render import remote_access
from fused_render.remote_access import RemoteError

OBJ = "s3://fused-asset/data/a b+c.parquet"
PUBLIC = "https://fused-asset.s3.us-west-2.amazonaws.com/data/a%20b%2Bc.parquet"
FUSED = {"X-Fused": "1"}


# --------------------------------------------------------- remote_access


def test_sign_returns_the_public_url_with_no_expiry():
    signed = remote_access.sign_read_url("fused-asset", "data/a b+c.parquet")
    assert signed.url == PUBLIC
    assert signed.url == remote_access.read_url("fused-asset", "data/a b+c.parquet")
    assert signed.expires_at is None


def test_sign_refuses_other_buckets_and_prefixes():
    with pytest.raises(RemoteError) as e:
        remote_access.sign_read_url("someone-else", "x.parquet")
    assert e.value.code == "unsupported_bucket"
    for key in ("", "data/"):
        with pytest.raises(RemoteError) as e:
            remote_access.sign_read_url("fused-asset", key)
        assert e.value.code == "bad_url"


def test_sign_makes_no_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("signing must not touch the network")

    monkeypatch.setattr(remote_access._client, "send", boom)
    monkeypatch.setattr(remote_access._client, "head", boom)
    remote_access.sign_read_url("fused-asset", "x.tif")


# ------------------------------------------------------------- the route


def _app(monkeypatch, *, enabled=True, record=None):
    from fused_render.server.routers import remote as remote_router
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: enabled)
    app = FastAPI()

    if record is not None:
        # Stands in for the call-log middleware (server/common.py), which
        # hands every route the in-flight record on request.state.
        @app.middleware("http")
        async def _call(request: Request, call_next):
            request.state.fused_call = record
            return await call_next(request)

    app.include_router(remote_router.router)
    return app


def test_route_signs_an_object(monkeypatch):
    client = TestClient(_app(monkeypatch))
    r = client.post("/api/remote/sign", json={"url": OBJ}, headers=FUSED)
    assert r.status_code == 200
    assert r.json() == {"url": PUBLIC, "expires_at": None}


def test_route_reports_an_expiry_as_iso_utc(monkeypatch):
    monkeypatch.setattr(remote_access, "sign_read_url", lambda b, k: remote_access.SignedUrl(
        url="https://signed.example/x?sig=1", expires_at=1_800_000_000.0))
    client = TestClient(_app(monkeypatch))
    r = client.post("/api/remote/sign", json={"url": OBJ}, headers=FUSED)
    assert r.json() == {"url": "https://signed.example/x?sig=1",
                        "expires_at": "2027-01-15T08:00:00Z"}


@pytest.mark.parametrize("body, status, code", [
    ({"url": "s3://other-bucket/x.parquet"}, 400, "unsupported_bucket"),
    ({"url": "s3://fused-asset/data/"}, 400, "bad_url"),
    ({"url": "https://fused-asset.s3.amazonaws.com/x"}, 400, "bad_url"),
    ({"url": 7}, 400, "bad_url"),
    ({}, 400, "bad_url"),
    (["s3://fused-asset/x"], 400, "bad_url"),
])
def test_route_refuses_what_it_will_not_sign(monkeypatch, body, status, code):
    client = TestClient(_app(monkeypatch))
    r = client.post("/api/remote/sign", json=body, headers=FUSED)
    assert (r.status_code, r.json()["code"]) == (status, code)
    assert isinstance(r.json()["error"], str) and r.json()["error"]


def test_route_needs_x_fused_and_the_canvases_flag(monkeypatch):
    client = TestClient(_app(monkeypatch))
    r = client.post("/api/remote/sign", json={"url": OBJ})
    assert (r.status_code, r.json()["code"]) == (403, "forbidden")

    client = TestClient(_app(monkeypatch, enabled=False))
    r = client.post("/api/remote/sign", json={"url": OBJ}, headers=FUSED)
    assert (r.status_code, r.json()["code"]) == (403, "disabled")


def test_call_log_names_the_object_not_the_signed_url(monkeypatch):
    monkeypatch.setattr(remote_access, "sign_read_url", lambda b, k: remote_access.SignedUrl(
        url="https://signed.example/x?X-Amz-Signature=secret", expires_at=None))
    record: dict = {"entrypoint": None, "entrypoint_name": None}
    client = TestClient(_app(monkeypatch, record=record))
    r = client.post("/api/remote/sign", json={"url": OBJ}, headers=FUSED)
    assert r.status_code == 200
    assert record["entrypoint"] == OBJ
    assert record["entrypoint_name"] == "a b+c.parquet"
    assert "secret" not in json.dumps(record)


# ------------------------------------------------- the Python client (shared)


_SHARED_DIR = os.path.join(os.path.dirname(fused_render.__file__), "templates", "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import fused_s3  # noqa: E402 - path seeded above, matching production


@pytest.fixture
def served(monkeypatch):
    """The real remote router on a local uvicorn; FUSED_RENDER_ORIGIN at it."""
    import uvicorn

    state = {"enabled": True}
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: state["enabled"])
    from fused_render.server.routers import remote as remote_router

    app = FastAPI()
    app.include_router(remote_router.router)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    give_up = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < give_up
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", f"http://127.0.0.1:{port}")
    yield state
    server.should_exit = True
    thread.join(5)


def test_python_client_signs(served):
    assert fused_s3.sign(OBJ) == {"url": PUBLIC, "expires_at": None}


def test_python_client_raises_with_the_route_code(served):
    with pytest.raises(fused_s3.S3Error) as e:
        fused_s3.sign("s3://other-bucket/x.parquet")
    assert (e.value.code, e.value.status) == ("unsupported_bucket", 400)
    assert "Only the fused-asset bucket" in str(e.value)

    served["enabled"] = False
    with pytest.raises(fused_s3.S3Error) as e:
        fused_s3.sign(OBJ)
    assert (e.value.code, e.value.status) == ("disabled", 403)


def test_python_client_without_a_server(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    with pytest.raises(fused_s3.S3Error) as e:
        fused_s3.sign(OBJ)
    assert e.value.code == "unreachable"

    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:9")  # discard port
    with pytest.raises(fused_s3.S3Error) as e:
        fused_s3.sign(OBJ)
    assert e.value.code == "unreachable"


def test_is_s3():
    assert fused_s3.is_s3("s3://fused-asset/x") and fused_s3.is_s3("S3://fused-asset/x")
    for other in ("/Users/me/x.parquet", "https://x/y", "", None, 3):
        assert not fused_s3.is_s3(other)


def test_expired():
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 30))
    later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    assert not fused_s3.expired({"url": "u", "expires_at": None})
    assert fused_s3.expired({"url": "u", "expires_at": soon})
    assert not fused_s3.expired({"url": "u", "expires_at": later})
    assert fused_s3.expired({"url": "u", "expires_at": "not a time"})


def test_python_client_is_stdlib_only():
    """The shared-dir rule: never `import fused_render`, nothing third party."""
    import ast

    tree = ast.parse(Path(_SHARED_DIR, "fused_s3.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - {"__future__"} <= set(sys.stdlib_module_names), imported


# ---------------------------------------------------- the JS client (runtime)


RUNTIME = (Path(fused_render.__file__).parent / "static" / "runtime.js").read_text(
    encoding="utf-8")
BLOCK = RUNTIME.split("// fused-s3:start", 1)[1].split("// fused-s3:end", 1)[0]

_PRELUDE = """
const CALLS = [];
let REPLY = {status: 200, body: {url: "https://x/y.parquet", expires_at: null}};
function fetch(url, init) {
  CALLS.push({url: url, init: init || {}});
  return Promise.resolve({
    ok: REPLY.status < 400,
    status: REPLY.status,
    json: () => ("body" in REPLY ? Promise.resolve(REPLY.body)
                                 : Promise.reject(new Error("not JSON"))),
  });
}
function callHeaders(extra) {
  return Object.assign({}, extra || {}, {"X-Fused-Call": "c1"});
}
function out(v) { console.log(JSON.stringify(v)); }
"""


def _node(call: str, prelude: str = "") -> Any:
    if not shutil.which("node"):
        pytest.skip("node is needed to run the fused.s3 block")
    script = _PRELUDE + prelude + BLOCK + "\n" + call
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_s3_is_on_the_fused_surface():
    surface = RUNTIME.split("window.fused = {", 1)[1].split("};", 1)[0]
    assert "\n    s3,\n" in surface


def test_js_sign_posts_the_address_with_x_fused():
    got = _node(f's3.sign({json.dumps(OBJ)}).then((r) => out({{r: r, calls: CALLS}}));')
    assert got["r"] == {"url": "https://x/y.parquet", "expires_at": None}
    (call,) = got["calls"]
    assert call["url"] == "/api/remote/sign"
    assert call["init"]["method"] == "POST"
    assert call["init"]["headers"]["X-Fused"] == "1"
    assert call["init"]["headers"]["X-Fused-Call"] == "c1"  # call-log attribution
    assert json.loads(call["init"]["body"]) == {"url": OBJ}


def test_js_sign_rejects_with_status_and_code():
    got = _node(
        's3.sign("s3://b/x").then(() => out("resolved"), '
        '(e) => out({m: e.message, s: e.status, c: e.code}));',
        prelude='REPLY = {status: 400, body: {error: "Only the fused-asset bucket can be '
                'opened for now.", code: "unsupported_bucket"}};')
    assert got == {"m": "Only the fused-asset bucket can be opened for now.",
                   "s": 400, "c": "unsupported_bucket"}

    got = _node('s3.sign("s3://b/x").then(() => out("resolved"), '
                '(e) => out({m: e.message, s: e.status, c: e.code}));',
                prelude="REPLY = {status: 502};")
    assert got == {"m": "HTTP 502", "s": 502, "c": None}


# -------------------------------------------------- which templates stream
# (D1485: the `stream-s3` marker and `/api/remote/stat`'s stream_templates)


def test_which_templates_stream_which_files():
    from fused_render.server import templates as server_templates

    pq = server_templates.remote_stream_templates("t.parquet")
    assert [e["mode"] for e in pq] == ["duckdb"]
    assert pq[0]["path"].endswith(os.path.join("duckdb", "template.html"))
    assert [e["mode"] for e in server_templates.remote_stream_templates("T.PARQUET")] == ["duckdb"]
    # duckdb is bound to CSV/JSON/.duckdb too, but its marker lists parquet only
    for name in ("t.csv", "t.json", "t.duckdb", "Makefile"):  # no text sniff either
        assert server_templates.remote_stream_templates(name) == [], name


def test_a_marker_lists_extensions_or_streams_everything(tmp_path):
    from fused_render.server import templates as server_templates

    m = tmp_path / "stream-s3"
    m.write_text(".parquet .GeoParquet\nprose. not an extension line\n")
    assert server_templates._streamed_extensions(str(m)) == (".parquet", ".geoparquet")
    m.write_text("no extension lines at all\n")
    assert server_templates._streamed_extensions(str(m)) is None
    assert server_templates._streamed_extensions(str(tmp_path / "missing")) == ()


def test_remote_stat_reports_stream_templates(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fused_render import remote_access, remote_fetch
    from fused_render.server.routers import remote as remote_router
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)
    monkeypatch.setattr(remote_router, "_index_answer", lambda name, arg: None)
    monkeypatch.setattr(remote_access, "head", lambda b, k: remote_access.ObjectInfo(
        size=123, mtime=None, etag="e"))
    monkeypatch.setattr(remote_fetch, "cached_path", lambda b, k, info: None)
    app = FastAPI()
    app.include_router(remote_router.router)
    c = TestClient(app)
    pq = c.get("/api/remote/stat", params={"url": "s3://fused-asset/a/b.parquet"}).json()
    assert [e["mode"] for e in pq["stream_templates"]] == ["duckdb"]
    csv = c.get("/api/remote/stat", params={"url": "s3://fused-asset/a/b.csv"}).json()
    assert csv["stream_templates"] == []
