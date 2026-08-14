"""Regression test for the sidecar JSON key rename: "sessions" ->
"claudeSessions" (fused_render/templates/claude/agent.py). Old sidecars
written under the pre-rename key are silently ignored (no migration).

Retargeted from the deleted plain chat template's agent to the split view's
(which now carries the `claude` name), the only chat backend left and one that
carries the same three rules verbatim. The
file's fourth test — record_session under a read-only mount — was dropped rather
than moved: test_claude_sidecar_location.py already pins it against this exact
module, and two copies of one assertion is a maintenance cost with no coverage.

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
