"""Sidecar location for claude's per-file/per-folder session index
(fused_render/templates/claude/agent.py) — forked from the since-deleted plain
chat template's agent (D166: templates must not import fused_render, so each
template keeps its own copy) and, until now, still on that fork's pre-relocation
sidecar scheme.

The sidecar now lives under home_dir()/sidecar/<mapped path>.json (D83-
reversal, D205 — shell/storage.py's sidecar_path, mirrored for templates in
shared/appenv.py), never next to the TARGET (file or folder). The key-name and
legacy-key rules live in test_claude_agent_sidecar.py, which pins the same
module. A folder target used to get a reserved `.claude-split.json` INSIDE itself
(avoiding a collision with a real sibling `<folder>.json`); that collision
risk doesn't exist once the sidecar lives in its own tree under home_dir(), so
a directory target now resolves through the exact same appenv.sidecar_path as
a file target. FUSED_RENDER_HOME is pinned to an isolated tmp dir for every
test so a real sidecar under the developer's actual ~/.fused-render is never
touched.
"""
import importlib.util
import json
import os

import pytest

from fused_render.shell import storage


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def test_file_target_sidecar_lives_under_home_dir(tmp_path):
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    path = agent._sidecar_path(str(f))
    assert path == storage.sidecar_path(str(f))
    assert not path.startswith(str(tmp_path) + os.sep + "sample")


def test_folder_target_sidecar_also_lives_under_home_dir_not_inside_it(tmp_path):
    # The old scheme wrote a reserved dotfile INSIDE the folder; the new one
    # maps the folder's own path under home_dir()/sidecar/, same as a file.
    agent = _load_agent()
    d = tmp_path / "my-app"
    d.mkdir()
    path = agent._sidecar_path(str(d))
    assert path == storage.sidecar_path(str(d))
    assert not path.startswith(str(d) + os.sep)
    assert os.path.basename(path) != ".claude-split.json"


def test_record_session_creates_the_sidecar_and_is_listed(tmp_path):
    agent = _load_agent()
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    agent._record_session(str(f), "sid-1", "hello there", "")
    path = agent._sidecar_path(str(f))
    assert os.path.exists(path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["claudeSessions"][0]["id"] == "sid-1"
    assert agent._sessions(str(f))["sessions"][0]["id"] == "sid-1"


def test_record_session_on_a_folder_target(tmp_path):
    agent = _load_agent()
    d = tmp_path / "my-app"
    d.mkdir()
    agent._record_session(str(d), "sid-1", "scaffold this app", "")
    path = agent._sidecar_path(str(d))
    assert os.path.exists(path)
    assert not os.path.exists(d / ".claude-split.json")
    assert agent._sessions(str(d))["sessions"][0]["id"] == "sid-1"


# --------------------------------------------------- read-only remote mounts
# D83-reversal, D205: the sidecar now lives under home_dir()/sidecar/, never
# on the mounted target's own filesystem, so a read-only remote mount no
# longer has any bearing on whether a claudeSessions sidecar can be written —
# the _mount_read_only gate that used to answer this has been removed.

def test_record_session_succeeds_under_read_only_mount(tmp_path, monkeypatch):
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
