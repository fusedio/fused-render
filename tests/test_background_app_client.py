"""Tests for `fused_render/templates/shared/background_app.py` — a background
daemon's client for the background-apps API, about itself (SPEC.md §46,
D505).

Same style as `test_fused_ai_client.py`: loaded the way production loads it
(shared dir on `sys.path`, then a plain `import`), HTTP mocked throughout
(`urllib.request.urlopen`), no real socket and no real daemon.
"""
import io
import json
import os
import sys
import urllib.error

import pytest

_SHARED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import appenv  # noqa: E402 - path seeded above, matching production
import background_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Never read a real `~/.fused-render/server.json`, and never leak
    `FUSED_RENDER_APP_DIR` in from the dev machine's own environment."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.delenv(background_app.APP_DIR_ENV, raising=False)


def _write_server_json(path, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fields, f)


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        super().__init__("http://x", code, "err", {}, io.BytesIO(body))
        self._body = body

    def read(self):
        return self._body


# ------------------------------------------------------------- origin lookup


def test_env_origin_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:9999")
    _write_server_json(background_app._server_json_path(),
                       origin="http://127.0.0.1:1234")
    assert background_app.resolve_origin() == "http://127.0.0.1:9999"


def test_a_missing_file_and_no_env_raises_server_not_running():
    with pytest.raises(background_app.ServerNotRunning):
        background_app.resolve_origin()


def test_a_stale_unreachable_origin_in_the_file_raises():
    _write_server_json(background_app._server_json_path(),
                       origin="http://127.0.0.1:1")  # nothing listens on :1
    with pytest.raises(background_app.ServerNotRunning):
        background_app.resolve_origin()


# ------------------------------------------------------------ self-addressing


def test_status_without_app_dir_env_raises_not_under_engine():
    with pytest.raises(background_app.NotUnderEngine):
        background_app.status()


def test_stop_without_app_dir_env_raises_not_under_engine():
    with pytest.raises(background_app.NotUnderEngine):
        background_app.stop()


def test_disable_without_app_dir_env_raises_not_under_engine():
    with pytest.raises(background_app.NotUnderEngine):
        background_app.disable()


# ------------------------------------------------------------------ requests


def test_status_gets_the_status_endpoint_with_urlencoded_html(monkeypatch, tmp_path):
    app_dir = str(tmp_path / "myapp")
    monkeypatch.setenv(background_app.APP_DIR_ENV, app_dir)
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:2266")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeHTTPResponse(json.dumps(
            {"enabled": True, "running": True, "pid": 111,
             "version": "v1", "engine_id": "bg_x"}).encode("utf-8"))

    monkeypatch.setattr(background_app.urllib.request, "urlopen", fake_urlopen)

    result = background_app.status()
    assert captured["method"] == "GET"
    assert captured["url"].startswith(
        "http://127.0.0.1:2266/api/apps/background/status?html=")
    expected_html = os.path.join(app_dir, "index.html")
    from urllib.parse import quote
    assert quote(expected_html) in captured["url"]
    assert result["running"] is True


def test_stop_posts_with_x_fused_header_and_html_body(monkeypatch, tmp_path):
    app_dir = str(tmp_path / "myapp")
    monkeypatch.setenv(background_app.APP_DIR_ENV, app_dir)
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:2266")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse(json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(background_app.urllib.request, "urlopen", fake_urlopen)

    result = background_app.stop()
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:2266/api/apps/background/stop"
    assert captured["headers"].get("X-fused") == "1"  # urllib title-cases headers
    assert captured["body"] == {"html": os.path.join(app_dir, "index.html")}
    assert result == {"ok": True}


def test_disable_posts_to_the_disable_endpoint(monkeypatch, tmp_path):
    app_dir = str(tmp_path / "myapp")
    monkeypatch.setenv(background_app.APP_DIR_ENV, app_dir)
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:2266")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeHTTPResponse(json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(background_app.urllib.request, "urlopen", fake_urlopen)

    background_app.disable()
    assert captured["url"] == "http://127.0.0.1:2266/api/apps/background/disable"


def test_an_http_error_body_is_surfaced_as_background_app_error(monkeypatch, tmp_path):
    app_dir = str(tmp_path / "myapp")
    monkeypatch.setenv(background_app.APP_DIR_ENV, app_dir)
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:2266")

    def fake_urlopen(req, timeout=None):
        raise _FakeHTTPError(400, {"error": "request body must include 'html'"})

    monkeypatch.setattr(background_app.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(background_app.BackgroundAppError) as exc_info:
        background_app.stop()
    assert exc_info.value.status == 400
    assert "html" in exc_info.value.message
