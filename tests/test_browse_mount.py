"""Mount-safety of the file-explorer directory listing (browse.py).

A kernel directory listing (os.listdir) on a path backed by a remote rclone NFS
mount forces rclone to enumerate the ENTIRE parent S3 prefix; on a large/flat
prefix this exceeds the NFS deadman timeout and DROPS the mount, wedging the
whole server. So browse.py must NEVER kernel-list a path that /api/fs/stat
reports as `remote:true`; it lists such dirs via /api/fs/list instead. All 7
templates share the same browse.py logic (only zarr_aoi differs, in the
store-extension classification), so these tests exercise the geotiff copy as the
representative; a couple of assertions pin the zarr_aoi store behaviour too.

The remote tests use the delete-the-local-dir trick from test_pyramid_mount.py:
the worker is handed a `dir` string that does NOT exist on disk, and os.listdir
is monkeypatched to explode, so any silent kernel fallback fails LOUDLY instead
of quietly returning an empty/wrong listing. A threaded localhost HTTP server
stands in for the /api/fs/stat + /api/fs/list endpoints.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render.templates.geotiff import browse as br
from fused_render.templates.zarr_aoi import browse as zbr

# --------------------------------------------------------------------------
# a stand-in for /api/fs/stat + /api/fs/list
# --------------------------------------------------------------------------


class _FakeFS:
    """Serves /api/fs/stat (json) and /api/fs/list (json) for one directory.

    `remote` toggles the stat `remote` flag; `exists` False -> stat 404;
    `list_status` lets a test force /api/fs/list to fail (503)."""

    def __init__(
        self, entries=None, remote=True, exists=True, is_dir=True, list_status=200, truncated=False
    ):
        self.entries = entries or []
        self.remote = remote
        self.exists = exists
        self.is_dir = is_dir
        self.list_status = list_status
        self.truncated = truncated
        fs = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/api/fs/stat"):
                    if not fs.exists:
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(b'{"error":"no such file"}')
                        return
                    self._json(
                        200,
                        {
                            "remote": fs.remote,
                            "is_dir": fs.is_dir,
                            "size": None if fs.is_dir else 123,
                            "name": "x",
                        },
                    )
                    return
                if self.path.startswith("/api/fs/list"):
                    if fs.list_status != 200:
                        self.send_response(fs.list_status)
                        self.end_headers()
                        self.wfile.write(b'{"error":"broken"}')
                        return
                    self._json(
                        200,
                        {
                            "path": "/mnt/d",
                            "entries": fs.entries,
                            "truncated": fs.truncated,
                            "cursor": None,
                        },
                    )
                    return
                self.send_response(404)
                self.end_headers()

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def src(self):
        # mirror how the template builds src: origin + /api/fs/raw?path=...
        return f"http://127.0.0.1:{self.port}/api/fs/raw?path=%2Fmnt%2Fd"

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


def _entry(name, is_dir=False, size=None):
    return {"name": name, "is_dir": is_dir, "size": size, "mtime": 0, "ignored": False}


@pytest.fixture
def no_kernel_list(monkeypatch):
    """Make ANY kernel directory listing / probe explode, so a silent fallback
    to os.listdir on a remote path fails loudly instead of returning wrong data.
    """

    def boom(*a, **k):
        raise AssertionError("kernel filesystem access on a remote path")

    monkeypatch.setattr(os, "listdir", boom)
    monkeypatch.setattr(os, "scandir", boom)
    monkeypatch.setattr(os.path, "isdir", boom)
    monkeypatch.setattr(os.path, "getsize", boom)


# --------------------------------------------------------------------------
# (a) remote dir -> lists via /api/fs/list, NEVER kernel-lists
# --------------------------------------------------------------------------


def test_remote_dir_lists_via_http_no_kernel(fs, no_kernel_list):
    s = fs(
        remote=True,
        is_dir=True,
        entries=[
            _entry("sub", is_dir=True),
            _entry("a.tif", size=10),
            _entry("b.txt", size=20),  # not a .tif -> filtered out (show_all False)
            _entry(".hidden", size=5),  # dotfile -> hidden
        ],
    )
    # /mnt/gone/d does not exist on disk; if browse kernel-listed it we'd either
    # AssertionError (patched) or get an error shape. Neither may happen.
    res = br.main(dir="/mnt/gone/d", exts=".tif,.tiff", src=s.src)
    assert "error" not in res
    assert [d["name"] for d in res["dirs"]] == ["sub"]
    assert res["dirs"][0]["path"] == "/mnt/gone/d/sub"
    assert res["dirs"][0]["is_dir"] is True
    # only the loadable .tif survives the ext filter; dotfile hidden
    assert [f["name"] for f in res["files"]] == ["a.tif"]
    f = res["files"][0]
    assert f["path"] == "/mnt/gone/d/a.tif"
    assert f["size"] == 10 and f["ext"] == ".tif" and f["loadable"] is True
    # breadcrumbs are pure string ops off `dir`
    assert res["crumbs"][-1] == {"label": "d", "path": "/mnt/gone/d"}
    assert res["dir"] == "/mnt/gone/d"


def test_remote_show_all_includes_nonloadable(fs, no_kernel_list):
    s = fs(remote=True, entries=[_entry("b.txt", size=20)])
    res = br.main(dir="/mnt/gone/d", exts=".tif", show_all=True, src=s.src)
    assert [f["name"] for f in res["files"]] == ["b.txt"]
    assert res["files"][0]["loadable"] is False


def test_remote_missing_returns_error_shape(fs, no_kernel_list):
    s = fs(exists=False)
    res = br.main(dir="/mnt/gone/d", exts=".tif", src=s.src)
    assert res["error"].startswith("cannot list /mnt/gone/d")
    assert res["parent"] == "/mnt/gone"


def test_remote_list_failure_no_kernel_fallback(fs, no_kernel_list):
    # stat says remote, but /api/fs/list 503s. We must NOT fall back to a kernel
    # listdir (that is the mount-killer); return the error shape instead.
    s = fs(remote=True, list_status=503)
    res = br.main(dir="/mnt/gone/d", exts=".tif", src=s.src)
    assert res["error"].startswith("cannot list /mnt/gone/d")


def test_remote_file_path_descends_to_parent(fs, no_kernel_list):
    # `dir` points at a file (stat is_dir False). browse must descend to the
    # parent via a pure string op (no kernel) and list that.
    s = fs(remote=True, is_dir=False, entries=[_entry("a.tif", size=1)])
    res = br.main(dir="/mnt/gone/f.tif", exts=".tif", src=s.src)
    assert res["dir"] == "/mnt/gone"
    assert [f["name"] for f in res["files"]] == ["a.tif"]


def test_remote_zarr_store_classified_as_loadable(fs, no_kernel_list):
    # zarr_aoi: a directory named *.zarr is a loadable store, not a folder.
    s = fs(
        remote=True,
        entries=[
            _entry("data.zarr", is_dir=True),
            _entry("plain", is_dir=True),
        ],
    )
    res = zbr.main(dir="/mnt/gone/d", exts=".zarr", store_exts=".zarr", src=s.src)
    assert [d["name"] for d in res["dirs"]] == ["plain"]
    store = [f for f in res["files"] if f["name"] == "data.zarr"][0]
    assert store["is_dir"] is True and store["loadable"] is True


# --------------------------------------------------------------------------
# (b) local dir (no src) -> kernel path still works
# --------------------------------------------------------------------------


def test_local_no_src_uses_kernel(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.tif").write_bytes(b"x" * 7)
    (tmp_path / "b.txt").write_bytes(b"y" * 3)
    (tmp_path / ".hidden").write_bytes(b"z")
    res = br.main(dir=str(tmp_path), exts=".tif,.tiff")
    assert "error" not in res
    assert [d["name"] for d in res["dirs"]] == ["sub"]
    assert [f["name"] for f in res["files"]] == ["a.tif"]
    assert res["files"][0]["size"] == 7


# --------------------------------------------------------------------------
# (c) server unreachable -> presume local, fall back to kernel
# --------------------------------------------------------------------------


def test_unreachable_falls_back_to_kernel(tmp_path):
    (tmp_path / "a.tif").write_bytes(b"x" * 4)
    # nothing listening on port 1 -> _stat returns "unreachable" -> local kernel
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fmnt%2Fd"
    res = br.main(dir=str(tmp_path), exts=".tif", src=src)
    assert "error" not in res
    assert [f["name"] for f in res["files"]] == ["a.tif"]


def test_local_not_remote_uses_kernel(fs, tmp_path):
    # stat reachable but reports remote:false -> kernel listdir is fine/faster.
    (tmp_path / "a.tif").write_bytes(b"x" * 4)
    s = fs(remote=False, is_dir=True)
    res = br.main(dir=str(tmp_path), exts=".tif", src=s.src)
    assert "error" not in res
    assert [f["name"] for f in res["files"]] == ["a.tif"]
