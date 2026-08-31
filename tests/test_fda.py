"""Tests for the Full Disk Access warning (fused_render/shell/fda.py).

The warning is macOS-packaged-app-only, so every test that wants it on forces
FUSED_RENDER_FDA_BANNER=1 — the same override a dev machine uses to exercise
the strip. FUSED_RENDER_HOME is redirected to a tmp dir so dismissal never
touches the real ~/.fused-render.
"""
import json
import os

from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.shell import fda as fda_mod


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


def _client(tmp_path, monkeypatch, *, force="1"):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv(fda_mod.FORCE_ENV, force)
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


# ---- offered() / snapshot() gating ------------------------------------------


def test_not_offered_outside_the_packaged_mac_app(monkeypatch):
    # A dev server is never sys.frozen == "macosx_app"; with no override the
    # nudge must stay off even on a mac — the process's TCC identity is the
    # terminal that launched it, so a grant would land on the wrong app.
    monkeypatch.delenv(fda_mod.FORCE_ENV, raising=False)
    assert fda_mod.offered() is False


def test_force_env_flips_offered_both_ways(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    assert fda_mod.offered() is True
    monkeypatch.setenv(fda_mod.FORCE_ENV, "0")
    assert fda_mod.offered() is False


def test_demo_forces_offered_and_ungranted(monkeypatch):
    # A terminal-launched dev server inherits the terminal's TCC identity,
    # which usually has FDA — "demo" is how the card gets exercised anyway.
    monkeypatch.setenv(fda_mod.FORCE_ENV, "demo")
    assert fda_mod.offered() is True
    assert fda_mod.granted() is False


def test_snapshot_is_none_when_not_offered(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "0")
    assert fda_mod.snapshot() is None


def test_snapshot_is_none_when_the_probe_is_inconclusive(monkeypatch):
    # Every probe target missing → None → the config field is omitted and the
    # shell renders nothing. Uncertainty must never nag.
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: None)
    assert fda_mod.snapshot() is None


def test_snapshot_carries_granted_and_dismissed(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    assert fda_mod.snapshot() == {"granted": False, "dismissed": False}
    fda_mod.set_dismissed()
    assert fda_mod.snapshot() == {"granted": False, "dismissed": True}


# ---- granted() probe semantics ----------------------------------------------


def test_granted_true_on_a_readable_probe(tmp_path, monkeypatch):
    probe = tmp_path / "probe-dir"
    probe.mkdir()
    monkeypatch.setattr(fda_mod, "_PROBES", [(str(probe), "listdir")])
    assert fda_mod.granted() is True


def test_granted_false_on_permission_error(tmp_path, monkeypatch):
    probe = tmp_path / "gated"
    probe.mkdir()

    def _deny(path):
        raise PermissionError(path)

    monkeypatch.setattr(fda_mod, "_PROBES", [(str(probe), "listdir")])
    monkeypatch.setattr(fda_mod.os, "listdir", _deny)
    assert fda_mod.granted() is False


def test_granted_none_when_every_probe_target_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fda_mod, "_PROBES", [(str(tmp_path / "nope"), "listdir"), (str(tmp_path / "no.db"), "read")]
    )
    assert fda_mod.granted() is None


def test_granted_skips_a_missing_target_and_reads_the_next(tmp_path, monkeypatch):
    present = tmp_path / "present.db"
    present.write_bytes(b"x")
    monkeypatch.setattr(
        fda_mod, "_PROBES", [(str(tmp_path / "missing"), "listdir"), (str(present), "read")]
    )
    assert fda_mod.granted() is True


# ---- the endpoints -----------------------------------------------------------


def test_dismiss_requires_the_fused_header(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/api/fda/dismiss")
    assert resp.status_code == 403


def test_dismiss_persists_under_the_home_dir(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    resp = client.post("/api/fda/dismiss", headers=FUSED)
    assert resp.status_code == 200
    with open(os.path.join(home, "fda.json")) as fh:
        assert json.load(fh) == {"strip_dismissed": True}


def test_endpoints_404_when_not_offered(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, force="0")
    assert client.post("/api/fda/dismiss", headers=FUSED).status_code == 404
    assert client.post("/api/fda/settings", headers=FUSED).status_code == 404


def test_settings_opens_the_fda_pane(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        fda_mod.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd) or None
    )
    resp = client.post("/api/fda/settings", headers=FUSED)
    assert resp.status_code == 200
    assert calls == [["open", fda_mod.SETTINGS_URL]]


# ---- /api/config integration --------------------------------------------------


def test_config_carries_fda_only_when_offered(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    body = client.get("/api/config").json()
    assert body["fda"] == {"granted": False, "dismissed": False}

    monkeypatch.setenv(fda_mod.FORCE_ENV, "0")
    assert "fda" not in client.get("/api/config").json()
