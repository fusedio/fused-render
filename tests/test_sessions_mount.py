"""Tests for the builtin sessions mount (the Claude Sessions sub-app): the
bundled sessions.zip upserted into mounts.json as a read-only :archive: mount
at automount time, via the same generic builtin-mount path as learn — the
full detach/refresh mechanics are covered by test_learn_mount.py; these cover
the sessions-specific wiring plus the app content's state-dir redirection.
"""
import json
import os
import subprocess
import sys

import pytest

import fused_render.shell.mounts as mounts_mod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(REPO_ROOT, "sessions")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


@pytest.fixture()
def sessions_zip(tmp_path, monkeypatch):
    zp = tmp_path / "sessions.zip"
    zp.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty-zip EOCD; content unused
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(zp))
    return zp


def _sessions_records():
    return [m for m in mounts_mod.list_mounts()
            if m.get("builtin") == mounts_mod.SESSIONS_MOUNT_NAME]


def test_zip_path_env_override(sessions_zip):
    assert mounts_mod.builtin_zip_path("sessions") == str(sessions_zip)


def test_zip_path_none_when_override_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(tmp_path / "gone.zip"))
    assert mounts_mod.builtin_zip_path("sessions") is None


def test_ensure_builtin_mounts_creates_record(home, sessions_zip, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_builtin_mounts()
    recs = _sessions_records()
    assert len(recs) == 1
    m = recs[0]
    assert m["name"] == "sessions"
    assert m["remote"] == f":archive:{sessions_zip}"
    assert m["read_only"] is True
    assert m["read_only_user"] is True


def test_ensure_builtin_mounts_creates_both(home, sessions_zip, tmp_path, monkeypatch):
    learn = tmp_path / "learn.zip"
    learn.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(learn))
    mounts_mod.ensure_builtin_mounts()
    builtins = sorted(m["builtin"] for m in mounts_mod.list_mounts()
                      if m.get("builtin"))
    assert builtins == ["learn", "sessions"]


def test_sessions_mount_ready_false_without_record(home):
    assert mounts_mod.sessions_mount_ready() is False


def test_never_clobbers_user_mount_named_sessions(home, sessions_zip):
    from fused_render.shell.mounts.store import _write
    user = {"id": "u1", "name": "sessions", "remote": "s3:bucket"}
    _write([user])
    mounts_mod.ensure_builtin_mounts()
    recs = [m for m in mounts_mod.list_mounts() if m["name"] == "sessions"]
    assert len(recs) == 1
    assert recs[0]["remote"] == "s3:bucket"
    assert "builtin" not in recs[0]


# -- shipped content: mutable state must live outside the (read-only) mount --


def _run_script(script_rel: str, code: str, env_home: str) -> str:
    """Run a snippet with the script's dir importable, FUSED_RENDER_HOME set —
    the scripts are standalone runPython targets, not package modules."""
    script_dir = os.path.join(SESSIONS_DIR, os.path.dirname(script_rel))
    env = dict(os.environ, FUSED_RENDER_HOME=env_home)
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=script_dir, env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_state_files_point_at_user_data_dir(tmp_path):
    home = str(tmp_path / "fr-home")
    expected = os.path.join(home, "claude-sessions")
    out = _run_script(
        "set_triage.py",
        "import set_triage, json; print(json.dumps(set_triage.TRIAGE_FILE))",
        home,
    )
    assert json.loads(out) == os.path.join(expected, "triage.json")
    out = _run_script(
        "sessions/set_name.py",
        "import set_name, json; print(json.dumps(set_name.NAMES_FILE))",
        home,
    )
    assert json.loads(out) == os.path.join(expected, "session_names.json")


def test_set_triage_writes_to_state_dir(tmp_path):
    home = str(tmp_path / "fr-home")
    out = _run_script(
        "set_triage.py",
        "import set_triage, json; "
        "print(json.dumps(set_triage.main('abc', json.dumps({'status': 'done'}))))",
        home,
    )
    assert json.loads(out)["ok"] is True
    triage_file = os.path.join(home, "claude-sessions", "triage.json")
    with open(triage_file) as f:
        assert json.load(f)["abc"]["status"] == "done"
    # nothing written next to the scripts (the shipped copy is read-only)
    assert not os.path.exists(os.path.join(SESSIONS_DIR, "triage.json"))
