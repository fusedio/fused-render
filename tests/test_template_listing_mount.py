"""Mount-safety of the lower-likelihood directory-listing template readers.

A kernel directory listing (os.listdir/os.scandir/os.walk) on a path under a
remote rclone NFS mount enumerates the entire parent S3 prefix and can DROP the
mount, wedging the server. Each audited reader must, when the server's
/api/fs/stat reports a path as `remote`, list it via the mount-routed,
paginated /api/fs/list — NEVER through the kernel.

These tests stand up a threaded localhost HTTP server for /api/fs/stat +
/api/fs/list and point each reader at a path that does NOT exist on local disk
while the fake server reports it as a remote directory. Any accidental kernel
fallback would fail to find the path (empty/NotADirectory) instead of returning
the HTTP-served entries — so a silent-fallback regression fails loudly.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from fused_render.templates.map import discover as map_discover
from fused_render.templates.photos import reader as photos
from fused_render.templates.log_studio import reader as logstudio


class _FakeFS:
    """Serves /api/fs/stat (remote-dir flag) and /api/fs/list (paginated
    entries) for one directory. `entries` is the full listing; `page` caps each
    /api/fs/list response so cursor pagination is exercised."""

    def __init__(self, entries, remote=True, exists=True, page=1000):
        self.entries = entries
        self.remote = remote
        self.exists = exists
        self.page = page
        fs = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path == "/api/fs/stat":
                    if not fs.exists:
                        self._json(404, {"error": "no such file"})
                        return
                    self._json(200, {"remote": fs.remote, "is_dir": True,
                                     "size": None, "name": "d"})
                    return
                if parts.path == "/api/fs/list":
                    qs = parse_qs(parts.query)
                    start = int(qs.get("cursor", ["0"])[0] or "0")
                    chunk = fs.entries[start:start + fs.page]
                    nxt = start + fs.page
                    more = nxt < len(fs.entries)
                    self._json(200, {
                        "path": "/mnt/d",
                        "entries": chunk,
                        "truncated": more,
                        "cursor": str(nxt) if more else "",
                    })
                    return
                self._json(404, {"error": "not found"})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def src(self):
        return f"http://127.0.0.1:{self.port}/api/fs/raw?path=%2Fmnt%2Fd"

    def close(self):
        self._srv.shutdown()


@pytest.fixture
def fs():
    servers = []

    def make(entries, **kw):
        s = _FakeFS(entries, **kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


# a path that does NOT exist on local disk — any kernel listing would fail
REMOTE_DIR = "/definitely/not/here/on/disk/mnt-dir"


def _ent(name, is_dir=False, size=0, mtime=0.0):
    return {"name": name, "is_dir": is_dir, "size": size, "mtime": mtime,
            "ignored": False}


# --------------------------------------------------------------------------
# shared helper block (identical across templates — test one copy: photos)
# --------------------------------------------------------------------------

def test_server_url_uses_origin_only_and_quotes_path():
    src = "http://host:8123/api/fs/raw?path=%2Fclient%2Fraw.tif"
    url = photos._server_url(src, "/api/fs/list", "/a dir/x")
    assert url.startswith("http://host:8123/api/fs/list?path=")
    assert "client" not in url
    assert url.endswith("/a%20dir/x")


def test_stat_ok_missing_unreachable(fs):
    s = fs([], remote=True)
    assert photos._stat(s.src, "/mnt/d")[0] == "ok"
    s2 = fs([], exists=False)
    assert photos._stat(s2.src, "/mnt/d")[0] == "missing"
    assert photos._stat("http://127.0.0.1:1/api/fs/raw?path=x", "/x")[0] == "unreachable"


def test_remote_dir_flag(fs):
    assert photos._remote_dir(fs([], remote=True).src, "/mnt/d") is True
    assert photos._remote_dir(fs([], remote=False).src, "/mnt/d") is False
    assert photos._remote_dir("", "/mnt/d") is False


def test_list_remote_follows_cursor(fs):
    ents = [_ent(f"f{i}.txt") for i in range(5)]
    s = fs(ents, page=2)  # 3 pages
    got, truncated = photos._list_remote(s.src, "/mnt/d")
    assert [e["name"] for e in got] == [e["name"] for e in ents]
    assert truncated is False


def test_list_remote_caps(fs):
    ents = [_ent(f"f{i}.txt") for i in range(10)]
    s = fs(ents, page=2)
    got, truncated = photos._list_remote(s.src, "/mnt/d", cap=4)
    assert len(got) == 4
    assert truncated is True


# --------------------------------------------------------------------------
# reader routing — remote dir listed via HTTP, never the kernel
# --------------------------------------------------------------------------

def test_map_discover_routes_remote(fs):
    s = fs([_ent("sub", is_dir=True), _ent("a.tif", size=10), _ent("note.txt")])
    res = map_discover.main(dir=REMOTE_DIR, src=s.src)
    assert "error" not in res
    kinds = {e["name"]: e["kind"] for e in res["entries"]}
    assert kinds["sub"] == "dir"
    assert kinds["a.tif"] == "raster"
    assert "note.txt" not in kinds  # "other" filtered out


def test_photos_folders_routes_remote(fs):
    s = fs([_ent("sub", is_dir=True), _ent("p.jpg", size=5), _ent("x.txt")])
    res = photos.folders(REMOTE_DIR, src=s.src)
    assert res["ok"] is True
    assert [d["name"] for d in res["dirs"]] == ["sub"]
    assert res["photos"] == 1  # only p.jpg counts


def test_photos_list_dir_routes_remote(fs):
    s = fs([_ent("sub", is_dir=True),
            _ent("p.jpg", size=5, mtime=100.0),
            _ent("readme.md")])
    res = photos.list_dir(REMOTE_DIR, "new", 0, 200, "", "", "", src=s.src)
    assert res["ok"] is True
    assert [i["name"] for i in res["items"]] == ["p.jpg"]
    assert [d["name"] for d in res["subdirs"]] == ["sub"]


def test_log_studio_listdir_routes_remote(fs):
    s = fs([_ent("logs", is_dir=True), _ent("app.log", size=42, mtime=1.0)])
    res = logstudio._listdir("", REMOTE_DIR, src=s.src)
    names = {e["name"]: e for e in res["entries"]}
    assert names["logs"]["is_dir"] is True
    assert names["app.log"]["size"] == 42


def test_reader_unreachable_falls_back_to_kernel_local(tmp_path):
    # server unreachable -> presume local -> kernel listing of a real local dir
    (tmp_path / "sub").mkdir()
    (tmp_path / "z.tif").write_bytes(b"0")
    bad_src = "http://127.0.0.1:1/api/fs/raw?path=x"
    res = map_discover.main(dir=str(tmp_path), src=bad_src)
    names = {e["name"] for e in res["entries"]}
    assert "sub" in names and "z.tif" in names
