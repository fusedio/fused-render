"""fused-render:// deep-link routing in the platform-neutral supervisor core
(Task B, SPEC §26 D110). A deep link arrives as a plain Open("fused-render://…")
over the same wire protocol as a file path (Rust-compatible — no new opcode), so
core must distinguish it: never cwd-join it, and route it to the server's /clone
confirm page instead of a /view URL. Verified headlessly by capturing the URL
core would open."""
from pathlib import Path
from urllib.parse import quote

import pytest

from fused_render.supervisor import core, protocol

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
    from fused_render._view_url_codec import clone_url_path as shared
    from fused_render.app import clone_url_path as app_fn

    assert app_fn(DEEPLINK) == shared(DEEPLINK) == "/clone?src=" + quote(DEEPLINK, safe="")
