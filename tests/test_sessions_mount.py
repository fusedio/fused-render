"""Tests for the Sessions sub-app's shipped content: the app persists triage
state, and the mount it ships on is read-only, so the state files must resolve
under FUSED_RENDER_HOME rather than next to the scripts.

The mount itself (record upsert, detach/refresh, readiness) is the generic
builtin-mount machinery, covered by test_builtin_mounts.py — which drives it
through this very mount, the only builtin the app still ships.
"""
import json
import os
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(REPO_ROOT, "core_apps", "sessions")


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
