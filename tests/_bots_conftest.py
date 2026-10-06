"""Fixtures the ported FusedBot tests (tests/test_bots_*.py) expect from
FusedBot's own conftest, rebuilt on fused-render.

Each bots test module does `from _bots_conftest import *` so pytest registers
these in that module only; nothing here leaks into the rest of the suite
(the one suite-wide bots fixture, `_no_bots_threads`, is in conftest.py).

* `app_home` (autouse): a per-test FUSED_RENDER_HOME. fused-render's conftest
  redirects the home once per session; the bots tests list and count bots, so
  they need a fresh one each. HOME is a per-test empty tree too, as in
  FusedBot (Chrome profiles, ~/Library/Messages and ~/.claude are read off it),
  and FUSED_RENDER_CLAUDE_BIN points at a missing file so no test spawns the
  developer's real `claude` (a test that wants one installs a stub).
* `client`: a REAL uvicorn server on a loopback port with `create_app()`,
  wrapped in FusedBot's `Client` (`get`/`post` -> `(status, headers, body)`,
  `X-Fused: 1` on every POST, `.base` and `._do` for the odd raw request).
  Real rather than TestClient because bot threads and botmcp reach the
  server back over HTTP through FUSED_RENDER_ORIGIN, which the fixture sets.
  Lifespan is OFF: startup hooks (index scans, warmers, the bots scheduler)
  are not what these tests are about, and FusedBot's `serve_in_thread` ran
  only `start_ai`, whose bots half its conftest stubbed out too.
* `v1_fused` / `v2_fused`: the .fused exports `/api/bot-apps/import` reads.
"""
import json
import threading
import time
import urllib.error
import urllib.request
import zipfile

import pytest

ENTRY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="fused-app" />
<meta name="fused-api-version" content="1" /><title>t</title></head>
<body><script>fused.runPython("calc.py", {n: "3"});</script></body></html>
"""

CALC_PY = """import json
def main(n: int = 1, label: str = "x") -> dict:
    print("hello from calc")
    return {"double": n * 2, "label": label}
"""


@pytest.fixture(autouse=True)
def app_home(tmp_path, tmp_path_factory, monkeypatch):
    from fused_render.shell import storage

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    fake_home = tmp_path_factory.mktemp("bots-user-home")
    (fake_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    return storage.home_dir()


class Client:
    def __init__(self, base):
        self.base = base

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        return self._do(req)

    def post(self, path, body, headers=None, raw=False):
        h = {"X-Fused": "1"}
        h.update(headers or {})
        data = body if raw else json.dumps(body).encode()
        if not raw:
            h["Content-Type"] = "application/json"
        return self._do(urllib.request.Request(self.base + path, data=data, headers=h, method="POST"))

    def delete(self, path, headers=None):
        h = {"X-Fused": "1"}
        h.update(headers or {})
        return self._do(urllib.request.Request(self.base + path, headers=h, method="DELETE"))

    @staticmethod
    def _do(req):
        # The headers stay an `http.client.HTTPMessage` (case-insensitive
        # lookups), not FusedBot's `dict(...)`: uvicorn sends lower-case names
        # where FusedBot's http.server sent `Content-Type`.
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import uvicorn

    from fused_render import jobs
    from fused_render.server.app import create_app

    reset = getattr(jobs, "reset", None)
    if callable(reset):
        reset()
    start = tmp_path / "start"
    start.mkdir()
    app = create_app(str(start))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="bots-test-server")
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            break
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", base)
    yield Client(base)
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def v2_fused(tmp_path):
    from fused_render import appfile_container as container

    out = tmp_path / "demo.fused"
    container.write(
        str(out),
        {"name": "demo", "entry": "index.html"},
        [("index.html", ENTRY_HTML.encode()), ("calc.py", CALC_PY.encode()),
         ("data/note.txt", b"hello")],
    )
    return str(out)


@pytest.fixture
def v1_fused(tmp_path):
    out = tmp_path / "legacy.fused"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(
            {"fused_app_file": 1, "name": "legacy", "entry": "index.html"}))
        zf.writestr("files/index.html", ENTRY_HTML)
        zf.writestr("files/calc.py", CALC_PY)
    return str(out)


__all__ = ["app_home", "client", "v1_fused", "v2_fused", "Client", "ENTRY_HTML", "CALC_PY"]
