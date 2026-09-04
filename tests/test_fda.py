"""Tests for the Full Disk Access warning (fused_render/shell/fda.py).

The warning is macOS-packaged-app-only, so every test that wants it on forces
FUSED_RENDER_FDA_BANNER=1 — the same override a dev machine uses to exercise
the strip. FUSED_RENDER_HOME is redirected to a tmp dir so nothing touches
the real ~/.fused-render.
"""
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


def test_demo_forces_offered_ungranted_and_denied(monkeypatch):
    # A terminal-launched dev server inherits the terminal's TCC identity,
    # which usually has FDA — "demo" is how the strip gets exercised anyway,
    # so it also forces `denied` without manufacturing a real PermissionError.
    # Both probes answer not-granted: a child of a dev terminal WITH FDA would
    # otherwise turn demo into a phantom "pending relaunch".
    monkeypatch.setenv(fda_mod.FORCE_ENV, "demo")
    monkeypatch.setattr(fda_mod, "_denied", False)
    assert fda_mod.offered() is True
    assert fda_mod.granted() is False
    assert fda_mod.child_granted() is False
    assert fda_mod.snapshot() == {"granted": False, "pending_relaunch": False, "denied": True}


def test_snapshot_is_none_when_not_offered(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "0")
    assert fda_mod.snapshot() is None


def test_snapshot_is_none_when_the_probe_is_inconclusive(monkeypatch):
    # Every probe target missing → None → the config field is omitted and the
    # shell renders nothing. Uncertainty must never nag.
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: None)
    assert fda_mod.snapshot() is None


def test_snapshot_carries_granted_and_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    monkeypatch.setattr(fda_mod, "child_granted", lambda: False)
    monkeypatch.setattr(fda_mod, "_denied", False)
    assert fda_mod.snapshot() == {"granted": False, "pending_relaunch": False, "denied": False}
    monkeypatch.setattr(fda_mod, "_denied", True)
    assert fda_mod.snapshot() == {"granted": False, "pending_relaunch": False, "denied": True}


# ---- the two-stage probe: pending_relaunch ------------------------------------


def test_pending_relaunch_when_only_a_fresh_child_can_read(monkeypatch):
    # macOS caches this process's TCC verdict, so after the user grants FDA the
    # in-process probe keeps saying no. A fresh child sees the grant: that is
    # the "granted, relaunch to apply" state.
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    monkeypatch.setattr(fda_mod, "child_granted", lambda: True)
    monkeypatch.setattr(fda_mod, "_denied", False)
    assert fda_mod.snapshot() == {"granted": False, "pending_relaunch": True, "denied": False}


def test_child_probe_is_not_asked_once_this_process_can_read(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: True)

    def _boom():
        raise AssertionError("child probe must not run when granted")

    monkeypatch.setattr(fda_mod, "child_granted", _boom)
    assert fda_mod.snapshot() == {"granted": True, "pending_relaunch": False, "denied": False}


def test_inconclusive_child_probe_is_not_pending(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    monkeypatch.setattr(fda_mod, "child_granted", lambda: None)
    monkeypatch.setattr(fda_mod, "_denied", False)
    assert fda_mod.snapshot()["pending_relaunch"] is False


class _Proc:
    def __init__(self, rc, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def _fresh_child(monkeypatch, tmp_path):
    target = tmp_path / "gated"
    target.mkdir()
    monkeypatch.setattr(fda_mod, "_PROBES", [(str(target), "listdir")])
    monkeypatch.setattr(fda_mod, "_child_memo", (0.0, None))
    monkeypatch.delenv(fda_mod.FORCE_ENV, raising=False)
    return target


def test_child_granted_reads_ls_exit_and_errno_text(tmp_path, monkeypatch):
    target = _fresh_child(monkeypatch, tmp_path)
    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return _Proc(0)

    monkeypatch.setattr(fda_mod.subprocess, "run", _run)
    assert fda_mod.child_granted() is True
    # Plain `ls <dir>`: it must READ the directory. `ls -d` or a trailing
    # slash would stat the entry, which succeeds without FDA.
    assert calls == [["/bin/ls", str(target)]]

    monkeypatch.setattr(fda_mod, "_child_memo", (0.0, None))
    monkeypatch.setattr(
        fda_mod.subprocess, "run",
        lambda cmd, **kw: _Proc(1, "ls: gated: Operation not permitted\n"),
    )
    assert fda_mod.child_granted() is False

    # Any other failure is "don't know", not "denied".
    monkeypatch.setattr(fda_mod, "_child_memo", (0.0, None))
    monkeypatch.setattr(fda_mod.subprocess, "run", lambda cmd, **kw: _Proc(1, "ls: something else\n"))
    assert fda_mod.child_granted() is None


def test_child_granted_is_memoized(tmp_path, monkeypatch):
    # /api/config is polled by several surfaces across tabs: one fork per
    # CHILD_PROBE_TTL_S, not one per poll.
    _fresh_child(monkeypatch, tmp_path)
    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return _Proc(0)

    monkeypatch.setattr(fda_mod.subprocess, "run", _run)
    assert fda_mod.child_granted() is True
    assert fda_mod.child_granted() is True
    assert len(calls) == 1


def test_child_granted_none_without_a_directory_target(tmp_path, monkeypatch):
    # A file target (`read` probe) is skipped: `ls` on a file is a stat,
    # which succeeds without FDA and would fake a grant.
    db = tmp_path / "TCC.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(fda_mod, "_PROBES", [(str(tmp_path / "missing"), "listdir"), (str(db), "read")])
    monkeypatch.setattr(fda_mod, "_child_memo", (0.0, None))
    monkeypatch.delenv(fda_mod.FORCE_ENV, raising=False)
    monkeypatch.setattr(
        fda_mod.subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    assert fda_mod.child_granted() is None


# ---- note_denied(): what makes the warning worth showing ----------------------


def test_denied_flips_only_on_a_permission_error(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "_denied", False)
    fda_mod.note_denied(FileNotFoundError("gone"))
    assert fda_mod._denied is False
    fda_mod.note_denied(PermissionError("EPERM"))
    assert fda_mod._denied is True


def test_denied_is_inert_when_not_offered(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "0")
    monkeypatch.setattr(fda_mod, "_denied", False)
    fda_mod.note_denied(PermissionError("EPERM"))
    assert fda_mod._denied is False


# ---- refused(): the one answer to a denied read -------------------------------


def test_refused_records_the_denial_and_answers_403(monkeypatch):
    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "_denied", False)
    resp = fda_mod.refused("/x/y", PermissionError("EPERM"))
    assert resp.status_code == 403
    assert b"cannot read /x/y" in resp.body
    assert fda_mod._denied is True


def test_an_uncaught_permission_error_is_a_403_not_a_500(tmp_path, monkeypatch):
    # The backstop handler: a route that never caught its PermissionError used
    # to 500 and the warning never heard about it.
    from fastapi import FastAPI

    monkeypatch.setenv(fda_mod.FORCE_ENV, "1")
    monkeypatch.setattr(fda_mod, "_denied", False)
    app = FastAPI()
    app.exception_handler(PermissionError)(fda_mod.permission_error_handler)

    @app.get("/boom")
    def boom(path: str):
        raise PermissionError(1, "Operation not permitted", path)

    resp = TestClient(app, raise_server_exceptions=False).get("/boom", params={"path": "/p"})
    assert resp.status_code == 403
    assert "cannot read /p" in resp.json()["error"]
    assert fda_mod._denied is True


def test_server_registers_the_permission_error_backstop(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    handlers = client.app.exception_handlers
    assert handlers.get(PermissionError) is fda_mod.permission_error_handler


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


def test_dismiss_clears_denied_until_the_next_denial(tmp_path, monkeypatch):
    # Dismiss is "not now", not "never": it clears the server-side flag, and
    # the next PermissionError raises it again.
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    monkeypatch.setattr(fda_mod, "child_granted", lambda: False)
    monkeypatch.setattr(fda_mod, "_denied", True)

    resp = client.post("/api/fda/dismiss", headers=FUSED)
    assert resp.status_code == 200
    assert client.get("/api/config").json()["fda"]["denied"] is False

    fda_mod.note_denied(PermissionError("EPERM"))
    assert client.get("/api/config").json()["fda"]["denied"] is True


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
    monkeypatch.setattr(fda_mod, "child_granted", lambda: False)
    monkeypatch.setattr(fda_mod, "_denied", False)
    body = client.get("/api/config").json()
    assert body["fda"] == {"granted": False, "pending_relaunch": False, "denied": False}

    monkeypatch.setenv(fda_mod.FORCE_ENV, "0")
    assert "fda" not in client.get("/api/config").json()


def test_a_refused_listing_flips_denied(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(fda_mod, "granted", lambda: False)
    monkeypatch.setattr(fda_mod, "child_granted", lambda: False)
    monkeypatch.setattr(fda_mod, "_denied", False)
    gated = tmp_path / "gated"
    gated.mkdir()

    assert client.get("/api/config").json()["fda"]["denied"] is False

    # Deny ONLY the gated path: fs_read's `os` is the global module, so a
    # blanket patch would still be live during the /api/config reads below.
    real_scandir = os.scandir

    def _deny(path, *a, **kw):
        if str(path) == str(gated):
            raise PermissionError(path)
        return real_scandir(path, *a, **kw)

    import fused_render.server.routers.fs_read as fs_read_mod
    monkeypatch.setattr(fs_read_mod.os, "scandir", _deny)
    resp = client.get("/api/fs/list", params={"path": str(gated)})
    # 403 with the refused() shape — the explorer keys its card on exactly this.
    assert resp.status_code == 403
    assert resp.json()["error"].startswith(f"cannot read {gated}")
    assert client.get("/api/config").json()["fda"]["denied"] is True
