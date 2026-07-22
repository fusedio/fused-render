"""Mount-safety of the canvas reader's sibling scan.

The canvas reader badges each UDF node with the sibling files (`{udfName}.py`
/`.json`/`.md`/`.html`) present next to the toml. It found them with one
`os.listdir` of the toml's directory — a KERNEL listing that, on a path backed
by a remote rclone NFS mount, enumerates the whole parent S3 prefix and can
DROP the mount, wedging the server.

The fix keeps the reader mount-AGNOSTIC: the browser passes a `src`
(server-origin + /api/fs/raw?path=); the reader asks /api/fs/stat whether the
toml's dir is `remote` and, if so, lists it via /api/fs/list (the server routes
that through rclone's rc, never the kernel) instead of os.listdir.

These tests stand up a threaded localhost server for the two fs endpoints and,
for the remote path, DELETE the on-disk siblings first: a silent kernel-listdir
fallback would then find nothing and the sibling assertions would fail loudly,
so the tests can only pass if the listing really came over HTTP.
"""

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render import server

requires_tomllib = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="canvas.toml parsing needs tomllib (Python 3.11+)",
)


# --------------------------------------------------------------------------
# reader module (plain callable — the @fused.udf shim is optional, so no
# importorskip("fused") is needed to exercise main()).
# --------------------------------------------------------------------------


def _load_reader():
    path = os.path.join(server.TEMPLATES_DIR, "canvas", "reader.py")
    spec = importlib.util.spec_from_file_location("canvas_reader_mount", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def reader():
    return _load_reader()


# --------------------------------------------------------------------------
# a stand-in for /api/fs/stat + /api/fs/list
# --------------------------------------------------------------------------


class _FakeFS:
    """Serves /api/fs/stat (json {remote,is_dir}) and /api/fs/list (json with
    `entries`). `remote` toggles the stat flag; `list_names` are the entry names
    /api/fs/list returns; `truncated` sets that flag on the list page."""

    def __init__(self, remote=True, list_names=(), truncated=False):
        self.remote = remote
        self.list_names = list(list_names)
        self.truncated = truncated
        self.list_calls = 0
        fs = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/fs/stat"):
                    self._json({"remote": fs.remote, "is_dir": True, "size": None, "name": "dir"})
                    return
                if self.path.startswith("/api/fs/list"):
                    fs.list_calls += 1
                    self._json(
                        {
                            "path": "/x",
                            "entries": [
                                {
                                    "name": n,
                                    "is_dir": False,
                                    "size": 0,
                                    "mtime": 0,
                                    "ignored": False,
                                }
                                for n in fs.list_names
                            ],
                            "truncated": fs.truncated,
                            "cursor": None,
                        }
                    )
                    return
                self.send_response(404)
                self.end_headers()

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def src(self):
        # mirror how the template builds src: origin + /api/fs/raw?path=...
        return f"http://127.0.0.1:{self.port}/api/fs/raw?path=%2Fx"

    def close(self):
        self._srv.shutdown()


@pytest.fixture
def fs():
    servers = []

    def make(**kw):
        s = _FakeFS(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


_CANVAS = (
    'type = "canvas"\nversion = 2\nname = "M"\n[canvas]\nedges = []\n'
    '[[canvas.nodes]]\nudfName = "a"\nx = 0\ny = 0\n'
    '[[canvas.nodes]]\nudfName = "b"\nx = 10\ny = 10\n'
    '[[canvas.nodes]]\nudfName = "c"\nx = 20\ny = 20\n'
)


def _write_toml(tmp_path):
    d = tmp_path / "cv"
    d.mkdir()
    (d / "canvas.toml").write_text(_CANVAS)
    return d


# --------------------------------------------------------------------------
# remote dir -> list over HTTP, NEVER kernel-listdir
# --------------------------------------------------------------------------


@requires_tomllib
def test_remote_siblings_come_from_http_list_not_kernel(reader, fs, tmp_path):
    d = _write_toml(tmp_path)
    # NO sibling files on disk — a kernel os.listdir would see only canvas.toml
    # and report no siblings. They exist only in the stubbed /api/fs/list.
    s = fs(remote=True, list_names=["canvas.toml", "a.py", "a.json", "c.md"])
    out = reader.main(file=str(d / "canvas.toml"), src=s.src)
    assert out["siblings"]["a"] == [".py", ".json"]
    assert out["siblings"]["c"] == [".md"]
    assert "b" not in out["siblings"]
    assert s.list_calls == 1  # the reader listed over HTTP


@requires_tomllib
def test_remote_dir_never_calls_kernel_listdir(reader, fs, tmp_path, monkeypatch):
    # Loud guard: make os.listdir explode. The remote path must still return
    # siblings (proving it routed through /api/fs/list and never touched the
    # kernel), rather than crashing.
    d = _write_toml(tmp_path)
    s = fs(remote=True, list_names=["a.py"])

    def boom(*a, **k):
        raise AssertionError("kernel os.listdir called on a remote dir")

    monkeypatch.setattr(os, "listdir", boom)
    out = reader.main(file=str(d / "canvas.toml"), src=s.src)
    assert out["siblings"]["a"] == [".py"]


@requires_tomllib
def test_remote_truncated_page_hides_match_without_wedging(reader, fs, tmp_path):
    # A huge remote dir returns a truncated first page that does NOT contain the
    # match; we accept a missing badge (no cursor-follow) over enumerating the
    # whole prefix. The call still succeeds.
    d = _write_toml(tmp_path)
    s = fs(remote=True, list_names=["zzz.py"], truncated=True)
    out = reader.main(file=str(d / "canvas.toml"), src=s.src)
    assert out["siblings"] == {}


# --------------------------------------------------------------------------
# local / unreachable -> kernel listdir preserved
# --------------------------------------------------------------------------


@requires_tomllib
def test_local_dir_uses_kernel_listdir(reader, fs, tmp_path):
    d = _write_toml(tmp_path)
    (d / "a.py").write_text("x = 1\n")
    (d / "c.md").write_text("# c\n")
    s = fs(remote=False, list_names=["should-not-be-used.py"])
    out = reader.main(file=str(d / "canvas.toml"), src=s.src)
    # siblings come from the on-disk kernel listing, not the HTTP list
    assert out["siblings"]["a"] == [".py"]
    assert out["siblings"]["c"] == [".md"]
    assert s.list_calls == 0


@requires_tomllib
def test_unreachable_server_falls_back_to_kernel(reader, tmp_path):
    d = _write_toml(tmp_path)
    (d / "a.py").write_text("x = 1\n")
    # nothing listening -> _stat unreachable -> presume local -> kernel listdir
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fx"
    out = reader.main(file=str(d / "canvas.toml"), src=src)
    assert out["siblings"]["a"] == [".py"]


@requires_tomllib
def test_no_src_preserves_kernel_behavior(reader, tmp_path):
    d = _write_toml(tmp_path)
    (d / "b.html").write_text("<i></i>\n")
    out = reader.main(file=str(d / "canvas.toml"))  # no src at all
    assert out["siblings"]["b"] == [".html"]
