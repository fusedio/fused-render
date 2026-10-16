"""Server plumbing for the second agent backend (docs/COPILOT_BACKEND.md,
WS-A): the root LIST every transcript reader walks, the `backend` field on
the task row and the settings record, the Preferences switches' rule, the
scheduler's disabled gate, erase under the Copilot root, the catalog
endpoint, and the grep-test behind invariant 7.

Every path is under tmp: FUSED_RENDER_HOME (which `copilot_dir()` hangs off)
and the Claude roots the modules expose for patching.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import (
    claude_artifacts,
    claude_spawn,
    schedule,
    schedule_wake,
    session_liveness,
    tasks_store,
    tasks_watch,
)
from fused_render.agent_backends import registry, roots
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod
from fused_render.shell import prefs as prefs_mod

FUSED = {"X-Fused": "1"}
# Old, so the one user row never reads as an open turn (`transcript_turn_open`).
T1 = "2026-08-16T09:00:00Z"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def claude_projects(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    for mod in (tasks_store, sessions_mod, session_liveness, claude_artifacts):
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    schedule._watched.clear()
    tasks_watch._registry.clear()
    yield
    schedule._watched.clear()


@pytest.fixture()
def copilot_projects(home):
    d = home / "copilot" / "projects"
    d.mkdir(parents=True)
    assert roots.projects_dir("copilot") == str(d)
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _user(text, ts=T1):
    return {"type": "user", "timestamp": ts, "entrypoint": "sdk-cli",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _write_transcript(projects_dir, session_id, cwd, records):
    d = projects_dir / roots.munge(cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    lines = []
    for i, record in enumerate(records):
        record = dict(record)
        record.setdefault("cwd", cwd)
        record.setdefault("sessionId", session_id)
        record.setdefault("uuid", f"{session_id}-{i}")
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")
    return path


def _enable_copilot():
    prefs_mod.write_pref("copilot_enabled", True)


# ------------------------------------------------------------------ roots


def test_both_roots_are_walked_and_the_root_names_the_backend(
        tmp_path, claude_projects, copilot_projects, client):
    target = tmp_path / "p"
    target.mkdir()
    a = _write_transcript(claude_projects, "sess-claude", str(target), [_user("one")])
    b = _write_transcript(copilot_projects, "sess-copilot", str(target), [_user("two")])

    assert tasks_store.transcripts() == [str(a), str(b)]
    assert tasks_store.transcripts_by_backend() == [
        ("claude", str(a)), ("copilot", str(b))]
    assert roots.backend_for_transcript(str(b)) == "copilot"
    # A patched Claude root is under neither real root, and reads as Claude.
    assert roots.backend_for_transcript(str(a)) == "claude"

    rows = {r["key"]: r for r in client.get("/api/tasks").json()["tasks"]}
    assert rows["sess-copilot"]["backend"] == "copilot"
    assert rows["sess-claude"]["backend"] == "claude"
    pulse = {r["key"]: r for r in client.get("/api/tasks/pulse").json()["tasks"]}
    assert pulse["sess-copilot"]["backend"] == "copilot"

    # The sessions router's scans see the Copilot transcript too.
    summaries = client.get("/api/claude-sessions/summaries").json()["sessions"]
    assert {s["session_id"] for s in summaries} >= {"sess-claude", "sess-copilot"}
    folders = client.get("/api/claude-sessions").json()["folders"]
    assert [f["path"] for f in folders] == [str(target)]


def test_transcript_path_finds_a_copilot_transcript(tmp_path, copilot_projects):
    target = tmp_path / "p"
    target.mkdir()
    b = _write_transcript(copilot_projects, "sess-copilot", str(target), [_user("two")])
    assert session_liveness.transcript_path("sess-copilot") == str(b)
    assert roots.find_transcript("sess-copilot", str(target)) == ("copilot", str(b))
    assert roots.backend_for_session("sess-copilot") == "copilot"
    assert roots.backend_for_session("nobody") == "claude"


def test_the_liveness_route_accepts_a_path_under_the_copilot_root(
        tmp_path, copilot_projects, client):
    target = tmp_path / "p"
    target.mkdir()
    b = _write_transcript(copilot_projects, "sess-copilot", str(target), [_user("two")])
    r = client.get("/api/claude-sessions/liveness", params={"path": str(b)})
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True
    r = client.get("/api/claude-sessions/liveness",
                   params={"path": str(tmp_path / "elsewhere.jsonl")})
    assert r.status_code == 400


def test_the_registry_watcher_reads_both_sessions_dirs(tmp_path, home, monkeypatch):
    claude_sessions = tmp_path / "claude-sessions"
    claude_sessions.mkdir()
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(claude_sessions))
    copilot_sessions = home / "copilot" / "sessions"
    copilot_sessions.mkdir(parents=True)
    monkeypatch.setattr(tasks_watch, "_pid_alive", lambda pid: True)
    (claude_sessions / "11.json").write_text(json.dumps(
        {"sessionId": "s-claude", "pid": 11, "cwd": "/a", "status": "busy"}))
    (copilot_sessions / "22.json").write_text(json.dumps(
        {"sessionId": "s-copilot", "pid": 22, "cwd": "/a", "status": "idle"}))
    tasks_watch._read_registry()
    assert tasks_watch.session_for_pid(11) == "s-claude"
    assert tasks_watch.session_for_pid(22) == "s-copilot"
    with tasks_watch._cond:
        assert set(tasks_watch._registry) >= {"s-claude", "s-copilot"}


# ------------------------------------------------------------------ backend field


def test_the_settings_record_carries_the_backend_once(client):
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-x", "backend": "copilot"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "model": "", "effort": "", "backend": "copilot"}
    assert client.get("/api/tasks/settings",
                      params={"session_id": "sess-x"}).json()["backend"] == "copilot"
    # A pill pick says nothing about the backend and leaves it alone.
    client.post("/api/tasks/settings", headers=FUSED,
                json={"session_id": "sess-x", "effort": "low"})
    state = tasks_store.settings_state()
    assert tasks_store.session_backend(state, "sess-x") == "copilot"
    assert tasks_store.session_settings(state, "sess-x") == ("", "low")
    # …and it never changes (D2).
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-x", "backend": "claude"})
    assert r.status_code == 400
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-x", "backend": "gemini"})
    assert r.status_code == 400


def test_the_recorded_backend_leads_the_row(tmp_path, claude_projects, client):
    target = tmp_path / "p"
    target.mkdir()
    _write_transcript(claude_projects, "sess-r", str(target), [_user("one")])
    tasks_store.record_settings("sess-r", backend="copilot")
    rows = {r["key"]: r for r in client.get("/api/tasks").json()["tasks"]}
    assert rows["sess-r"]["backend"] == "copilot"


# ------------------------------------------------------------------ gates


def test_create_refuses_a_disabled_or_unknown_backend(tmp_path, client):
    target = tmp_path / "p"
    target.mkdir()
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "backend": "copilot"})
    assert r.status_code == 400, r.text
    assert r.json()["reason"] == "backend_disabled"
    assert r.json()["backend"] == "copilot"
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "backend": "gemini"})
    assert r.status_code == 400
    # /api/schedule takes the same gate through `schedule.create`'s ValueError.
    with pytest.raises(ValueError):
        schedule.create(str(target), "go", backend="copilot")
    r = client.post("/api/schedule", headers=FUSED,
                    json={"target": str(target), "message": "go",
                          "due": (datetime.now(timezone.utc)
                                  + timedelta(hours=1)).isoformat(),
                          "backend": "copilot"})
    assert r.status_code == 400, r.text


def test_create_stores_the_backend_and_the_send_passes_it(tmp_path, monkeypatch):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    calls = []

    def fake_spawn(t, prompt, permission_mode, session_id="", **kw):
        calls.append(kw)
        return {"run_id": "r-1"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    entry = schedule.create(str(target), "go", datetime.now(timezone.utc)
                            - timedelta(seconds=5), backend="copilot")
    assert entry["backend"] == "copilot"
    schedule.tick()
    assert calls and calls[0]["backend"] == "copilot"
    # A Claude send keeps the call shape it always had: no `backend` kwarg.
    schedule.create(str(target), "again", datetime.now(timezone.utc)
                    - timedelta(seconds=5))
    schedule.tick()
    assert "backend" not in calls[1]


def test_the_tick_marks_a_disabled_backends_entry_error_without_spawning(
        tmp_path, monkeypatch):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    entry = schedule.create(str(target), "go", datetime.now(timezone.utc)
                            - timedelta(seconds=5), backend="copilot")
    prefs_mod.write_pref("copilot_enabled", False)
    spawned = []
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: spawned.append(1) or {"run_id": "r"})
    schedule.tick()
    stored = next(e for e in schedule.list_entries() if e["id"] == entry["id"])
    assert stored["state"] == schedule.ERROR
    assert stored["error"] == "Copilot disabled"
    assert spawned == []


def test_spawn_helper_hands_backend_to_start(monkeypatch):
    seen = {}

    class Agent:
        @staticmethod
        def _start(*a, **kw):
            seen.update(kw)
            return {"run_id": "r"}

    monkeypatch.setattr(claude_spawn, "load_agent", lambda: Agent)
    claude_spawn.spawn_helper("/tmp", "hi", "auto", backend="copilot")
    assert seen["backend"] == "copilot"
    claude_spawn.spawn_helper("/tmp", "hi", "auto")
    assert seen["backend"] == ""


# ------------------------------------------------------------------ prefs


def test_the_last_enabled_agent_cannot_be_switched_off(client):
    r = client.put("/api/prefs", headers=FUSED, json={"claude_enabled": False})
    assert r.status_code == 400
    assert r.json()["error"] == "turn on the other agent first"
    # Both in one body is fine: the merged result has one on.
    r = client.put("/api/prefs", headers=FUSED,
                   json={"claude_enabled": False, "copilot_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == {"claude": False, "copilot": True,
                                  "default": "copilot"}
    assert prefs_mod.read_prefs()["default_backend"] == "copilot"
    assert registry.enabled_ids() == ["copilot"]
    # And now Copilot is the last one.
    r = client.put("/api/prefs", headers=FUSED, json={"copilot_enabled": False})
    assert r.status_code == 400


def test_default_backend_is_repointed_in_the_same_write(client):
    r = client.put("/api/prefs", headers=FUSED,
                   json={"copilot_enabled": True, "default_backend": "copilot"})
    assert r.json()["agents"]["default"] == "copilot"
    r = client.put("/api/prefs", headers=FUSED, json={"copilot_enabled": False})
    assert r.status_code == 200
    assert r.json()["agents"] == {"claude": True, "copilot": False,
                                  "default": "claude"}
    assert prefs_mod.read_prefs()["default_backend"] == "claude"
    r = client.put("/api/prefs", headers=FUSED, json={"default_backend": "gemini"})
    assert r.status_code == 400
    r = client.put("/api/prefs", headers=FUSED, json={"claude_enabled": "yes"})
    assert r.status_code == 400
    body = client.get("/api/prefs").json()
    assert body["agents"] == {"claude": True, "copilot": False, "default": "claude"}
    assert body["model"]["choices"] == list(prefs_mod.VALID_DEFAULT_MODELS)


def test_a_copilot_default_never_reaches_fused_ai(monkeypatch):
    from fused_render.server import ai as ai_mod
    prefs_mod.write_pref("default_model", "opus")
    assert ai_mod.default_model() == "opus"
    prefs_mod.write_pref("copilot_enabled", True)
    prefs_mod.write_pref("default_backend", "copilot")
    assert ai_mod.default_backend() == "copilot"
    # The relay's own resolution: with Copilot the default the pref is ignored.
    src = open(ai_mod.__file__, encoding="utf-8").read()
    assert 'default_backend() == "claude"' in src


# ------------------------------------------------------------------ catalog


def test_the_catalog_endpoint_serves_the_registry(client, monkeypatch):
    from fused_render import claude_health
    monkeypatch.setattr(claude_health, "summary", lambda: {"ok": True})
    body = client.get("/api/agent-backends").json()
    assert [b["id"] for b in body["backends"]] == ["claude"]
    assert [b["id"] for b in body["all"]] == ["claude", "copilot"]
    assert body["default"] == "claude"
    claude = body["all"][0]
    assert claude["health"] == {"ok": True}
    assert claude["caps"]["plan_mode"] is True
    assert body["all"][1]["enabled"] is False
    _enable_copilot()
    body = client.get("/api/agent-backends").json()
    assert [b["id"] for b in body["backends"]] == ["claude", "copilot"]


# ------------------------------------------------------------------ erase (D15)


def test_erase_under_the_copilot_root_takes_the_cli_session_state_too(
        tmp_path, claude_projects, copilot_projects, client, monkeypatch):
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(copilot_projects, "sess-copilot", str(target), [_user("two")])
    sidecar = path.parent / "sess-copilot"
    sidecar.mkdir()
    (sidecar / "x.jsonl").write_text("{}\n")
    state = tmp_path / "copilot-session-state"
    (state / "sess-copilot").mkdir(parents=True)
    (state / "sess-copilot" / "events.jsonl").write_text("{}\n")
    (state / "sess-other").mkdir()
    monkeypatch.setattr(tasks_mod, "_copilot_session_state_dir", lambda: str(state))

    assert client.get("/api/tasks").json()["tasks"][0]["key"] == "sess-copilot"
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": "sess-copilot"})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert not sidecar.exists()
    assert not (state / "sess-copilot").exists()
    assert (state / "sess-other").exists()


def test_erase_of_a_claude_session_leaves_the_copilot_cli_store_alone(
        tmp_path, claude_projects, client, monkeypatch):
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(claude_projects, "sess-a", str(target), [_user("one")])
    state = tmp_path / "copilot-session-state"
    (state / "sess-a").mkdir(parents=True)
    monkeypatch.setattr(tasks_mod, "_copilot_session_state_dir", lambda: str(state))
    assert client.get("/api/tasks").json()["tasks"][0]["key"] == "sess-a"
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert (state / "sess-a").exists()


# ------------------------------------------------------------------ invariant 7


_LITERAL = re.compile(r'expanduser\(\s*"~/\.claude"\s*\)'
                      r'|environ(\.get)?\(\s*"CLAUDE_CONFIG_DIR"'
                      r'|environ\[\s*"CLAUDE_CONFIG_DIR"')


def test_no_module_touched_here_spells_the_claude_root_itself():
    """Invariant 7: the roots come from `agent_roots` only. Comments and
    docstrings may still SAY `~/.claude`; code may not resolve it."""
    import fused_render
    base = os.path.dirname(fused_render.__file__)
    modules = [
        "tasks_store.py", "session_liveness.py", "tasks_watch.py",
        "claude_artifacts.py", "claude_session_move.py", "schedule.py",
        "claude_spawn.py", "server/routers/claude_sessions.py",
        "server/routers/tasks.py", "server/routers/agent_backends.py",
        "agent_backends/roots.py", "server/ai.py", "shell/prefs.py",
    ]
    offenders = []
    for rel in modules:
        with open(os.path.join(base, rel), encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                code = line.split("#", 1)[0]
                if _LITERAL.search(code):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == []
