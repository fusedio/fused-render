"""fused-render:// deep-link routing in the platform-neutral supervisor core
(Task B, SPEC §26 D110). A deep link arrives as a plain Open("fused-render://…")
over the same wire protocol as a file path (Rust-compatible — no new opcode), so
core must distinguish it: never cwd-join it, and route it to the server's /clone
confirm page instead of a /view URL. Verified headlessly by capturing the URL
core would open."""
from pathlib import Path
from urllib.parse import quote

import pytest

try:
    from fused_render.supervisor import core, protocol
except Exception:  # noqa: BLE001 - no supervisor backend on this OS (e.g. darwin)
    core = None
    protocol = None

pytestmark = pytest.mark.skipif(core is None, reason="no supervisor backend on this OS")

DEEPLINK = "fused-render://open?git=https://github.com/fusedio/udfs/tree/main/public"


@pytest.fixture
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(core, "_open_browser", urls.append)
    return urls


def test_deep_link_is_not_cwd_joined():
    # _absolute_command cwd-joins relative Open paths — but a deep link is a URL,
    # not a path, and must pass through untouched.
    assert core._absolute_command(protocol.Open(DEEPLINK)) == protocol.Open(DEEPLINK)


def test_uppercase_scheme_is_also_a_deep_link():
    upper = "FUSED-RENDER://open?git=x"
    assert core._absolute_command(protocol.Open(upper)) == protocol.Open(upper)


def test_relative_file_path_still_cwd_joined():
    result = core._absolute_command(protocol.Open("sub/report.parquet"))
    assert isinstance(result, protocol.Open)
    assert Path(result.path).is_absolute()
    assert result.path == str(Path.cwd() / "sub" / "report.parquet")


def test_open_command_routes_deep_link_to_clone(opened):
    core._open_command(4242, protocol.Open(DEEPLINK))
    assert opened == ["http://127.0.0.1:4242/clone?src=" + quote(DEEPLINK, safe="")]


def test_open_command_routes_file_to_view(opened, tmp_path):
    from fused_render._view_url_codec import view_url

    f = tmp_path / "data.parquet"
    f.write_text("x")
    core._open_command(4242, protocol.Open(str(f)))
    assert opened == [view_url(4242, str(f))]


def test_open_command_missing_file_still_raises(opened, tmp_path):
    missing = tmp_path / "nope.parquet"
    with pytest.raises(FileNotFoundError):
        core._open_command(4242, protocol.Open(str(missing)))
    assert opened == []  # nothing opened on failure


def test_clone_url_path_shared_with_app():
    # app.py's macOS mapping and the shared codec must agree byte-for-byte.
    # The shared body is now open_target_path (which supersedes the branch's
    # clone_url_path); app.clone_url_path delegates to it.
    from fused_render._view_url_codec import open_target_path as shared
    from fused_render.app import clone_url_path as app_fn

    assert app_fn(DEEPLINK) == shared(DEEPLINK) == "/clone?src=" + quote(DEEPLINK, safe="")


# ---- file:// URI decode (GIO launchers pass %u as file:///path) --------------
# Nautilus/GNOME and other GIO-based launchers hand the .desktop `%u` field a
# `file:///…` URI, not a plain path. It must decode to a filesystem path
# (mirroring app.py's macOS handling): _absolute_command leaves the file: URI
# intact (it is a launch URL, not a path to cwd-join), and _open_command routes
# it through open_target_url which decodes it to a /view URL. This supersedes
# the branch's old _normalize_target, which decoded at _absolute_command time.


def test_file_uri_left_intact_by_absolute_command():
    # A file: URI is a launch URL — never cwd-joined; decode happens later.
    uri = "file:///tmp/data.parquet"
    assert core._absolute_command(protocol.Open(uri)) == protocol.Open(uri)


def test_file_uri_with_encoded_space_decoded_to_view_url(opened, tmp_path):
    from fused_render._view_url_codec import view_url

    f = tmp_path / "my report.parquet"  # space forces percent-encoding
    f.write_text("x")
    uri = "file://" + quote(str(f))  # e.g. file:///tmp/.../my%20report.parquet
    assert "%20" in uri

    cmd = core._absolute_command(protocol.Open(uri))
    core._open_command(4242, cmd)
    assert opened == [view_url(4242, str(f))]  # decoded to the file's /view URL


def test_file_uri_with_utf8_decoded(opened, tmp_path):
    from fused_render._view_url_codec import view_url

    f = tmp_path / "café.csv"
    f.write_text("x")
    uri = "file://" + quote(str(f))
    core._open_command(4242, core._absolute_command(protocol.Open(uri)))
    assert opened == [view_url(4242, str(f))]


def test_plain_absolute_path_unchanged_by_absolute_command():
    # A plain absolute path must not be mangled by file:// handling.
    p = str(Path("/home/user/data.csv"))
    assert core._absolute_command(protocol.Open(p)) == protocol.Open(p)


# ---- fused-render://launch (D128): ensure running, open NO tab ---------------
# The server-down banner's "Start fused-render" control is the action-only
# launch link. macOS (app.py) and Windows (winopen._open) ensure the app/server
# is up and open no tab; the Linux supervisor must match — routing it to /clone
# would open a spurious tab and surface an unsupported-link error.


def test_launch_url_opens_no_tab(opened):
    core._open_command(4242, protocol.Open("fused-render://launch"))
    assert opened == []


def test_opaque_launch_url_opens_no_tab(opened):
    # `fused-render:launch` (no authority) — some carriers strip the empty host.
    core._open_command(4242, protocol.Open("fused-render:launch"))
    assert opened == []


def test_launch_url_left_intact_by_absolute_command():
    # A launch link is a URL, not a path — never cwd-joined.
    launch = "fused-render://launch"
    assert core._absolute_command(protocol.Open(launch)) == protocol.Open(launch)


def test_clone_deep_link_still_routes_to_clone_after_launch_guard(opened):
    # The launch guard must not swallow the real clone deep link.
    core._open_command(4242, protocol.Open(DEEPLINK))
    assert opened == ["http://127.0.0.1:4242/clone?src=" + quote(DEEPLINK, safe="")]


# ---- host-bearing / non-file URLs must fail loudly, not open garbage ---------
# A file: URI naming a remote host, or any other scheme:// that is neither
# fused-render nor file, has no local filesystem path — decoding/falling
# through produced a garbage /view page. It must raise an OSError so
# _safe_open answers status 1 (→ the "FusedRender could not open" dialog on
# the forwarding side), exactly like a missing file does.


class _LogPaths:
    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(message)


def test_open_command_http_url_raises_and_opens_nothing(opened):
    with pytest.raises(OSError):
        core._open_command(4242, protocol.Open("https://example.com/x"))
    assert opened == []


def test_open_command_remote_file_uri_raises_and_opens_nothing(opened):
    with pytest.raises(OSError):
        core._open_command(4242, protocol.Open("file://server/share/x.parquet"))
    assert opened == []


def test_safe_open_answers_failure_for_http_url(opened):
    # Same surfacing as a missing file: _safe_open catches the OSError, logs,
    # and returns False (→ pipe status 1 → rejection dialog client-side).
    paths = _LogPaths()
    assert core._safe_open(4242, protocol.Open("https://example.com/x"), paths) is False
    assert opened == []
    assert paths.messages


def test_localhost_file_uri_still_opens(opened, tmp_path):
    from fused_render._view_url_codec import view_url

    f = tmp_path / "data.parquet"
    f.write_text("x")
    core._open_command(4242, protocol.Open("file://localhost" + quote(str(f))))
    assert opened == [view_url(4242, str(f))]
