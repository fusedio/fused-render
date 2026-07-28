"""/api/fs/raw must not hand a page the app's own origin.

The route serves any absolute path with a content-type guessed from its name,
as a plain GET with no X-Fused — so it is top-level navigable, and navigation
is not subject to CORS. A foreign site can point the browser at a .html file it
arranged to be on disk (a drive-by download into ~/Downloads names the file for
it) and that file then runs as a FIRST-PARTY document on 127.0.0.1:<port>,
inside the trust boundary, free to send X-Fused: 1 to /api/run.

D4 concedes that an .html file *you open* runs same-origin — that is the user
choosing the file. Here the attacker chooses it.

So: nosniff always, and scriptable types downgraded to text/plain when (and
only when) the request is a document load. The route has four exits — HEAD, the
307 to the store, the proxied mount read, and the local file — and three of
them can carry a scriptable type, so each is covered here; the mount-backed
proxy in particular is the one a fix aimed only at the local FileResponse
would miss.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import fused_render.shell.mounts as shell_mounts
import fused_render.shell.prefetch as shell_prefetch
from fused_render.server import create_app


NAV = {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate"}
SUBRESOURCE = {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}

HTML = "<script>fetch('/api/run',{method:'POST',headers:{'X-Fused':'1'}})</script>"
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def _ctype(resp):
    return resp.headers.get("content-type", "").split(";")[0].strip().lower()


# ---------------------------------------------------------- the local file

def test_navigating_to_a_local_html_file_does_not_get_a_document(client, tmp_path):
    """The attack: window.open('.../api/fs/raw?path=/…/evil.html')."""
    p = _write(tmp_path, "evil.html", HTML)
    r = client.get("/api/fs/raw", params={"path": p}, headers=NAV)

    assert r.status_code == 200
    assert _ctype(r) == "text/plain"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.text == HTML  # the bytes are untouched; only the framing changes


def test_the_same_file_as_a_subresource_keeps_its_type(client, tmp_path):
    """Templates fetch this endpoint as data. Coercing there would break
    working callers to fix a threat that only exists for document loads."""
    p = _write(tmp_path, "page.html", HTML)
    r = client.get("/api/fs/raw", params={"path": p}, headers=SUBRESOURCE)

    assert _ctype(r) == "text/html"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_an_svg_in_an_img_tag_is_left_alone(client, tmp_path):
    """SVG in <img> cannot execute script — and the shell really does load
    template icons this way (ModeSwitcher's CSS mask). Pins the coercion
    against being widened to every request."""
    p = _write(tmp_path, "icon.svg", SVG)
    r = client.get("/api/fs/raw", params={"path": p},
                   headers={"sec-fetch-dest": "image", "sec-fetch-mode": "no-cors"})

    assert _ctype(r) == "image/svg+xml"


@pytest.mark.parametrize("dest", ["document", "iframe", "frame", "embed", "object"])
def test_every_document_destination_is_covered(client, tmp_path, dest):
    """Framing is the same capability as navigating: the framed document lands
    on this origin either way."""
    p = _write(tmp_path, "f.html", HTML)
    r = client.get("/api/fs/raw", params={"path": p},
                   headers={"sec-fetch-dest": dest})
    assert _ctype(r) == "text/plain"


def test_svg_navigated_is_coerced(client, tmp_path):
    """Script inside an SVG *does* run when the SVG is the document."""
    p = _write(tmp_path, "x.svg", SVG)
    assert _ctype(client.get("/api/fs/raw", params={"path": p},
                             headers=NAV)) == "text/plain"


def test_non_scriptable_types_keep_their_content_type(client, tmp_path):
    """Only the executable types are downgraded — a navigated .json or .csv
    must still describe itself honestly."""
    p = _write(tmp_path, "d.json", '{"a":1}')
    r = client.get("/api/fs/raw", params={"path": p}, headers=NAV)

    assert _ctype(r) == "application/json"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_head_is_hardened_too(client, tmp_path):
    """Ranged clients probe with HEAD first, and it has its own response path
    with its own guess_type call."""
    p = _write(tmp_path, "h.html", HTML)
    r = client.head("/api/fs/raw", params={"path": p}, headers=NAV)

    assert r.status_code == 200
    assert _ctype(r) == "text/plain"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_missing_file_still_404s(client, tmp_path):
    """The wrapper must not change the route's error behaviour."""
    r = client.get("/api/fs/raw", params={"path": str(tmp_path / "nope.html")},
                   headers=NAV)
    assert r.status_code == 404


# ------------------------------------------------- the mount-backed proxy

@pytest.fixture()
def stub_serve():
    """Stands in for a mount's rclone `serve http`, which answers with its own
    content-type — the header the proxy forwards via _PROXY_HEADERS."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_a_proxied_mount_read_is_hardened(client, tmp_path, stub_serve,
                                          monkeypatch):
    """The exit a fix aimed at the local FileResponse would miss: bytes coming
    back from rclone serve carry ITS content-type straight through the proxy,
    so an .html in a mounted bucket is just as navigable."""
    p = _write(tmp_path, "in_bucket.html", HTML)
    monkeypatch.setattr(shell_mounts, "serve_url_for", lambda path: stub_serve)
    # Warm, so the read is proxied rather than 307'd to the store.
    monkeypatch.setattr(shell_prefetch, "is_done", lambda path: True)
    monkeypatch.setattr(shell_prefetch, "schedule", lambda *a, **k: None)

    r = client.get("/api/fs/raw", params={"path": p}, headers=NAV)

    assert r.status_code == 200
    assert r.text == HTML          # really came from the stub serve
    assert _ctype(r) == "text/plain"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_307_to_the_object_store_is_untouched(client, tmp_path, stub_serve,
                                                  monkeypatch):
    """The redirect points at a foreign origin (S3/GCS) and is already gated to
    non-browser clients, so it is out of scope — and rewriting a redirect's
    headers would be meddling with a path the readers depend on."""
    p = _write(tmp_path, "big.parquet", "x")
    monkeypatch.setattr(shell_mounts, "serve_url_for", lambda path: stub_serve)
    monkeypatch.setattr(shell_mounts, "upstream_url_for",
                        lambda path: "https://store.example/signed")
    monkeypatch.setattr(shell_prefetch, "is_done", lambda path: False)
    monkeypatch.setattr(shell_prefetch, "schedule", lambda *a, **k: None)

    # No Sec-Fetch-* at all: that absence is how the route recognises a native
    # ranged client (duckdb httpfs) rather than a browser.
    r = client.get("/api/fs/raw", params={"path": p}, follow_redirects=False)

    assert r.status_code == 307
    assert r.headers["location"] == "https://store.example/signed"
    assert "x-content-type-options" not in r.headers
