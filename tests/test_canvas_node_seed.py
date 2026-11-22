"""The saved canvas handed to a canvas node's framed page (SPEC §53.3 NW-8):
the document the sync's probe stores,
`GET /api/canvases/saved`, 
and the contract the `canvas_widget` template keeps, pinned by text (the
template runs in a browser)."""

import importlib.util
import json
import os
import re
import stat
import sys
import threading
import types

import fused_render.canvases as canvases_mod
import pytest
from fastapi.testclient import TestClient
from fused_render import _canvas_saved
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}
ENV = "prod"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(
    ROOT, "fused_render", "templates", "canvas_widget", "template.html"
)
NODE_JS = os.path.join(ROOT, "fused_render", "templates", "shared", "canvas-node.js")
SHIM = os.path.join(ROOT, "fused_render", "_fused_canvas_manifest.py")
# Opaque to the host: not valid for any model, with non-ASCII and odd spacing,
# so only a byte-for-byte relay passes.
DOC = '{ "id": "c1",  "udfs": [], "note": "café ☃", "params": {"k": 1} }'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(tmp_path / "canvases"))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "creds.json"))
    canvases_mod._cli_cache_clear()
    yield TestClient(create_app(start_dir=str(tmp_path)))
    canvases_mod._cli_cache_clear()


def _clone(name="alpha", env=ENV):
    folder = canvases_mod._canvas_dir(env, name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "canvas.toml"), "w", encoding="utf-8") as fh:
        fh.write('type = "canvas"\n')
    return folder


def _store(name="alpha", env=ENV, text=DOC):
    _canvas_saved.write(
        _canvas_saved.path_for(canvases_mod.canvases_root(env), name), text
    )


def _saved(client, name="alpha", env=ENV, headers=FUSED):
    return client.get(
        "/api/canvases/saved", params={"env": env, "name": name}, headers=headers
    )


# ------------------------------------------------------------- preference


# ----------------------------------------------------------------- status


# ------------------------------------------------------ the stored document


def test_the_document_is_written_whole_with_no_temp_file_left(tmp_path):
    path = str(tmp_path / ".sync" / "alpha.canvas.json")
    _canvas_saved.write(path, DOC)
    with open(path, encoding="utf-8", newline="") as fh:
        assert fh.read() == DOC
    assert os.listdir(tmp_path / ".sync") == ["alpha.canvas.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_document_is_private_to_the_user(tmp_path):
    path = str(tmp_path / ".sync" / "alpha.canvas.json")
    _canvas_saved.write(path, DOC)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # A replacement is private too, whatever the umask.
    old = os.umask(0)
    try:
        _canvas_saved.write(path, DOC + " ")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_failed_replace_keeps_the_old_document(tmp_path, monkeypatch):
    path = str(tmp_path / ".sync" / "alpha.canvas.json")
    _canvas_saved.write(path, DOC)

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(_canvas_saved.os, "replace", boom)
    with pytest.raises(OSError):
        _canvas_saved.write(path, "{}")
    assert open(path, encoding="utf-8").read() == DOC
    assert os.listdir(tmp_path / ".sync") == ["alpha.canvas.json"]


class _Manager:
    root = "/r"
    name = "alpha"
    env = ENV


def test_the_probe_is_told_where_to_write(monkeypatch):
    monkeypatch.setattr(canvases_mod, "_cli_env", lambda cli, env: {"A": "1"})
    env = canvases_mod._SyncManager._probe_env(_Manager(), object())
    assert env == {
        "A": "1",
        _canvas_saved.OUT_ENV: os.path.join("/r", ".sync", "alpha.canvas.json"),
    }


def _run_shim(monkeypatch, collection, out_path, name="alpha"):
    """Run the manifest shim's `main` against a fake SDK that answers
    `get_collection_by_name` with `collection`. Returns its stdout."""
    fused = types.ModuleType("fused")
    fused._env = lambda env: None
    api_mod = types.ModuleType("fused._global_api")

    class Api:
        def get_collection_by_name(self, n):
            assert n == name
            if isinstance(collection, Exception):
                raise collection
            return collection

    api_mod.get_api = lambda: Api()
    monkeypatch.setitem(sys.modules, "fused", fused)
    monkeypatch.setitem(sys.modules, "fused._global_api", api_mod)
    monkeypatch.delenv("FUSED_ENV", raising=False)
    if out_path is None:
        monkeypatch.delenv(_canvas_saved.OUT_ENV, raising=False)
    else:
        monkeypatch.setenv(_canvas_saved.OUT_ENV, out_path)
    monkeypatch.setattr(sys, "argv", ["shim", name])
    spec = importlib.util.spec_from_file_location("_shim_under_test", SHIM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = []
    monkeypatch.setattr(mod.sys, "stdout", types.SimpleNamespace(write=out.append))
    mod.main()
    return "".join(out)


def test_the_shim_stores_the_service_answer_and_keeps_its_output(monkeypatch, tmp_path):
    collection = {
        "id": "c1",
        "last_updated": "t1",
        "revision": 4,
        "udfs": [{"slug": "a", "udf_body_hash": "h", "last_updated": "t", "id": "u1"}],
        "extra": {"param": "café"},
    }
    path = str(tmp_path / ".sync" / "alpha.canvas.json")
    manifest = json.loads(_run_shim(monkeypatch, collection, path))
    assert manifest == {
        "id": "c1",
        "last_updated": "t1",
        "udfs": {"a": {"hash": "h", "last_updated": "t", "id": "u1"}},
        "revision": 4,
    }
    assert json.loads(open(path, encoding="utf-8").read()) == collection


def test_the_shim_writes_nothing_without_a_path(monkeypatch, tmp_path):
    out = _run_shim(monkeypatch, {"id": "c1", "udfs": []}, None)
    assert json.loads(out)["id"] == "c1"
    assert not (tmp_path / ".sync").exists()


def test_a_failed_capture_never_breaks_the_probe(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    # The directory cannot be made: the path's parent is a file.
    path = str(blocker / ".sync" / "alpha.canvas.json")
    out = _run_shim(monkeypatch, {"id": "c1", "udfs": []}, path)
    assert json.loads(out)["id"] == "c1"


def test_the_document_goes_with_the_clone():
    leftovers = canvases_mod._clone_leftovers(ENV, "alpha")
    assert _canvas_saved.path_for(canvases_mod.canvases_root(ENV), "alpha") in leftovers


# ------------------------------------------------------------------ route


def test_the_route_is_404_without_a_stored_document(client):
    _clone()
    res = _saved(client)
    assert res.status_code == 404
    assert "detail" in res.json()


def test_the_route_serves_the_stored_bytes_exactly(client):
    _clone()
    _store()
    res = _saved(client)
    assert res.status_code == 200
    assert res.content == DOC.encode("utf-8")
    assert res.headers["content-type"].split(";")[0] == "application/json"
    assert res.headers["cache-control"] == "no-store"


def test_the_route_follows_the_newest_document(client):
    _clone()
    _store(text="{}")
    _store(text='{"id": "c2"}')
    assert _saved(client).content == b'{"id": "c2"}'


def test_the_route_is_per_environment(client):
    _clone()
    _store()
    assert _saved(client, env="unstable").status_code == 404


@pytest.mark.parametrize(
    "name",
    ["nope", "", "../alpha", "..", ".", "alpha/../alpha", "/etc/passwd", "alpha\x00"],
)
def test_an_unknown_canvas_or_a_traversal_is_404(client, name):
    _clone()
    _store()
    # A document planted where a traversal would reach.
    sync = os.path.join(canvases_mod.canvases_root(ENV), ".sync")
    with open(os.path.join(sync, "secret.json"), "w", encoding="utf-8") as fh:
        fh.write("secret")
    res = _saved(client, name=name)
    assert res.status_code == 404
    assert b"secret" not in res.content


def test_a_stored_document_without_a_clone_is_not_served(client):
    _store()
    assert _saved(client).status_code == 404


def test_the_route_needs_the_guard_header(client):
    _clone()
    _store()
    assert _saved(client, headers={}).status_code == 403


def test_a_bad_environment_is_refused(client):
    assert _saved(client, env="nowhere").status_code == 400


# --------------------------------------------- node-js passes it through


def test_canvas_node_workbench_carries_no_seed_switch():
    js = open(NODE_JS, encoding="utf-8").read()
    assert "nodeSeed" not in js and "node_seed" not in js


def test_canvas_node_saved_returns_the_text_or_null():
    js = open(NODE_JS, encoding="utf-8").read()
    body = _body_of(js, "async function saved")
    assert '"/api/canvases/saved?env=" + encodeURIComponent(env)' in body
    assert "encodeURIComponent(name)" in body
    assert "{ headers: GUARD }" in body
    assert "return res.ok ? await res.text() : null;" in body
    assert "catch (_)" in body and "return null;" in body
    assert "JSON.parse" not in body
    assert "saved: saved," in js


# ----------------------------------------------- template contract


@pytest.fixture(scope="module")
def html():
    return open(TEMPLATE, encoding="utf-8").read()


def _body_of(text, header):
    """The text of the brace-balanced block opened by `header`."""
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += text[i] == "{"
        depth -= text[i] == "}"
        if depth == 0:
            return text[start : i + 1]
    raise AssertionError(header)


def test_the_capability_is_always_offered(html):
    send = _body_of(html, "async function sendToken")
    assert send.count('"canvas"') == 1
    assert 'payload.caps = [...payload.caps, "canvas"];' in send
    assert "nodeSeed" not in html
    # Alongside "sql", never instead of it, and not tied to the SQL offer.
    assert 'payload.caps = ["sql"]' in _body_of(send, "if (offer)")
    assert '"canvas"' not in _body_of(send, "if (offer)")


def test_the_text_is_fetched_once_per_frame_load_in_parallel(html):
    load = _body_of(html, "function loadFrame")
    assert "canvasSeed = CanvasNode.saved(node.env, node.name);" in load
    # Started with the frame, ahead of the frame's address and any token.
    assert load.index("canvasSeed =") < load.index("frame.src")
    assert html.count("CanvasNode.saved(") == 1
    assert html.index("frameGen++") < html.index("canvasSeed = CanvasNode.saved")


def test_one_canvas_message_follows_the_first_token_through_post(html):
    send = _body_of(html, "async function sendToken")
    assert "const seed = fresh ? canvasSeed : null;" in send
    assert send.count("fused-embed-canvas") == 1
    token = send.index("post(payload);")
    wait = send.index("await seed")
    message = send.index('post({ type: "fused-embed-canvas"')
    assert token < wait < message
    assert "canvas: node.name, json" in send
    assert "postMessage" not in send


def test_the_token_is_not_held_for_the_canvas(html):
    send = _body_of(html, "async function sendToken")
    # The token was awaited and posted before the text is waited on.
    assert send.index("await CanvasNode.token") < send.index("post(payload);")
    assert send.index("post(payload);") < send.index("await seed")


def test_an_older_document_gets_no_canvas_message(html):
    send = _body_of(html, "async function sendToken")
    assert "const gen = frameGen;" in send
    assert re.search(
        r'if \(gen === frameGen\) post\(\{ type: "fused-embed-canvas"', send
    )


def test_a_refresh_does_not_resend_it_and_a_ready_does(html):
    handler = _body_of(html, 'window.addEventListener("message"')
    assert 'void sendToken(data.type === "fused-embed-auth-ready");' in handler


def test_a_failed_token_sends_no_canvas_message(html):
    send = _body_of(html, "async function sendToken")
    catch = _body_of(send, "catch (error)")
    assert "return;" in catch
# ------------------------------------- a canvas no sync probes (NW-8)


class _Probe:
    """A stand-in for `_seed_run`: counts calls, optionally stores a document,
    and can hold the call until the test releases it."""

    def __init__(self, store=True, hold=False):
        self.calls = 0
        self.store = store
        self.started = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        if not hold:
            self.release.set()

    def __call__(self, env, name):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10)
        if self.store:
            _store(name, env)
        self.done.set()


@pytest.fixture
def probe(monkeypatch):
    def install(**kw):
        fake = _Probe(**kw)
        monkeypatch.setattr(canvases_mod, "_seed_run", fake)
        return fake

    return install


def _age(name="alpha", env=ENV, seconds=3600):
    path = _canvas_saved.path_for(canvases_mod.canvases_root(env), name)
    old = os.stat(path).st_mtime - seconds
    os.utime(path, (old, old))


def test_a_missing_document_is_probed_once_and_served(client, probe):
    fake = probe()
    _clone()
    res = _saved(client)
    assert res.status_code == 200
    assert res.content == DOC.encode("utf-8")
    assert fake.calls == 1


def test_a_failed_probe_is_a_404(client, probe):
    fake = probe(store=False)
    _clone()
    assert _saved(client).status_code == 404
    assert fake.calls == 1


def test_concurrent_requests_share_one_probe(client, probe):
    fake = probe(hold=True)
    _clone()
    results = []

    def ask():
        results.append(_saved(client))

    first = threading.Thread(target=ask)
    first.start()
    assert fake.started.wait(10)
    second = threading.Thread(target=ask)
    second.start()
    fake.release.set()
    first.join(10)
    second.join(10)
    assert [r.status_code for r in results] == [200, 200]
    assert fake.calls == 1


def test_a_fresh_document_starts_no_refresh(client, probe):
    fake = probe()
    _clone()
    _store()
    assert _saved(client).status_code == 200
    assert fake.calls == 0
    assert canvases_mod._SEED_INFLIGHT == {}


def test_a_stale_document_is_served_at_once_and_refreshed_once(client, probe):
    fake = probe(hold=True)
    _clone()
    _store()
    _age()
    # The refresh is held, so the answer cannot have waited for it.
    first = _saved(client)
    assert first.status_code == 200 and first.content == DOC.encode("utf-8")
    assert fake.started.wait(10)
    second = _saved(client)  # the refresh is still in flight
    assert second.status_code == 200
    fake.release.set()
    assert fake.done.wait(10)
    assert fake.calls == 1


def test_a_probe_is_not_run_for_an_unknown_canvas(client, probe):
    fake = probe()
    assert _saved(client, name="nope").status_code == 404
    assert fake.calls == 0


def test_the_probe_runs_the_manifest_shim_with_the_output_path(monkeypatch):
    seen = []
    monkeypatch.setattr(canvases_mod, "_QUITTING", False)
    monkeypatch.setattr(canvases_mod, "_logged_in", lambda env: True)
    monkeypatch.setattr(canvases_mod, "_probe_manifest", lambda *a: seen.append(a))
    canvases_mod._seed_run(ENV, "alpha")
    assert seen == [
        (ENV, canvases_mod.canvases_root(ENV), "alpha", canvases_mod.SEED_PROBE_TIMEOUT)
    ]
    assert canvases_mod.SEED_PROBE_TIMEOUT == 15.0


def test_no_probe_when_signed_out_or_quitting(monkeypatch):
    seen = []
    monkeypatch.setattr(canvases_mod, "_probe_manifest", lambda *a: seen.append(a))
    monkeypatch.setattr(canvases_mod, "_logged_in", lambda env: False)
    canvases_mod._seed_run(ENV, "alpha")
    monkeypatch.setattr(canvases_mod, "_logged_in", lambda env: True)
    monkeypatch.setattr(canvases_mod, "_QUITTING", True)
    canvases_mod._seed_run(ENV, "alpha")
    assert seen == []


def test_a_reference_copy_is_served_like_a_clone(client):
    # A reference copy is not a live clone, but its node files open the same
    # page, so it gets a saved canvas too.
    folder = _clone("mirrored")
    with open(os.path.join(folder, ".fused-canvas.json"), "w", encoding="utf-8") as fh:
        fh.write('{"version": 1, "sync": "mirror", "name": "mirrored"}')
    assert "mirrored" not in canvases_mod._cloned_names(ENV)
    _store("mirrored")
    res = _saved(client, "mirrored")
    assert res.status_code == 200
    assert res.text == DOC
