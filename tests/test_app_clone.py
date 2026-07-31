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

    def serve(self, *, meta: bytes | None = None, archive: bytes | None = None, status: int = 200):
        """Stub the one fetch seam: `?meta=1` gets `meta`, the bare URL gets `archive`."""

        def _fake_get(url: str):
            self.requests.append(url)
            body = meta if url.endswith("?meta=1") else archive
            return app_clone._Fetched(status, body if body is not None else b"")

        self._monkeypatch.setattr(app_clone, "_get", _fake_get)

    def fail(self, status: int, content: bytes = b""):
        """Stub the seam to exercise the real status mapping."""

        def _fake_get(url: str):
            self.requests.append(url)
            app_clone._raise_for_clone_status(status, content)
            raise AssertionError("unreachable")

        self._monkeypatch.setattr(app_clone, "_get", _fake_get)


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
        app_clone._assert_public_address("evil.example")


def test_a_host_resolving_to_both_public_and_private_is_refused(monkeypatch):
    # DNS rebinding's cheap cousin: if any answer is private we cannot control which one
    # the connection uses, so the whole name is refused.
    monkeypatch.setattr(
        app_clone.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(app_clone.CloneError, match="non-public address"):
        app_clone._assert_public_address("split.example")


def test_a_public_host_passes(monkeypatch):
    monkeypatch.setattr(
        app_clone.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    app_clone._assert_public_address("open.fused.io")  # no raise


def test_an_unresolvable_host_is_a_clean_error(monkeypatch):
    def _boom(*a, **k):
        raise app_clone.socket.gaierror("nope")

    monkeypatch.setattr(app_clone.socket, "getaddrinfo", _boom)
    with pytest.raises(app_clone.CloneError, match="could not resolve"):
        app_clone._assert_public_address("nx.example")


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
