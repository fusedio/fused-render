"""Regression test for the sidecar JSON key rename: "sessions" ->
"claudeSessions" (fused_render/templates/claude/agent.py). Old sidecars
written under the pre-rename key are silently ignored (no migration).
"""
import importlib.util
import json
import os


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sidecar_uses_claudeSessions_key(tmp_path):
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    agent._record_session(str(f), "sid-1", "hello there", "")
    data = json.loads((tmp_path / "sample.html.json").read_text())
    assert "claudeSessions" in data and "sessions" not in data
    assert data["claudeSessions"][0]["id"] == "sid-1"


def test_old_sessions_key_ignored(tmp_path):
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    (tmp_path / "sample.html.json").write_text(
        json.dumps({"sessions": [{"id": "old"}]}))
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
    (tmp_path / "sample.html.json").write_text(
        json.dumps({"bookmarkHistory": history}))

    loaded = agent._load_sidecar(str(f))
    assert loaded["bookmarkHistory"] == history
    assert loaded["claudeSessions"] == []  # backfilled so the guard passes

    agent._save_sidecar(str(f), loaded)
    data = json.loads((tmp_path / "sample.html.json").read_text())
    assert data["bookmarkHistory"] == history


def test_record_session_skipped_under_read_only_mount(tmp_path, monkeypatch):
    # Chatting about a file inside a read-only S3 mount must not write a
    # claudeSessions sidecar next to it — the mount can't take the write
    # (CacheMode=full loops the doomed upload, the sidecar-write incident).
    # The chat + its transcript (~/.claude/projects) still work; only the
    # sidecar session list is skipped.
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
    assert not os.path.exists(f + ".json")
    assert agent._sessions(f)["sessions"] == []


# ------------------------------------------------- read-only remote mounts
# _mount_read_only gates the sidecar write: under a read-only S3 mount with
# CacheMode=full the write lands in the local VFS cache and 403-loops on the
# async upload (the sidecar-write incident). The fact reaches this template as
# FUSED_RENDER_RO_MOUNTS (shared/appenv), never as a fused_render import — a
# child under the fused engine has no PYTHONPATH, so the old import failed on
# every run there and a read-only mount looked writable.

def test_mount_read_only_reads_the_env_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    agent = _load_agent()
    assert agent._mount_read_only(os.path.join(mp, "page.html")) is True
    # A local path is never read-only for THIS reason.
    assert agent._mount_read_only(str(tmp_path / "local.html")) is False


def test_mount_read_only_degrades_to_false_without_the_env(tmp_path, monkeypatch):
    """No server exported the list => nothing is known to be read-only. False is
    the pre-guard behavior and the only safe default: refusing every write
    because we cannot tell would break the local case entirely."""
    monkeypatch.delenv("FUSED_RENDER_RO_MOUNTS", raising=False)
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", str(tmp_path / "mounts"))
    assert _load_agent()._mount_read_only(
        str(tmp_path / "mounts" / "pub" / "page.html")) is False
