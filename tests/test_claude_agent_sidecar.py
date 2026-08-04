"""Regression test for the sidecar JSON key rename: "sessions" ->
"claudeSessions" (fused_render/templates/claude/agent.py). Old sidecars
written under the pre-rename key are silently ignored (no migration).

The sidecar now lives under home_dir()/sidecar/<mapped path>.json (D83-
reversal), never next to the TARGET file — see shell/storage.py's
sidecar_path (mirrored for templates in shared/appenv.py). FUSED_RENDER_HOME
is pinned to an isolated tmp dir for every test so a real sidecar under the
developer's actual ~/.fused-render is never touched.
"""
import importlib.util
import json
import os

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def test_sidecar_uses_claudeSessions_key(tmp_path):
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    agent._record_session(str(f), "sid-1", "hello there", "")
    data = json.loads(open(agent._sidecar_path(str(f)), encoding="utf-8").read())
    assert "claudeSessions" in data and "sessions" not in data
    assert data["claudeSessions"][0]["id"] == "sid-1"


def test_old_sessions_key_ignored(tmp_path):
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    sidecar_path = agent._sidecar_path(str(f))
    os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump({"sessions": [{"id": "old"}]}, fh)
    # old key not recognised -> reads as empty
    assert agent._sessions(str(f))["sessions"] == []


def test_bookmark_history_survives_load_save_roundtrip(tmp_path):
    # Defense-in-depth (spec-2): a sidecar carrying bookmarkHistory but NO
    # claudeSessions yet (server wrote it first) must not lose the history when
    # a claude turn loads and re-saves the sidecar.
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    history = [{"id": "bk-1", "url": "/view/x", "recorded_at": 1.0}]
    sidecar_path = agent._sidecar_path(str(f))
    os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump({"bookmarkHistory": history}, fh)

    loaded = agent._load_sidecar(str(f))
    assert loaded["bookmarkHistory"] == history
    assert loaded["claudeSessions"] == []  # backfilled so the guard passes

    agent._save_sidecar(str(f), loaded)
    data = json.loads(open(sidecar_path, encoding="utf-8").read())
    assert data["bookmarkHistory"] == history


# --------------------------------------------------- read-only remote mounts
# D83-reversal: the sidecar now lives under home_dir()/sidecar/, never on the
# mounted file's own filesystem, so a read-only remote mount no longer has any
# bearing on whether a claudeSessions sidecar can be written — the old
# sidecar-write incident (CacheMode=full 403-looping a doomed PutObject)
# structurally can't happen anymore, and the _mount_read_only gate that used
# to answer this (via FUSED_RENDER_RO_MOUNTS/shared/appenv) has been removed
# from agent.py entirely.

def test_record_session_succeeds_under_read_only_mount(tmp_path, monkeypatch):
    # Chatting about a file inside a read-only S3 mount still writes a
    # claudeSessions sidecar — it lives under home_dir()/sidecar/, not next to
    # the mounted file, so the mount's read-only-ness has no bearing on it.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "sample.html")
    with open(f, "w") as fh:
        fh.write("<html></html>")

    agent = _load_agent()
    agent._record_session(f, "sid-1", "hello there", "")
    assert os.path.exists(agent._sidecar_path(f))
    assert agent._sessions(f)["sessions"][0]["id"] == "sid-1"
