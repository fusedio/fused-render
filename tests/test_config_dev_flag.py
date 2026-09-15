"""/api/config's `dev` flag: is this server a dev.sh run?

The shell's status banner asks before it blocks the page with "fused-render
updated — refresh". On a dev server that prompt is a lie: `version` is whatever
the checkout says while the bundle in the tab was rebuilt by the vite watch
moments ago, so a version bump or a branch switch raised a blocking dialog over
the newest code there is. scripts/dev.sh exports FUSED_RENDER_DEV=1; nothing in
a packaged app does.
"""
from fastapi.testclient import TestClient

from fused_render.server import create_app


def test_config_reports_dev_when_the_env_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DEV", "1")
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/config").json()["dev"] is True


def test_config_is_not_dev_without_the_env(tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_DEV", raising=False)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    # Present and False, not absent: the shell reads `body.dev === true`, and a
    # key that comes and goes is one more shape for that read to be wrong about.
    assert client.get("/api/config").json()["dev"] is False


def test_only_an_exact_1_counts(tmp_path, monkeypatch):
    # The env is a flag, not a truthiness test — `FUSED_RENDER_DEV=0` is how a
    # dev checkout asks to see the production banner.
    monkeypatch.setenv("FUSED_RENDER_DEV", "0")
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/config").json()["dev"] is False


def test_dev_is_read_per_request(tmp_path, monkeypatch):
    # Same app object, two answers: the value is read on the call, not frozen at
    # import — the whole config dict is built per request (`engine` next to it
    # changes under the Preferences switch).
    monkeypatch.delenv("FUSED_RENDER_DEV", raising=False)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/config").json()["dev"] is False
    monkeypatch.setenv("FUSED_RENDER_DEV", "1")
    assert client.get("/api/config").json()["dev"] is True
