"""Cloning a deployed page back to this machine (`023` §8.3, viewer half).

The archive is treated as hostile throughout, so most of these are refusal tests: the
happy path is one zip, and everything else is a way the far end could try to write outside
the workspace, exhaust the disk, or point the request somewhere it should not go.

No network: `_get` is the single seam every request goes through, so stubbing it exercises
the whole flow (URL derivation → status mapping → unpack → move) without a server. The
tests that *are* about the network stub one layer lower (`socket.getaddrinfo`,
`httpx.Client`) so the address and redirect guards are covered too.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from fused_render import app_clone, zip_import
from fused_render.server.app import create_app

FUSED = {"X-Fused": "1"}

PAGE_HTML = "<html><head></head><body><script>fused.runPython('./sine.py', {});</script></body></html>"
SINE = "def main(n: int = 1):\n    return n\n"


def _bundle_zip(
    *,
    name: str = "my-page",
    root: str = "files",
    page: str = "page.html",
    extra: dict[str, bytes] | None = None,
    manifest_over: dict | None = None,
    version: int = 2,
) -> bytes:
    """A v2 export bundle as the serve path's `_clone` emits it: manifest at the root,
    payload under `root`."""
    manifest = {
        "fused_render_bundle": version,
        "root": root,
        "page": page,
        "name": name,
        "entrypoints": [{"path": "./sine.py", "name": "sine", "key": "sine.py"}],
        "assets": [],
        "resources": [],
    }
    manifest.update(manifest_over or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"{root}/{page}", PAGE_HTML)
        zf.writestr(f"{root}/sine.py", SINE)
        for key, data in (extra or {}).items():
            zf.writestr(key, data)
    return buf.getvalue()


def _meta(*, name: str = "my-page", download_bytes: int | None = 420) -> bytes:
    body = {
        "clone": True,
        "name": name,
        "page": "page.html",
        "root": "files",
        "files": [
            {"path": "manifest.json", "bytes": 180},
            {"path": "files/page.html", "bytes": 120},
            {"path": "files/sine.py", "bytes": 30},
        ],
        "bytes": 330,
    }
    if download_bytes is not None:
        body["download_bytes"] = download_bytes
    return json.dumps(body).encode()


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.workspace = tmp_path / "Fused"
        monkeypatch.setenv("FUSED_RENDER_DIR", str(self.workspace))
        monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.requests: list[str] = []
        self.client = TestClient(create_app(start_dir=str(tmp_path)))
        self._monkeypatch = monkeypatch

    def serve(
        self,
        *,
        meta: bytes | None = None,
        archive: bytes | None = None,
        status: int = 200,
        etag: str | None = None,
    ):
        """Stub the one fetch seam: `?meta=1` gets `meta`, the bare URL gets `archive`.

        `etag` models the digest a current host sends with the archive; omitted, it models an
        older host that sends none (the protocol's advisory fields are additive)."""

        def _fake_get(url: str):
            self.requests.append(url)
            body = meta if url.endswith("?meta=1") else archive
            return app_clone._Fetched(
                status, body if body is not None else b"", None if url.endswith("?meta=1") else etag
            )

        self._monkeypatch.setattr(app_clone, "_get", _fake_get)

    def fail(self, status: int, content: bytes = b""):
        """Stub the seam to exercise the real status mapping."""

        def _fake_get(url: str):
            self.requests.append(url)
            app_clone._raise_for_clone_status(status, content)
            raise AssertionError("unreachable")

        self._monkeypatch.setattr(app_clone, "_get", _fake_get)


def _exhausted(*a, **k):
    raise zip_import.ZipRejected("could not find an unused folder name for 'my-page' in /w")


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# -- the URL contract ----------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "https://open.fused.io/my-link",
        "https://open.fused.io/my-link/",
        "https://open.fused.io/my-link/_shell",
        "https://open.fused.io/my-link/_clone",
        "  https://open.fused.io/my-link  ",
        "https://open.fused.io/my-link?utm_source=chat",
        "https://open.fused.io/my-link#section",
    ],
)
def test_every_shape_a_page_is_served_at_resolves_to_the_same_clone_url(src):
    # A user pastes whatever their address bar shows: the bare link, a trailing slash, the
    # shell path, or a link with tracking params someone's chat client added. All name the
    # same mount, so all must clone it — and the query/fragment must NOT ride along into
    # the request (a query string is also where a credential would hide).
    assert app_clone.clone_url_from(src) == "https://open.fused.io/my-link/_clone"


def test_the_org_scoped_url_form_is_preserved():
    # `{host}/{org}/{token}` is the custom-token form; stripping to one segment would clone
    # a different mount (or none).
    assert (
        app_clone.clone_url_from("https://open.fused.io/acme/dash")
        == "https://open.fused.io/acme/dash/_clone"
    )


@pytest.mark.parametrize(
    "src,expected",
    [
        ("", "paste the URL"),
        ("http://open.fused.io/my-link", "only https"),
        ("ftp://open.fused.io/my-link", "only https"),
        ("https://user:pw@open.fused.io/my-link", "embedded credentials"),
        ("https://open.fused.io/", "names no deployed page"),
        ("https://open.fused.io/_shell", "names no deployed page"),
    ],
)
def test_unusable_urls_are_refused_with_a_reason(src, expected):
    with pytest.raises(app_clone.CloneError, match=expected):
        app_clone.clone_url_from(src)


def test_http_is_refused_rather_than_upgraded():
    # Silently rewriting to https would hide that the user's link was insecure — and a
    # clone carries a page's source.
    with pytest.raises(app_clone.CloneError):
        app_clone.clone_url_from("http://open.fused.io/my-link")


# -- address guard (the one that matters in a desktop app) ---------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",  # loopback — the user's own services
        "10.1.2.3",  # private
        "192.168.1.10",  # private
        "169.254.169.254",  # cloud metadata
        "::1",  # loopback, v6
        "fd00::1",  # unique-local, v6
    ],
)
def test_hosts_resolving_to_non_public_addresses_are_refused(monkeypatch, addr):
    # This process sits inside the user's network, so an unchecked URL is a request their
    # browser could not make. Checked on the RESOLVED address, never the name: a hostname
    # under someone else's control can point anywhere.
    family = 10 if ":" in addr else 2
    monkeypatch.setattr(
        app_clone.socket,
        "getaddrinfo",
        lambda *a, **k: [(family, 1, 6, "", (addr, 443))],
    )
    with pytest.raises(app_clone.CloneError, match="non-public address"):
        app_clone._validated_address("evil.example")


def test_a_host_resolving_to_both_public_and_private_is_refused(monkeypatch):
    # DNS rebinding's cheap cousin: if any answer is private we cannot control which one
    # the connection uses, so the whole name is refused.
    monkeypatch.setattr(
        app_clone.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(app_clone.CloneError, match="non-public address"):
        app_clone._validated_address("split.example")


def test_a_public_host_passes(monkeypatch):
    monkeypatch.setattr(
        app_clone.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    # Returns the address it validated — the value `_get` dials, so the connection cannot
    # land on an answer that was never checked.
    assert app_clone._validated_address("open.fused.io") == "93.184.216.34"


def test_an_unresolvable_host_is_a_clean_error(monkeypatch):
    def _boom(*a, **k):
        raise app_clone.socket.gaierror("nope")

    monkeypatch.setattr(app_clone.socket, "getaddrinfo", _boom)
    with pytest.raises(app_clone.CloneError, match="could not resolve"):
        app_clone._validated_address("nx.example")


class _FakeStream:
    """Enough of a streamed `httpx.Response` for `_get`: status, redirect flag, chunks."""

    def __init__(self, status_code=200, content=b"", is_redirect=False, headers=None):
        self.status_code = status_code
        self._content = content
        self.is_redirect = is_redirect
        # `_get` reads the ETag off the response (the archive's digest), so the double has to
        # carry headers.
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._content


class _FakeClient:
    """Records what `_get` actually dialled. Instances land in `calls`, which the tests
    read — the whole point is the connect URL, not the body."""

    calls: list[dict] = []

    def __init__(self, *, timeout=None, follow_redirects=None):
        self.follow_redirects = follow_redirects

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, *, headers=None, extensions=None):
        _FakeClient.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "extensions": extensions or {},
                "follow_redirects": self.follow_redirects,
            }
        )
        return _FakeStream(content=b"zipbytes")


@pytest.fixture
def dialled(monkeypatch):
    """Stub one layer below `_get`: DNS answers one address, httpx records the connect."""
    _FakeClient.calls = []
    monkeypatch.setattr(
        app_clone.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    monkeypatch.setattr(app_clone.httpx, "Client", _FakeClient)
    return _FakeClient.calls


def test_the_request_dials_the_validated_address_not_the_name(dialled):
    # The DNS-rebinding fix: validating a NAME and then handing the name to httpx lets the
    # client resolve again at connect time, so an attacker can answer public for the check
    # and loopback for the connection. `_get` must dial the address that was checked.
    fetched = app_clone._get("https://open.fused.io/my-link/_clone")
    assert fetched.content == b"zipbytes"
    call = dialled[-1]
    assert call["url"] == "https://93.184.216.34/my-link/_clone"
    # ...while the hostname still travels, so pinning the address costs no authentication:
    # `sni_hostname` drives SNI *and* certificate verification, and `Host` routes at the
    # far end.
    assert call["extensions"]["sni_hostname"] == "open.fused.io"
    assert call["headers"]["host"] == "open.fused.io"
    assert call["follow_redirects"] is False


def test_a_v6_address_is_dialled_in_brackets_with_the_port_kept(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(
        app_clone.socket, "getaddrinfo", lambda *a, **k: [(10, 1, 6, "", ("2606:2800:220::1", 443))]
    )
    monkeypatch.setattr(app_clone.httpx, "Client", _FakeClient)
    app_clone._get("https://open.fused.io:8443/p/_clone")
    call = _FakeClient.calls[-1]
    # A bare v6 literal in a URL is unparseable, and dropping the port would silently
    # retarget the request at 443.
    assert call["url"] == "https://[2606:2800:220::1]:8443/p/_clone"
    assert call["headers"]["host"] == "open.fused.io:8443"


def test_a_redirect_is_refused_rather_than_followed(dialled, monkeypatch):
    # Every other guard applies to the URL we validated; a followed redirect would apply
    # none of them to wherever the request landed.
    monkeypatch.setattr(
        _FakeClient,
        "stream",
        lambda self, *a, **k: _FakeStream(status_code=302, is_redirect=True),
    )
    with pytest.raises(app_clone.CloneError, match="redirected"):
        app_clone._get("https://open.fused.io/my-link/_clone")


# -- status mapping ------------------------------------------------------------


def test_a_404_says_both_possibilities(h):
    # The gate deliberately cannot distinguish "cloning is off" from "no such mount" (it
    # must not confirm the capability), so the message must not guess one — a client that
    # picks wrong sends the user chasing the wrong problem.
    h.fail(404)
    resp = h.client.get("/api/clone-app/info", params={"src": "https://x.example/p"})
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert "not enabled cloning" in body and "URL is wrong" in body


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "requires sign-in"),
        (403, "requires sign-in"),
        (410, "taken down"),
        (429, "rate-limiting"),
        (500, "HTTP 500"),
    ],
)
def test_server_statuses_map_to_actionable_messages(h, status, expected):
    h.fail(status)
    resp = h.client.get("/api/clone-app/info", params={"src": "https://x.example/p"})
    assert resp.status_code == 400
    assert expected in resp.json()["error"]


def test_a_413_passes_the_hosts_own_reason_through(h):
    # The server names the real size; restating it in our own words would lose the number
    # the publisher needs.
    h.fail(413, json.dumps({"error": "this page's bundle is too large (9000 > 5242880)"}).encode())
    resp = h.client.get("/api/clone-app/info", params={"src": "https://x.example/p"})
    assert "9000 > 5242880" in resp.json()["error"]


def test_a_plain_text_error_body_is_also_surfaced(h):
    # The AWS plane answers 413 as text/plain, the managed one as JSON — both are read.
    h.fail(413, b"this page's bundle is too large (9000 > 5242880)")
    assert "9000" in h.client.get(
        "/api/clone-app/info", params={"src": "https://x.example/p"}
    ).json()["error"]


# -- preview -------------------------------------------------------------------


def test_the_preview_describes_what_the_clone_will_do(h):
    h.serve(meta=_meta())
    body = h.client.get(
        "/api/clone-app/info", params={"src": "https://open.fused.io/my-link"}
    ).json()
    assert body["url"] == "https://open.fused.io/my-link/_clone"
    assert h.requests == ["https://open.fused.io/my-link/_clone?meta=1"]
    assert body["name"] == "my-page"
    assert [f["path"] for f in body["files"]] == [
        "manifest.json",
        "files/page.html",
        "files/sine.py",
    ]
    assert body["bytes"] == 330
    assert body["download_bytes"] == 420
    assert body["folder"] == "my-page"
    assert body["renamed"] is False
    assert body["dest"] == os.path.join(str(h.workspace), "my-page")


def test_the_preview_reports_the_collision_safe_name_it_will_use(h):
    # The confirm step must name the folder the clone ACTUALLY lands in, or the user goes
    # looking for "my-page" and finds their unrelated existing folder instead.
    (h.workspace / "my-page").mkdir()
    h.serve(meta=_meta())
    body = h.client.get(
        "/api/clone-app/info", params={"src": "https://open.fused.io/my-link"}
    ).json()
    assert body["folder"] == "my-page-2"
    assert body["renamed"] is True


def test_a_missing_download_size_is_reported_as_absent_not_guessed(h):
    # An older serve path does not report it; printing a made-up number would be worse
    # than printing none.
    h.serve(meta=_meta(download_bytes=None))
    body = h.client.get(
        "/api/clone-app/info", params={"src": "https://open.fused.io/my-link"}
    ).json()
    assert body["download_bytes"] is None


def test_a_non_clone_json_body_is_refused(h):
    h.serve(meta=json.dumps({"entrypoints": {"run": "x"}}).encode())
    resp = h.client.get("/api/clone-app/info", params={"src": "https://open.fused.io/my-link"})
    assert resp.status_code == 400
    assert "not a clonable" in resp.json()["error"]


def test_an_unreadable_inventory_is_refused(h):
    h.serve(meta=b"<html>not json</html>")
    resp = h.client.get("/api/clone-app/info", params={"src": "https://open.fused.io/my-link"})
    assert "readable clone inventory" in resp.json()["error"]


# -- clone ---------------------------------------------------------------------


def test_a_clone_lands_as_an_openable_local_page(h):
    h.serve(archive=_bundle_zip())
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dest = body["dest"]
    # The payload dir BECOMES the page folder: page.html beside its runPython target, at
    # their real page-relative paths — an ordinary local page, no rewriting.
    assert os.path.isfile(os.path.join(dest, "page.html"))
    assert os.path.isfile(os.path.join(dest, "sine.py"))
    assert not os.path.exists(os.path.join(dest, "files"))
    # The manifest rides along as a dotfile so a re-export can reproduce the same bundle.
    assert os.path.isfile(os.path.join(dest, ".fused-render-bundle.json"))
    assert body["page"] == os.path.join(dest, "page.html")
    assert body["view"].startswith("/view/")
    assert h.requests == ["https://open.fused.io/my-link/_clone"]


def test_the_clone_lands_in_the_folder_the_preview_promised(h, tmp_path):
    # CL-1: the preview writes nothing, so it cannot reserve the name — the client passes it
    # back instead. Without that, a page appearing in the workspace between the two calls
    # would silently move the clone: `my-page` is free at preview time, so the confirm button
    # says "Clone to my-page", and it must still land there even though a LATER-created
    # sibling would otherwise shift the derived name.
    h.serve(meta=_meta(), archive=_bundle_zip())
    preview = h.client.get(
        "/api/clone-app/info", params={"src": "https://open.fused.io/my-link"}
    ).json()
    assert preview["folder"] == "my-page"
    resp = h.client.post(
        "/api/clone-app",
        json={"src": "https://open.fused.io/my-link", "folder": preview["folder"]},
        headers=FUSED,
    )
    assert resp.json()["folder"] == "my-page"


def test_a_promised_folder_that_is_taken_by_then_falls_back_and_reports_it(h):
    # The race the carry-through cannot close: honouring a name that now exists would
    # overwrite or merge (CL-2), so the derived name wins and the RESPONSE is authoritative.
    (h.workspace / "my-page").mkdir()
    h.serve(archive=_bundle_zip())
    resp = h.client.post(
        "/api/clone-app",
        json={"src": "https://open.fused.io/my-link", "folder": "my-page"},
        headers=FUSED,
    )
    assert resp.json()["folder"] == "my-page-2"


def test_a_destination_appearing_mid_commit_is_never_nested_into(h, monkeypatch):
    """The commit claims the name by RENAMING, so a folder that appears between the
    prediction and the write cannot swallow the payload.

    `shutil.move` onto an existing directory moves the source *inside* it — the clone would
    land at `my-page/my-page/page.html` while the response said `my-page`, and the reported
    `view` path would 404. The rename fails instead, and the next name is used.
    """
    real_rename = app_clone.os.rename
    first = {"done": False}

    def _someone_else_got_there(src, dst):
        # Simulate the race precisely: the first candidate becomes a NON-EMPTY directory
        # (an empty one is legitimately replaceable) an instant before the rename.
        if not first["done"]:
            first["done"] = True
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, "someone-elses-file"), "w") as f:
                f.write("mine")
        return real_rename(src, dst)

    monkeypatch.setattr(app_clone.os, "rename", _someone_else_got_there)
    h.serve(archive=_bundle_zip())
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    body = resp.json()
    assert body["folder"] == "my-page-2"
    # The page is where the response says it is, and the squatter is untouched.
    assert os.path.isfile(body["page"])
    assert (h.workspace / "my-page" / "someone-elses-file").read_text() == "mine"
    assert not (h.workspace / "my-page" / "page.html").exists()


def test_running_out_of_folder_names_is_a_message_not_a_500(h, monkeypatch):
    # `unique_dir`/`move_into_new_dir` raise ZipRejected on exhaustion; unconverted it would
    # reach the route as an unhandled 500 that names neither the cause nor the remedy.
    monkeypatch.setattr(app_clone.zip_import, "unique_dir", _exhausted)
    monkeypatch.setattr(app_clone.zip_import, "move_into_new_dir", _exhausted)
    h.serve(meta=_meta(), archive=_bundle_zip())
    preview = h.client.get(
        "/api/clone-app/info", params={"src": "https://open.fused.io/my-link"}
    )
    assert preview.status_code == 400
    assert "unused folder name" in preview.json()["error"]
    cloned = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert cloned.status_code == 400
    assert "unused folder name" in cloned.json()["error"]


def test_a_hostile_promised_folder_cannot_steer_where_the_clone_lands(h, tmp_path):
    # `folder` arrives from a client, so it is reduced like any other untrusted name: one
    # segment, no separators, no traversal.
    outside = tmp_path / "outside"
    outside.mkdir()
    h.serve(archive=_bundle_zip())
    resp = h.client.post(
        "/api/clone-app",
        json={"src": "https://open.fused.io/my-link", "folder": "../../outside/pwned"},
        headers=FUSED,
    )
    dest = resp.json()["dest"]
    assert os.path.dirname(os.path.abspath(dest)) == str(h.workspace)
    assert sorted(p.name for p in outside.iterdir()) == []


def test_a_second_clone_of_the_same_page_never_overwrites_the_first(h):
    # An archive carries no identity we can verify, so there is no "update in place" here
    # (unlike the git flow) — landing on top of an existing folder would be data loss.
    h.serve(archive=_bundle_zip())
    first = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    ).json()
    with open(os.path.join(first["dest"], "page.html"), "w", encoding="utf-8") as f:
        f.write("MY LOCAL EDITS")
    second = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    ).json()
    assert second["dest"] != first["dest"]
    assert second["folder"] == "my-page-2"
    with open(os.path.join(first["dest"], "page.html"), encoding="utf-8") as f:
        assert f.read() == "MY LOCAL EDITS"


def test_the_clone_endpoint_requires_the_shell_guard(h):
    h.serve(archive=_bundle_zip())
    resp = h.client.post("/api/clone-app", json={"src": "https://open.fused.io/my-link"})
    assert resp.status_code == 403
    assert "X-Fused" in resp.json()["error"]


def test_a_newer_clone_protocol_is_refused_with_an_upgrade_message(h):
    """Compatibility is read from the inventory's stated version, not sniffed from the archive.

    `fused` owns the artifact's layout, so a client that unpacked a format it does not
    understand would either write a broken page or — worse — one that looks fine. The refusal
    names the remedy, since "update fused-render" is something the user can act on.
    """
    body = json.loads(_meta())
    body["protocol"] = 99
    h.serve(meta=json.dumps(body).encode())
    resp = h.client.get("/api/clone-app/info", params={"src": "https://open.fused.io/p"})
    assert resp.status_code == 400
    assert "newer clone format" in resp.json()["error"]


def test_a_bare_post_still_refuses_an_unimportable_archive(h):
    """The enforcing gate is on the ARCHIVE, so it holds with no preview at all.

    `POST /api/clone-app` can be called directly — a programmatic client, or the modal after a
    preview the user has since edited away from — so a compatibility check that lived only in
    the inventory path would be no gate at all. This reads the version out of the bytes actually
    downloaded, which needs nothing upstream to be present or trusted.
    """
    h.serve(archive=_bundle_zip(version=3))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "newer clone format" in resp.json()["error"]
    # `?meta=1` was never fetched: the refusal came from the archive, not from an inventory.
    assert not any(u.endswith("?meta=1") for u in h.requests)
    assert sorted(p.name for p in h.workspace.iterdir() if not p.name.startswith(".")) == []


def test_the_two_compatibility_checks_read_one_table(h):
    # The inventory check is an early refusal (before megabytes move) and the archive check is
    # the enforcing one. They must agree, so they resolve the same mapping rather than each
    # carrying its own list.
    assert app_clone.SUPPORTED_CLONE_PROTOCOLS == frozenset(app_clone._PROTOCOL_BUNDLE_VERSION)
    assert app_clone.SUPPORTED_BUNDLE_VERSIONS == frozenset(
        app_clone._PROTOCOL_BUNDLE_VERSION.values()
    )


def test_the_current_protocol_and_a_host_that_states_none_both_import(h):
    # Protocol 1 is what this client speaks; a host predating the field is an older plane, and
    # a weaker guarantee is not an error — the bundle's own version check still applies there.
    for meta_body in ({**json.loads(_meta()), "protocol": 1}, json.loads(_meta())):
        h.serve(meta=json.dumps(meta_body).encode(), archive=_bundle_zip())
        resp = h.client.get("/api/clone-app/info", params={"src": "https://open.fused.io/p"})
        assert resp.status_code == 200, resp.json()


def test_a_download_that_fails_its_published_checksum_is_refused(h):
    """Bytes that arrive complete but WRONG are the one failure a length check cannot see.

    The archive's `ETag` is the digest `fused` computed over the bytes it assembled, so this is
    an end-to-end check across whatever proxied them — and it must fire before the zip is
    opened, so a corrupted archive never reaches the workspace.
    """
    h.serve(archive=_bundle_zip(), etag="sha256:" + "0" * 64)
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "did not match the checksum" in resp.json()["error"]
    assert sorted(p.name for p in h.workspace.iterdir() if not p.name.startswith(".")) == []


def test_a_download_matching_its_checksum_imports(h):
    archive = _bundle_zip()
    h.serve(archive=archive, etag="sha256:" + hashlib.sha256(archive).hexdigest())
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.json()
    assert os.path.isfile(resp.json()["page"])


@pytest.mark.parametrize("etag", [None, '"opaque-etag"', "md5:abc", '""', '"'])
def test_an_absent_or_unrecognised_digest_does_not_block_an_import(h, etag):
    # An older host sends no ETag, and a proxy may send an opaque one. Treating either as a
    # failure would make this client reject valid clones — the digest strengthens the import
    # when present rather than gating it.
    h.serve(archive=_bundle_zip(), etag=etag)
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.json()


@pytest.mark.parametrize(
    "etag",
    [
        '"sha256:{d}"',  # the compliant spelling: RFC 9110 §8.8.3 entity-tags are quoted
        "W/\"sha256:{d}\"",  # weak validator — unwrapped, since the digest still decides
        'w/"sha256:{d}"',  # ...case-insensitively, per the grammar's tolerance in the wild
        "sha256:{d}",  # the bare form an early build of the serve path emitted
        '  "sha256:{d}"  ',  # header whitespace
    ],
)
def test_every_spelling_of_a_published_digest_is_actually_checked(h, etag):
    """The bug this guards: a QUOTED tag matched against a BARE prefix parses as "no digest",
    so verification is skipped and a mismatched archive imports while reporting success.

    Each spelling is asserted to reach a *verdict* — a wrong digest must be refused — because
    "the import succeeded" proves nothing here: it is exactly what a skipped check looks like.
    """
    wrong = "0" * 64
    h.serve(archive=_bundle_zip(), etag=etag.format(d=wrong))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400, f"verification skipped on {etag!r}"
    assert "did not match the checksum" in resp.json()["error"]

    # ...and the same spelling with the RIGHT digest imports.
    archive = _bundle_zip()
    h.serve(archive=archive, etag=etag.format(d=hashlib.sha256(archive).hexdigest()))
    ok = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert ok.status_code == 200, ok.json()


def test_the_compliant_etag_the_serve_path_emits_is_the_one_we_parse():
    """Pins the exact wire spelling `fused`'s `clone_etag` produces (its own suite asserts the
    same literal), since this repo cannot import it — the viewer runs with no `fused` installed.
    A drift on either side silently disables verification rather than failing loudly."""
    payload = b"archive-bytes"
    emitted = f'"sha256:{hashlib.sha256(payload).hexdigest()}"'
    assert app_clone._digest_from_etag(emitted) == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    app_clone._verify_digest(payload, emitted)  # no raise
    with pytest.raises(app_clone.CloneError, match="did not match the checksum"):
        app_clone._verify_digest(payload + b"x", emitted)


def test_a_download_that_is_not_a_zip_is_refused(h):
    h.serve(archive=b"<html>not a zip</html>")
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "not a valid .zip" in resp.json()["error"]


# -- hostile archives ----------------------------------------------------------


def test_a_zip_slip_entry_is_refused_and_writes_nothing(h, tmp_path):
    outside = tmp_path / "pwned.txt"
    h.serve(archive=_bundle_zip(extra={"../../pwned.txt": b"owned"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "path escape" in resp.json()["error"]
    assert not outside.exists()
    # And nothing partial in the workspace.
    assert [p for p in os.listdir(h.workspace) if not p.startswith(".")] == []


def test_an_absolute_path_entry_is_refused(h):
    h.serve(archive=_bundle_zip(extra={"/etc/pwned": b"owned"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "absolute path" in resp.json()["error"]


def test_a_symlink_entry_is_refused(h):
    # Extracting one lets a later write go through it to anywhere the process can reach.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"fused_render_bundle": 2, "root": "files", "page": "page.html"}))
        info = zipfile.ZipInfo("files/link")
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        zf.writestr(info, "/etc/passwd")
    h.serve(archive=buf.getvalue())
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "symlink entry not allowed" in resp.json()["error"]


def test_a_manifest_root_pointing_outside_the_bundle_is_refused(h, tmp_path):
    # The ENTRIES can all be safe while the manifest's own `root` points elsewhere — and
    # `root` is what gets MOVED, so it needs its own containment check.
    h.serve(archive=_bundle_zip(manifest_over={"root": "../../escape"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "may not contain '..'" in resp.json()["error"]


def test_a_manifest_page_pointing_outside_the_payload_is_refused(h):
    h.serve(archive=_bundle_zip(manifest_over={"page": "../manifest.json"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "may not contain '..'" in resp.json()["error"]


@pytest.mark.parametrize("root", [".", "./", "files/.."])
def test_a_manifest_root_naming_the_bundle_itself_is_refused(h, root):
    # `root` is the directory `clone` MOVES, so accepting the staging dir itself broke the
    # staged-then-move guarantee: the move vacated the path `manifest.json` was still
    # expected at, raising an uncaught FileNotFoundError and leaving a half-built clone in
    # the workspace. `root` must name a real child directory.
    h.serve(archive=_bundle_zip(manifest_over={"root": root}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert "must name a folder inside the bundle" in error or "may not contain '..'" in error
    # Nothing landed: the refusal happens before either move (`.clone-staging` is the
    # staging root, swept of its contents but not itself removed).
    assert sorted(p.name for p in h.workspace.iterdir() if not p.name.startswith(".")) == []


def test_a_failed_manifest_move_rolls_the_payload_move_back(h, monkeypatch):
    # The commit is two moves. The second one failing would otherwise leave a clone in the
    # workspace that this call reports as failed — the one state stage-then-move exists to
    # prevent.
    # The payload is committed by rename (`move_into_new_dir`); the manifest is the one
    # remaining `shutil.move`, so failing it is failing the second half of the commit.
    def _manifest_move_fails(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(app_clone.shutil, "move", _manifest_move_fails)
    h.serve(archive=_bundle_zip())
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "could not finish writing the clone" in resp.json()["error"]
    # Rolled back: no page folder in the workspace, so a failed call leaves nothing that
    # looks like a real clone.
    assert sorted(p.name for p in h.workspace.iterdir() if not p.name.startswith(".")) == []


def test_an_absolute_manifest_root_is_refused(h):
    h.serve(archive=_bundle_zip(manifest_over={"root": "/etc"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert "must be a relative path" in resp.json()["error"]


def test_a_v1_bundle_is_refused_with_a_version_message(h):
    # v1 carries no `root`/payload layout, so there is nothing to unpack as a page.
    h.serve(archive=_bundle_zip(version=1))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert "unsupported bundle format" in resp.json()["error"]


def test_a_bundle_without_a_manifest_is_refused(h):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("files/page.html", PAGE_HTML)
    h.serve(archive=buf.getvalue())
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert "missing its manifest.json" in resp.json()["error"]


def test_an_oversized_entry_is_refused_on_bytes_actually_written(h, monkeypatch):
    # The cap is enforced while decompressing, never from the declared size — a crafted
    # zip can understate that field, which is how a "25 MB cap" extracts gigabytes.
    monkeypatch.setattr(app_clone, "MAX_ENTRY_UNCOMPRESSED", 512)
    h.serve(archive=_bundle_zip(extra={"files/big.bin": b"\0" * 4096}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"]
    assert [p for p in os.listdir(h.workspace) if not p.startswith(".")] == []


def test_too_many_entries_is_refused(h, monkeypatch):
    monkeypatch.setattr(app_clone, "MAX_ENTRIES", 3)
    h.serve(archive=_bundle_zip(extra={f"files/f{n}.txt": b"x" for n in range(5)}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert "too many entries" in resp.json()["error"]


def test_a_hostile_app_name_cannot_steer_where_the_clone_lands(h, tmp_path):
    # `name` comes from the far end and becomes a directory name.
    h.serve(archive=_bundle_zip(manifest_over={"name": "../../../etc/cron.d/x"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.text
    dest = resp.json()["dest"]
    assert os.path.dirname(dest) == str(h.workspace)
    assert ".." not in resp.json()["folder"]


def test_an_empty_app_name_still_yields_a_usable_folder(h):
    h.serve(archive=_bundle_zip(manifest_over={"name": "///"}))
    resp = h.client.post(
        "/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED
    )
    assert resp.json()["folder"] == "cloned-page"


def test_staging_is_cleaned_up_on_success_and_on_failure(h):
    h.serve(archive=_bundle_zip())
    h.client.post("/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED)
    h.serve(archive=_bundle_zip(extra={"../../pwned.txt": b"x"}))
    h.client.post("/api/clone-app", json={"src": "https://open.fused.io/my-link"}, headers=FUSED)
    staging = os.path.join(str(h.workspace), ".clone-staging")
    assert not os.path.isdir(staging) or os.listdir(staging) == []


# -- the shared unpack guards --------------------------------------------------


def test_unique_dir_allocates_in_order_and_gives_up_loudly(tmp_path):
    assert zip_import.unique_dir(str(tmp_path), "p") == os.path.join(str(tmp_path), "p")
    (tmp_path / "p").mkdir()
    assert zip_import.unique_dir(str(tmp_path), "p") == os.path.join(str(tmp_path), "p-2")
    (tmp_path / "p-2").mkdir()
    assert zip_import.unique_dir(str(tmp_path), "p") == os.path.join(str(tmp_path), "p-3")
    # Bounded rather than looping forever, and it says what to do about it.
    (tmp_path / "p-3").mkdir()
    with pytest.raises(zip_import.ZipRejected, match="could not find an unused folder name"):
        zip_import.unique_dir(str(tmp_path), "p", limit=3)
