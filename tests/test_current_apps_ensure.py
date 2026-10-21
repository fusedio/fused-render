"""Opening a task or an app page puts its app on the desk when the one look
`observe` took found no app yet (a Bots build is listed while its folder is
still empty) — POST /api/current-apps/ensure."""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import current_apps
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    d = tmp_path / "Fused" / "app" / "built"
    (d / "sub").mkdir(parents=True)
    return canonical_fs_path(str(d)).rstrip("/")


def _mark(folder):
    with open(os.path.join(folder, "index.html"), "w") as f:
        f.write('<!doctype html><html><head><meta name="fused-app" /></head></html>')


def _paths():
    return [a["path"] for a in current_apps.list_apps()]


def test_a_task_seen_on_an_empty_folder_is_added_when_opened(env):
    folder = env
    # The listing's one look: the build task, folder still empty.
    current_apps.observe([{"key": "k1", "project": folder, "status": "in_progress"}])
    assert _paths() == []
    assert current_apps.ensure(folder) == (None, False)  # still no page: opening adds nothing
    _mark(folder)
    assert current_apps.ensure(folder + "/sub") == (folder, True)  # a task inside the app, too
    assert _paths() == [folder]
    assert current_apps.list_apps()[0]["opened_at"] > 0  # born opened: no dot
    assert current_apps.ensure(folder) == (folder, False)  # already there: keeps its place
    assert _paths() == [folder]


def test_ensure_route(env, tmp_path):
    folder = env
    _mark(folder)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/current-apps/ensure", json={"path": folder + "/sub"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["app"] == folder and r.json()["added"] is True
    assert [a["path"] for a in r.json()["apps"]] == [folder]
    r = client.post("/api/current-apps/ensure", json={"path": folder}, headers=FUSED)
    assert r.json()["added"] is False and r.json()["apps"] is None  # nothing to announce
    r = client.post("/api/current-apps/ensure", json={"path": os.path.dirname(folder)}, headers=FUSED)
    assert r.status_code == 200 and r.json() == {"ok": True, "app": None, "added": False, "apps": None}
