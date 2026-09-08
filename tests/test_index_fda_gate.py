"""Indexing waits for Full Disk Access on the packaged mac app
(fused_render/shell/index_gate.py).

The default scan root is the user's home, and a recursive walk of it reads
under every TCC-protected folder; on a fresh install that used to fire a
prompt per folder at boot, before the onboarding wizard had painted. Every
trigger of a scan now asks the one gate, and the gate says "fda" whenever the
app offers the nudge and the in-process probe conclusively says not granted.
"""
import pytest

from fused_render.index.config import load_config
from fused_render.server import index_touch
from fused_render.server.routers import index as index_router
from fused_render.shell import fda as fda_mod
from fused_render.shell import index_gate


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _no_fda(monkeypatch):
    monkeypatch.setattr(fda_mod, "offered", lambda: True)
    monkeypatch.setattr(fda_mod, "granted", lambda: False)


# ------------------------------------------------------------------- the gate

def test_not_blocked_where_the_nudge_is_not_offered(home, monkeypatch):
    """Non-mac and dev servers: TCC is not in play (or the identity is the
    terminal's), so a missing grant means nothing here."""
    monkeypatch.setattr(fda_mod, "offered", lambda: False)
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    assert index_gate.indexing_blocked() == ""
    assert index_gate.indexing_allowed()


def test_blocked_when_offered_and_conclusively_not_granted(home, monkeypatch):
    _no_fda(monkeypatch)
    assert index_gate.indexing_blocked() == "fda"
    assert not index_gate.indexing_allowed()


def test_not_blocked_when_granted_or_inconclusive(home, monkeypatch):
    """Only a conclusive "no" blocks: an install with no probe target would
    otherwise never index, with nothing the user could do about it."""
    monkeypatch.setattr(fda_mod, "offered", lambda: True)
    monkeypatch.setattr(fda_mod, "granted", lambda: True)
    assert index_gate.indexing_blocked() == ""
    monkeypatch.setattr(fda_mod, "granted", lambda: None)
    assert index_gate.indexing_blocked() == ""


def test_the_preference_outranks_the_grant(home, monkeypatch):
    _no_fda(monkeypatch)
    monkeypatch.setattr(index_gate.prefs, "indexing_enabled", lambda: False)
    assert index_gate.indexing_blocked() == "disabled"


# ---------------------------------------------------------------- the triggers

def test_startup_scan_starts_nothing_without_fda(home, tmp_path, monkeypatch):
    """The boot-time trigger, the one that fired the prompts on a fresh
    install. No config read either: the gate is checked first."""
    _no_fda(monkeypatch)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == []


def test_runner_start_refuses_without_fda(home, tmp_path, monkeypatch):
    """The backstop for anything that reaches the runner directly."""
    _no_fda(monkeypatch)
    with pytest.raises(ValueError, match="Full Disk Access"):
        index_router.runner.start(load_config(), str(tmp_path))


def test_scan_route_409s_with_the_fda_reason(home, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    _no_fda(monkeypatch)
    client = TestClient(create_app(str(tmp_path)))
    resp = client.post("/api/index/scan", json={"root": str(tmp_path)},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "fda"


def test_scan_folder_reports_fda_and_starts_nothing(home, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    _no_fda(monkeypatch)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    client = TestClient(create_app(str(tmp_path)))
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    resp = client.post("/api/index/scan-folder", json={"path": str(folder)},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert resp.json()["started"] is False
    assert resp.json()["why"] == "fda"
    assert started == []


def test_folder_open_and_mutation_triggers_are_gated(home, tmp_path, monkeypatch):
    _no_fda(monkeypatch)
    threads = []
    monkeypatch.setattr(index_router.threading, "Thread",
                        lambda **kw: threads.append(kw))
    assert index_router.note_folder_opened(str(tmp_path)) is False
    assert threads == []
    noted = []
    monkeypatch.setattr(index_touch._queue, "note", lambda *p: noted.append(p))
    index_touch.note_index_mutation(str(tmp_path / "a.txt"))
    assert noted == []


def test_rank_reason_is_fda_for_an_uncovered_root(home, tmp_path, monkeypatch):
    """What the home search keys its "grant Full Disk Access" callout on."""
    _no_fda(monkeypatch)
    cfg = load_config()
    reason = index_router._rank_reason(cfg, str(tmp_path), {"covered": False})
    assert reason == "fda"
