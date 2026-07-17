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
    (tmp_path / "sample.html.json").write_text(json.dumps({"sessions": [{"id": "old"}]}))
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
    (tmp_path / "sample.html.json").write_text(json.dumps({"bookmarkHistory": history}))

    loaded = agent._load_sidecar(str(f))
    assert loaded["bookmarkHistory"] == history
    assert loaded["claudeSessions"] == []  # backfilled so the guard passes

    agent._save_sidecar(str(f), loaded)
    data = json.loads((tmp_path / "sample.html.json").read_text())
    assert data["bookmarkHistory"] == history
