"""The OS clipboard bridge routes (server/routers/clipboard.py):
GET /api/clipboard/files reads the system clipboard, POST writes to it.

The pasteboard module is monkeypatched at the route's seam in every test, so
nothing here touches the developer's real clipboard and the file runs the
same on all three platforms.
"""
import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.routers import clipboard as clipboard_mod


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def fake(monkeypatch):
    """Install a fake pasteboard; returns a dict recording the write."""
    state = {"paths": [], "supported": True, "written": None}

    def read_files():
        if not state["supported"]:
            return [], "", False
        paths = list(state["paths"])
        return paths, ("tok:" + "|".join(paths)) if paths else "", True

    def write_files(paths):
        # Mirrors the real contract: it validates, refuses to clear the
        # clipboard on an empty list, and raises ValueError on a bad path.
        for p in paths:
            if not isinstance(p, str) or not p.startswith("/"):
                raise ValueError(f"clipboard paths must be absolute: {p!r}")
        if not state["supported"]:
            return "", False
        if not paths:
            return "", True
        state["written"] = list(paths)
        return "tok:" + "|".join(paths), True

    monkeypatch.setattr(clipboard_mod.pasteboard, "read_files", read_files)
    monkeypatch.setattr(clipboard_mod.pasteboard, "write_files", write_files)
    return state


# ----------------------------------------------------------------------- GET

def test_get_returns_paths_token_and_supported(client, fake):
    fake["paths"] = ["/a/b.csv", "/c/dir"]
    out = client.get("/api/clipboard/files").json()
    assert out["paths"] == ["/a/b.csv", "/c/dir"]
    assert out["token"] == "tok:/a/b.csv|/c/dir"
    assert out["supported"] is True


def test_get_on_an_empty_clipboard(client, fake):
    out = client.get("/api/clipboard/files").json()
    assert out == {"paths": [], "token": "", "supported": True}


def test_get_reports_unsupported(client, fake):
    fake["supported"] = False
    out = client.get("/api/clipboard/files").json()
    assert out == {"paths": [], "token": "", "supported": False}
    # Unsupported is a normal answer, not an error — the frontend must not
    # see a failed request and log noise on every focus change.
    assert client.get("/api/clipboard/files").status_code == 200


# ---------------------------------------------------------------------- POST

def test_post_writes_and_returns_the_token(client, fake):
    r = client.post("/api/clipboard/files", json={"paths": ["/a/b.csv"]},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    assert r.json() == {"token": "tok:/a/b.csv", "supported": True}
    assert fake["written"] == ["/a/b.csv"]


def test_post_reports_unsupported(client, fake):
    fake["supported"] = False
    out = client.post("/api/clipboard/files", json={"paths": ["/a/b.csv"]},
                      headers={"X-Fused": "1"}).json()
    assert out == {"token": "", "supported": False}


def test_post_requires_the_fused_header(client, fake):
    # A mutating POST any website could fire blind — same guard as the fs
    # mutation routes.
    r = client.post("/api/clipboard/files", json={"paths": ["/a/b.csv"]})
    assert r.status_code == 403
    assert fake["written"] is None


def test_post_rejects_a_relative_path(client, fake):
    r = client.post("/api/clipboard/files", json={"paths": ["relative.txt"]},
                    headers={"X-Fused": "1"})
    assert r.status_code == 400
    assert "absolute" in r.json()["error"]
    assert fake["written"] is None


def test_post_rejects_a_non_list_body(client, fake):
    r = client.post("/api/clipboard/files", json={"paths": "/a/b.csv"},
                    headers={"X-Fused": "1"})
    assert r.status_code == 400
    assert fake["written"] is None


def test_post_of_an_empty_list_is_accepted_without_writing(client, fake):
    # Clearing the OS clipboard is never something an in-app copy should do.
    r = client.post("/api/clipboard/files", json={"paths": []},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    assert fake["written"] is None
