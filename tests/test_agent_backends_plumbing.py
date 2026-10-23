"""Server plumbing for the second, third and fourth agent backends
(docs/COPILOT_BACKEND.md, docs/CODEX_BACKEND.md and docs/GEMINI_BACKEND.md,
WS-A): the root LIST every transcript reader walks, the `backend` field on
the task row and the settings record, the Preferences switches' rule, the
scheduler's disabled gate, erase under the Copilot, Codex and Gemini roots,
the catalog endpoint, and the grep-tests behind invariants 7 and 16 (and
16's Gemini twin).

Every path is under tmp: FUSED_RENDER_HOME (which `copilot_dir()`,
`codex_dir()` and `gemini_dir()` hang off), CODEX_HOME, GEMINI_CLI_HOME, and
the Claude roots the modules expose for patching.
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
def codex_projects(home):
    d = home / "codex" / "projects"
    d.mkdir(parents=True)
    assert roots.projects_dir("codex") == str(d)
    return d


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    """Codex CLI's own home, where its rollout files live — redirected for
    every test, so no erase here can ever reach the real `~/.codex`."""
    d = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(d))
    return d


@pytest.fixture()
def gemini_projects(home):
    d = home / "gemini" / "projects"
    d.mkdir(parents=True)
    assert roots.projects_dir("gemini") == str(d)
    return d


@pytest.fixture(autouse=True)
def gemini_home(tmp_path, monkeypatch):
    """Gemini CLI's own data dir (`<GEMINI_CLI_HOME>/.gemini`), where its
    chat files live — redirected for every test, so no erase here can ever
    reach the real `~/.gemini`."""
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini-user"))
    d = tmp_path / "gemini-user" / ".gemini"
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


def _enable_codex():
    prefs_mod.write_pref("codex_enabled", True)


def _enable_gemini():
    prefs_mod.write_pref("gemini_enabled", True)


# ------------------------------------------------------------------ roots


def test_every_root_list_names_four_roots_in_backends_order(
        tmp_path, home, claude_projects):
    """docs/CODEX_BACKEND.md §1 and docs/GEMINI_BACKEND.md §1: a third and a
    fourth root everywhere a list is built — the shared file's own lists,
    the app-side helpers a module with a patchable Claude constant builds
    through, and the modules' own doors. `codex_dir()` and `gemini_dir()`
    hang off the UNNESTED home, as `copilot_dir()` does."""
    assert roots.BACKENDS == ("claude", "copilot", "codex", "gemini")
    assert roots.codex_dir() == str(home / "codex")
    assert roots.base_dir("codex") == str(home / "codex")
    assert roots.projects_dir("codex") == str(home / "codex" / "projects")
    assert roots.sessions_dir("codex") == str(home / "codex" / "sessions")
    assert roots.gemini_dir() == str(home / "gemini")
    assert roots.base_dir("gemini") == str(home / "gemini")
    assert roots.projects_dir("gemini") == str(home / "gemini" / "projects")
    assert roots.sessions_dir("gemini") == str(home / "gemini" / "sessions")
    claude = roots.claude_dir()
    assert roots.transcript_roots() == [
        ("claude", os.path.join(claude, "projects")),
        ("copilot", str(home / "copilot" / "projects")),
        ("codex", str(home / "codex" / "projects")),
        ("gemini", str(home / "gemini" / "projects"))]
    assert roots.registry_roots() == [
        ("claude", os.path.join(claude, "sessions")),
        ("copilot", str(home / "copilot" / "sessions")),
        ("codex", str(home / "codex" / "sessions")),
        ("gemini", str(home / "gemini" / "sessions"))]
    assert roots.projects_roots("/c/projects") == [
        ("claude", "/c/projects"),
        ("copilot", str(home / "copilot" / "projects")),
        ("codex", str(home / "codex" / "projects")),
        ("gemini", str(home / "gemini" / "projects"))]
    assert roots.sessions_roots("/c/sessions") == [
        ("claude", "/c/sessions"),
        ("copilot", str(home / "copilot" / "sessions")),
        ("codex", str(home / "codex" / "sessions")),
        ("gemini", str(home / "gemini" / "sessions"))]
    # The routers' and the store's doors carry the patched Claude root first.
    assert [b for b, _ in sessions_mod.projects_roots()] == list(roots.BACKENDS)
    assert sessions_mod.projects_roots()[0][1] == str(claude_projects)
    assert [b for b, _ in tasks_store.projects_roots()] == list(roots.BACKENDS)


def test_every_root_is_walked_and_the_root_names_the_backend(
        tmp_path, claude_projects, copilot_projects, codex_projects,
        gemini_projects, client):
    target = tmp_path / "p"
    target.mkdir()
    a = _write_transcript(claude_projects, "sess-claude", str(target), [_user("one")])
    b = _write_transcript(copilot_projects, "sess-copilot", str(target), [_user("two")])
    c = _write_transcript(codex_projects, "sess-codex", str(target), [_user("three")])
    d = _write_transcript(gemini_projects, "sess-gemini", str(target), [_user("four")])

    assert tasks_store.transcripts() == [str(a), str(b), str(c), str(d)]
    assert tasks_store.transcripts_by_backend() == [
        ("claude", str(a)), ("copilot", str(b)), ("codex", str(c)), ("gemini", str(d))]
    assert roots.backend_for_transcript(str(b)) == "copilot"
    assert roots.backend_for_transcript(str(c)) == "codex"
    assert roots.backend_for_transcript(str(d)) == "gemini"
    assert roots.find_transcript("sess-gemini", str(target)) == ("gemini", str(d))
    assert roots.backend_for_session("sess-gemini") == "gemini"
    # A patched Claude root is under neither real root, and reads as Claude.
    assert roots.backend_for_transcript(str(a)) == "claude"
    assert roots.find_transcript("sess-codex", str(target)) == ("codex", str(c))
    assert roots.backend_for_session("sess-codex") == "codex"

    rows = {r["key"]: r for r in client.get("/api/tasks").json()["tasks"]}
    assert rows["sess-copilot"]["backend"] == "copilot"
    assert rows["sess-codex"]["backend"] == "codex"
    assert rows["sess-gemini"]["backend"] == "gemini"
    assert rows["sess-claude"]["backend"] == "claude"
    pulse = {r["key"]: r for r in client.get("/api/tasks/pulse").json()["tasks"]}
    assert pulse["sess-copilot"]["backend"] == "copilot"
    assert pulse["sess-codex"]["backend"] == "codex"
    assert pulse["sess-gemini"]["backend"] == "gemini"

    # The sessions router's scans see every non-Claude transcript too.
    summaries = client.get("/api/claude-sessions/summaries").json()["sessions"]
    assert {s["session_id"] for s in summaries} >= {
        "sess-claude", "sess-copilot", "sess-codex", "sess-gemini"}
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


def test_the_registry_watcher_reads_every_sessions_dir(tmp_path, home, monkeypatch):
    claude_sessions = tmp_path / "claude-sessions"
    claude_sessions.mkdir()
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(claude_sessions))
    copilot_sessions = home / "copilot" / "sessions"
    copilot_sessions.mkdir(parents=True)
    codex_sessions = home / "codex" / "sessions"
    codex_sessions.mkdir(parents=True)
    gemini_sessions = home / "gemini" / "sessions"
    gemini_sessions.mkdir(parents=True)
    monkeypatch.setattr(tasks_watch, "_pid_alive", lambda pid: True)
    (claude_sessions / "11.json").write_text(json.dumps(
        {"sessionId": "s-claude", "pid": 11, "cwd": "/a", "status": "busy"}))
    (copilot_sessions / "22.json").write_text(json.dumps(
        {"sessionId": "s-copilot", "pid": 22, "cwd": "/a", "status": "idle"}))
    (codex_sessions / "55.json").write_text(json.dumps(
        {"sessionId": "s-codex", "pid": 55, "cwd": "/a", "status": "busy"}))
    (gemini_sessions / "77.json").write_text(json.dumps(
        {"sessionId": "s-gemini", "pid": 77, "cwd": "/a", "status": "idle"}))
    tasks_watch._read_registry()
    assert tasks_watch.session_for_pid(11) == "s-claude"
    assert tasks_watch.session_for_pid(22) == "s-copilot"
    assert tasks_watch.session_for_pid(55) == "s-codex"
    assert tasks_watch.session_for_pid(77) == "s-gemini"
    with tasks_watch._cond:
        assert set(tasks_watch._registry) >= {"s-claude", "s-copilot", "s-codex", "s-gemini"}


# ------------------------------------------------------------------ backend field


def test_the_settings_record_carries_the_backend_once(client):
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-x", "backend": "copilot"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "model": "", "effort": "", "backend": "copilot",
                        "permission": ""}
    assert client.get("/api/tasks/settings",
                      params={"session_id": "sess-x"}).json()["backend"] == "copilot"
    # The third agent is a backend the record takes like the others.
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-y", "backend": "codex"})
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "codex"
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-z", "backend": "gemini"})
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "gemini"
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
    # No opinion is the default AT CREATE, stored, and said out loud at send:
    # `_start` reads "" as Claude, not as the Preferences default.
    again = schedule.create(str(target), "again", datetime.now(timezone.utc)
                            - timedelta(seconds=5))
    assert again["backend"] == "claude"
    schedule.tick()
    assert calls[1]["backend"] == "claude"
    assert calls[1]["bot"] == "", "no bot: the Claude switch applies in `_start`"


def _past():
    return datetime.now(timezone.utc) - timedelta(seconds=5)


def _strip_backend(entry_id):
    """Rewrite one stored entry as a pre-field one: no `backend` key at all."""
    entries = schedule._read()
    for e in entries:
        if e["id"] == entry_id:
            e.pop("backend", None)
    schedule._write(entries)


def test_the_backend_is_pinned_at_create_not_at_send(tmp_path, monkeypatch):
    _enable_copilot()
    prefs_mod.write_pref("default_backend", "copilot")
    target = tmp_path / "p"
    target.mkdir()
    calls = []
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **kw: calls.append(kw) or {"run_id": "r"})
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    entry = schedule.create(str(target), "go", _past())
    assert entry["backend"] == "copilot"
    # The default moves before the entry fires; the entry does not.
    prefs_mod.write_pref("default_backend", "claude")
    schedule.tick()
    assert calls[0]["backend"] == "copilot"


def test_a_legacy_entry_runs_on_claude_whatever_the_default(tmp_path, monkeypatch):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    calls = []
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **kw: calls.append(kw) or {"run_id": "r"})
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    entry = schedule.create(str(target), "go", _past())
    _strip_backend(entry["id"])
    prefs_mod.write_pref("default_backend", "copilot")
    schedule.tick()
    assert calls[0]["backend"] == "claude"
    # …and a re-ask of it is a Claude entry too, never the default of the day.
    schedule._update(entry["id"], state=schedule.SENT, turn="failed",
                     error="the turn died", claude_session_id="sess-old")
    _strip_backend(entry["id"])
    result = schedule.resend(entry["id"])
    assert result["ok"] is True, result
    assert result["entry"]["backend"] == "claude"


def test_a_session_bound_entry_takes_the_sessions_agent(tmp_path, copilot_projects):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    prefs_mod.write_pref("default_backend", "copilot")
    # No record and no transcript: every pre-existing session is Claude's.
    assert schedule.create(str(target), "go", _past(),
                           session_id="sess-old")["backend"] == "claude"
    # The record leads; the root its transcript sits under is the fallback.
    tasks_store.record_settings("sess-rec", backend="claude")
    assert schedule.create(str(target), "go", _past(),
                           session_id="sess-rec")["backend"] == "claude"
    prefs_mod.write_pref("default_backend", "claude")
    _write_transcript(copilot_projects, "sess-cop", str(target), [_user("hi")])
    assert schedule.create(str(target), "go", _past(),
                           session_id="sess-cop")["backend"] == "copilot"


def test_a_follower_takes_its_leaders_agent(tmp_path):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    leader = schedule.create(str(target), "first", _past(), backend="copilot")
    follower = schedule.create(str(target), "second", _past(),
                               follow_of=leader["id"])
    assert follower["backend"] == "copilot"


def test_tasks_create_pins_the_default_on_the_pending_row(tmp_path, client):
    _enable_copilot()
    prefs_mod.write_pref("default_backend", "copilot")
    target = tmp_path / "p"
    target.mkdir()
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target),
                          "due": (datetime.now(timezone.utc)
                                  + timedelta(hours=1)).isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    stored = next(e for e in schedule.list_entries()
                  if e["id"] == body["entry_id"])
    assert stored["backend"] == "copilot"
    prefs_mod.write_pref("default_backend", "claude")
    rows = {t["key"]: t for t in client.get("/api/tasks").json()["tasks"]}
    assert rows[body["key"]]["backend"] == "copilot"


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
    # `bot` is always passed too ("" for no bot), so `_start` can let a bot's
    # Claude task through the Claude switch.
    assert seen["bot"] == ""
    claude_spawn.spawn_helper("/tmp", "hi", "auto", backend="claude", bot="bot-1")
    assert seen["bot"] == "bot-1"


# ------------------------------------------------------------------ prefs


def test_the_last_enabled_agent_cannot_be_switched_off(client):
    r = client.put("/api/prefs", headers=FUSED, json={"claude_enabled": False})
    assert r.status_code == 400
    assert r.json()["error"] == "turn on another agent first"
    # Both in one body is fine: the merged result has one on.
    r = client.put("/api/prefs", headers=FUSED,
                   json={"claude_enabled": False, "copilot_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == {"claude": False, "copilot": True,
                                  "codex": False, "gemini": False, "default": "copilot"}
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
                                  "codex": False, "gemini": False, "default": "claude"}
    assert prefs_mod.read_prefs()["default_backend"] == "claude"
    r = client.put("/api/prefs", headers=FUSED, json={"default_backend": "nope"})
    assert r.status_code == 400
    r = client.put("/api/prefs", headers=FUSED, json={"claude_enabled": "yes"})
    assert r.status_code == 400
    body = client.get("/api/prefs").json()
    assert body["agents"] == {"claude": True, "copilot": False, "codex": False,
                              "gemini": False, "default": "claude"}


def test_codex_is_off_until_stored_on_and_takes_part_in_the_rule(client):
    """docs/CODEX_BACKEND.md D17: a third switch, off by default; the PUT
    rule is "at least one of the THREE on", judged on the merged record."""
    assert prefs_mod.codex_enabled() is False
    assert prefs_mod.AGENT_BACKEND_KEYS["codex"] == "codex_enabled"
    assert prefs_mod.VALID_DEFAULT_BACKENDS == registry.ORDER
    body = client.get("/api/prefs").json()
    assert body["agents"]["codex"] is False
    r = client.put("/api/prefs", headers=FUSED, json={"codex_enabled": "yes"})
    assert r.status_code == 400
    # Codex on, then Claude and Copilot off in one body: two off with the
    # third on is fine, and the default walks ORDER to the one left.
    r = client.put("/api/prefs", headers=FUSED,
                   json={"codex_enabled": True, "copilot_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == {"claude": True, "copilot": True,
                                  "codex": True, "gemini": False, "default": "claude"}
    r = client.put("/api/prefs", headers=FUSED,
                   json={"claude_enabled": False, "copilot_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == {"claude": False, "copilot": False,
                                  "codex": True, "gemini": False, "default": "codex"}
    assert prefs_mod.read_prefs()["default_backend"] == "codex"
    assert registry.enabled_ids() == ["codex"]
    assert registry.default_id() == "codex"
    # All three off is refused whole — nothing in that body is written.
    r = client.put("/api/prefs", headers=FUSED,
                   json={"codex_enabled": False, "default_backend": "claude"})
    assert r.status_code == 400
    assert r.json()["error"] == "turn on another agent first"
    assert prefs_mod.read_prefs()["codex_enabled"] is True
    assert prefs_mod.read_prefs()["default_backend"] == "codex"


def test_the_default_repoints_to_the_first_enabled_in_order(client):
    r = client.put("/api/prefs", headers=FUSED,
                   json={"copilot_enabled": True, "codex_enabled": True,
                         "default_backend": "codex"})
    assert r.status_code == 200, r.text
    assert r.json()["agents"]["default"] == "codex"
    # Codex goes off: Claude and Copilot are both on, Claude is first.
    r = client.put("/api/prefs", headers=FUSED, json={"codex_enabled": False})
    assert r.json()["agents"]["default"] == "claude"
    # A default naming an agent that is off in the same body is re-pointed.
    r = client.put("/api/prefs", headers=FUSED,
                   json={"claude_enabled": False, "default_backend": "claude"})
    assert r.status_code == 200, r.text
    assert r.json()["agents"]["default"] == "copilot"
    r = client.put("/api/prefs", headers=FUSED, json={"default_backend": "codex"})
    assert r.status_code == 200
    assert prefs_mod.read_prefs()["default_backend"] == "copilot"


def test_the_unknown_key_message_names_the_codex_and_gemini_switches(client):
    r = client.put("/api/prefs", headers=FUSED, json={"nothing_known": 1})
    assert r.status_code == 400
    assert "'codex_enabled'" in r.json()["error"]
    assert "'gemini_enabled'" in r.json()["error"]


def test_gemini_is_off_until_stored_on_and_takes_part_in_the_rule(client):
    """docs/GEMINI_BACKEND.md D17: a fourth switch, off by default; the PUT
    rule is "at least one of the FOUR on", judged on the merged record."""
    assert prefs_mod.gemini_enabled() is False
    assert prefs_mod.AGENT_BACKEND_KEYS["gemini"] == "gemini_enabled"
    assert prefs_mod.VALID_DEFAULT_BACKENDS == registry.ORDER
    assert registry.ORDER[-1] == "gemini"
    body = client.get("/api/prefs").json()
    assert body["agents"]["gemini"] is False
    r = client.put("/api/prefs", headers=FUSED, json={"gemini_enabled": "yes"})
    assert r.status_code == 400
    # Gemini on, then the other three off in one body: the default walks
    # ORDER to the one left.
    r = client.put("/api/prefs", headers=FUSED, json={"gemini_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == {"claude": True, "copilot": False, "codex": False,
                                  "gemini": True, "default": "claude"}
    r = client.put("/api/prefs", headers=FUSED, json={"claude_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == {"claude": False, "copilot": False, "codex": False,
                                  "gemini": True, "default": "gemini"}
    assert registry.enabled_ids() == ["gemini"]
    assert registry.default_id() == "gemini"
    # All four off is refused whole.
    r = client.put("/api/prefs", headers=FUSED, json={"gemini_enabled": False})
    assert r.status_code == 400
    assert r.json()["error"] == "turn on another agent first"
    assert prefs_mod.read_prefs()["gemini_enabled"] is True
    # Codex on beside it: Codex comes first in ORDER, so the default re-points
    # there once Gemini goes off.
    r = client.put("/api/prefs", headers=FUSED,
                   json={"codex_enabled": True, "default_backend": "gemini"})
    assert r.json()["agents"]["default"] == "gemini"
    r = client.put("/api/prefs", headers=FUSED, json={"gemini_enabled": False})
    assert r.json()["agents"]["default"] == "codex"


def test_fused_ai_never_reads_the_default_agent(monkeypatch):
    # The relay is Claude-only and takes no default from any preference, so
    # which agent new chats open on cannot reach it either way.
    from fused_render.server import ai as ai_mod
    src = open(ai_mod.__file__, encoding="utf-8").read()
    assert "default_id()" not in src
    assert "default_backend()" not in src


# ------------------------------------------------------------------ catalog


def test_the_catalog_endpoint_serves_the_registry(client, monkeypatch):
    from fused_render import claude_health
    # The catalog reads the CACHED snapshot only, never a probe (`catalog`),
    # with the cache's bookkeeping stripped.
    monkeypatch.setattr(claude_health, "cached",
                        lambda: {"ok": True, "fingerprint": "x"})
    monkeypatch.setattr(claude_health, "summary",
                        lambda: pytest.fail("the catalog must not probe"))
    from fused_render import codex_health, copilot_health, gemini_health
    # A disabled agent's health is read from its cache too, never probed.
    monkeypatch.setattr(copilot_health, "cached", lambda: None)
    monkeypatch.setattr(codex_health, "cached", lambda: None)
    monkeypatch.setattr(gemini_health, "cached", lambda: None)
    body = client.get("/api/agent-backends").json()
    assert [b["id"] for b in body["backends"]] == ["claude"]
    assert [b["id"] for b in body["all"]] == ["claude", "copilot", "codex", "gemini"]
    assert [b["id"] for b in body["all"]] == list(registry.ORDER)
    assert body["default"] == "claude"
    claude = body["all"][0]
    assert claude["health"] == {"ok": True}
    assert claude["caps"]["plan_mode"] is True
    assert body["all"][1]["enabled"] is False
    codex = body["all"][2]
    assert codex["enabled"] is False
    assert codex["label"] == "Codex"
    assert codex["default_model"] == "gpt-6-luna"
    assert codex["modes"] == ["prompt", "acceptEdits", "auto"]
    assert codex["caps"]["plan_mode"] is False
    assert codex["caps"]["live_model_switch"] is True
    assert codex["caps"]["live_mode_switch"] is True
    assert codex["health"] is None
    gemini = body["all"][3]
    assert gemini["enabled"] is False
    assert gemini["label"] == "Gemini"
    assert gemini["default_model"] == "auto"
    assert gemini["modes"] == ["prompt", "acceptEdits", "auto"]
    # No effort control at all: an empty vocabulary, every model `reasoning`
    # off, so the pill never shows (docs/GEMINI_BACKEND.md D7).
    assert gemini["efforts"] == []
    assert all(m["reasoning"] is False and m["efforts"] == [] for m in gemini["models"])
    assert [m["id"] for m in gemini["models"]][:2] == ["auto", "gemini-3-pro-preview"]
    assert gemini["caps"]["plan_mode"] is False
    assert gemini["caps"]["live_model_switch"] is True
    assert gemini["caps"]["live_mode_switch"] is True
    assert gemini["health"] is None
    _enable_copilot()
    body = client.get("/api/agent-backends").json()
    assert [b["id"] for b in body["backends"]] == ["claude", "copilot"]
    _enable_codex()
    body = client.get("/api/agent-backends").json()
    assert [b["id"] for b in body["backends"]] == ["claude", "copilot", "codex"]
    _enable_gemini()
    body = client.get("/api/agent-backends").json()
    assert [b["id"] for b in body["backends"]] == ["claude", "copilot", "codex", "gemini"]


def test_geminis_terminal_resume_line():
    gemini = registry.get("gemini")
    assert gemini.terminal_resume("/a b", "3f2c6c1e-1111-4222-8333-444444444444") == (
        "cd '/a b' && gemini --resume 3f2c6c1e-1111-4222-8333-444444444444")
    assert gemini.terminal_resume("/a", "") == "cd /a && gemini"
    assert gemini.routes_prefix == "/api/gemini"
    assert gemini.pref_key == "gemini_enabled"
    assert getattr(prefs_mod, gemini.pref_key)() is False


def test_the_catalog_serves_geminis_discovered_models(monkeypatch):
    """docs/GEMINI_BACKEND.md D11a: the health snapshot's `models` (off
    `session/new`'s `availableModels`) replace the shortlist, `auto` first,
    every row effortless; nothing cached → the shortlist stands."""
    from fused_render import gemini_health
    from fused_render.agent_backends import gemini as backend_gemini
    monkeypatch.setattr(gemini_health, "cached_models", lambda: None)
    assert backend_gemini.discovered_models(None) is None
    assert backend_gemini.GEMINI.model_ids() == tuple(m.id for m in backend_gemini.MODELS)
    rows = [{"id": "auto", "label": "Auto"}, {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro"},
            {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"}, {"id": "", "label": "x"}, "junk"]
    found = backend_gemini.discovered_models({"models": rows})
    assert [m.id for m in found] == ["auto", "gemini-3.1-pro-preview", "gemini-2.5-flash"]
    assert all(m.reasoning is False and m.effort_list(()) == [] for m in found)
    monkeypatch.setattr(gemini_health, "cached_models", lambda: rows)
    assert backend_gemini.GEMINI.model_ids(discovered=True) == (
        "auto", "gemini-3.1-pro-preview", "gemini-2.5-flash")
    # A discovered id is never foreign on a Gemini task; a Codex id still is.
    assert registry.foreign_model_error("gemini", "gemini-3.1-pro-preview") is None
    assert registry.foreign_model_error("gemini", "gpt-6-luna")["reason"] == "unsupported"
    assert registry.foreign_model_error("claude", "gemini-2.5-pro")["reason"] == "unsupported"


def test_codexs_terminal_resume_line():
    codex = registry.get("codex")
    assert codex.terminal_resume("/a b", "01a15f4b-da04") == (
        "cd '/a b' && codex resume 01a15f4b-da04")
    assert codex.terminal_resume("/a", "") == "cd /a && codex"
    assert codex.routes_prefix == "/api/codex"
    assert codex.pref_key == "codex_enabled"
    # `pref_key` names a function prefs really has (base.Backend's contract).
    assert getattr(prefs_mod, codex.pref_key)() is False


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


def _rollout(codex_home, sub, rel_dir, session_id, ts="2026-10-21T10-00-00"):
    d = codex_home / sub / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-{ts}-{session_id}.jsonl"
    p.write_text("{}\n")
    return p


CODEX_SID = "01a15f4b-da04-7b11-8b21-41c96595aa15"
OTHER_SID = "01a15f4b-da04-7b11-8b21-000000000000"


def test_erase_under_the_codex_root_takes_the_cli_rollouts_too(
        tmp_path, claude_projects, codex_projects, codex_home, client):
    """docs/CODEX_BACKEND.md D15: `codex resume <sid>` replays from Codex's
    own rollout file, so an erase that left it would leave the conversation
    resumable from a terminal. Both stores go — the dated live one and the
    archive — and only this id's files."""
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(codex_projects, CODEX_SID, str(target), [_user("two")])
    live = _rollout(codex_home, "sessions", "2026/10/21", CODEX_SID)
    later = _rollout(codex_home, "sessions", "2026/10/22", CODEX_SID,
                     ts="2026-10-22T08-00-00")
    archived = _rollout(codex_home, "archived_sessions", "", CODEX_SID)
    other = _rollout(codex_home, "sessions", "2026/10/21", OTHER_SID)
    # A file that merely CONTAINS the id is not a rollout of it.
    lookalike = codex_home / "sessions" / "2026" / "10" / "21" / f"notes-{CODEX_SID}.jsonl"
    lookalike.write_text("{}\n")
    assert tasks_mod._codex_home() == str(codex_home)
    assert sorted(tasks_mod._codex_rollout_files(CODEX_SID)) == sorted(
        os.path.realpath(p) for p in (live, later, archived))

    assert client.get("/api/tasks").json()["tasks"][0]["key"] == CODEX_SID
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": CODEX_SID})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert not live.exists() and not later.exists() and not archived.exists()
    assert other.exists() and lookalike.exists()


def test_erase_of_a_claude_session_leaves_the_codex_rollouts_alone(
        tmp_path, claude_projects, codex_home, client):
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(claude_projects, CODEX_SID, str(target), [_user("one")])
    live = _rollout(codex_home, "sessions", "2026/10/21", CODEX_SID)
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": CODEX_SID})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert live.exists()


def test_erase_through_a_symlinked_codex_bucket_takes_the_rollouts_too(
        tmp_path, claude_projects, codex_projects, codex_home, client):
    target = tmp_path / "p"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    path = _write_transcript(elsewhere, CODEX_SID, str(target), [_user("two")])
    (codex_projects / path.parent.name).symlink_to(path.parent)
    live = _rollout(codex_home, "sessions", "2026/10/21", CODEX_SID)
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": CODEX_SID})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert not live.exists()


GEMINI_SID = "3f2c6c1e-5b1a-4c2d-9e3f-0a1b2c3d4e5f"


def _chat(gemini_home, project, session_id, ts="2026-10-23T10-00", ext=".jsonl",
          body_sid=None):
    """A Gemini chat file as the CLI names it: `tmp/<project>/chats/
    session-<ts>-<sid[:8]><ext>`, carrying the whole id inside."""
    d = gemini_home / "tmp" / project / "chats"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"session-{ts}-{session_id[:8]}{ext}"
    p.write_text(json.dumps({"sessionId": body_sid or session_id, "type": "user"}) + "\n")
    return p


def test_erase_under_the_gemini_root_takes_the_cli_chat_files_too(
        tmp_path, claude_projects, gemini_projects, gemini_home, client):
    """docs/GEMINI_BACKEND.md D15: `gemini --resume <sid>` replays from the
    CLI's chat file(s), named by the id's first eight characters; erasing
    the fused transcript removes those too — only the ones whose text
    carries the whole id, so a neighbour sharing the prefix stays."""
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(gemini_projects, GEMINI_SID, str(target), [_user("two")])
    live = _chat(gemini_home, "proj-a", GEMINI_SID)
    older = _chat(gemini_home, "proj-a", GEMINI_SID, ts="2026-10-22T09-00", ext=".json")
    elsewhere = _chat(gemini_home, "proj-b", GEMINI_SID)
    other = _chat(gemini_home, "proj-a", "00000000-0000-4000-8000-000000000000")
    lookalike = _chat(gemini_home, "proj-a", GEMINI_SID, ts="2026-10-21T08-00",
                      body_sid="3f2c6c1e-ffff-4fff-8fff-ffffffffffff")
    assert tasks_mod._gemini_home() == str(gemini_home)
    assert sorted(tasks_mod._gemini_chat_files(GEMINI_SID)) == sorted(
        os.path.realpath(str(p)) for p in (live, older, elsewhere))
    assert client.get("/api/tasks").json()["tasks"][0]["key"] == GEMINI_SID
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": GEMINI_SID})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert not live.exists() and not older.exists() and not elsewhere.exists()
    assert other.exists() and lookalike.exists()


def test_erase_of_a_claude_session_leaves_the_gemini_chat_files_alone(
        tmp_path, claude_projects, gemini_home, client):
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(claude_projects, GEMINI_SID, str(target), [_user("one")])
    live = _chat(gemini_home, "proj-a", GEMINI_SID)
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": GEMINI_SID})
    assert r.status_code == 200, r.text
    assert not path.exists() and live.exists()


def test_the_gemini_chat_glob_refuses_a_bad_id_and_a_symlink_out(tmp_path, gemini_home):
    _chat(gemini_home, "proj-a", "x" * 36)
    for bad in ("..", ".", "a/b", "short"):
        assert tasks_mod._gemini_chat_files(bad) == []
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"sessionId": GEMINI_SID}) + "\n")
    link_dir = gemini_home / "tmp" / "proj-l" / "chats"
    link_dir.mkdir(parents=True)
    (link_dir / f"session-2026-10-23T00-00-{GEMINI_SID[:8]}.jsonl").symlink_to(outside)
    assert tasks_mod._gemini_chat_files(GEMINI_SID) == []
    # No Gemini home at all is simply nothing to remove.
    assert tasks_mod._gemini_chat_files("00000000-0000-4000-8000-000000000000") == []


def test_the_codex_rollout_glob_refuses_a_bad_id_and_a_symlink_out(
        tmp_path, codex_home):
    """The same safety the Copilot path has: an id that is not a name never
    builds a path, and a hit must RESOLVE inside the Codex dir it was globbed
    under — a rollout-named symlink to a file elsewhere is not followed."""
    _rollout(codex_home, "sessions", "2026/10/21", "x")
    for bad in ("..", ".", "a/b", "*", "", "-x"):
        assert tasks_mod._codex_rollout_files(bad) == []
    outside = tmp_path / "precious.jsonl"
    outside.write_text("keep me\n")
    link_dir = codex_home / "sessions" / "2026" / "10" / "23"
    link_dir.mkdir(parents=True)
    (link_dir / f"rollout-2026-10-23T00-00-00-{CODEX_SID}.jsonl").symlink_to(outside)
    assert tasks_mod._codex_rollout_files(CODEX_SID) == []
    # No Codex home at all is simply nothing to remove.
    assert tasks_mod._codex_rollout_files(OTHER_SID) == []


# ------------------------------------------------------------------ round 2


def _copilot_default():
    _enable_copilot()
    prefs_mod.write_pref("default_backend", "copilot")


def test_app_builder_tasks_are_claude_whatever_the_default(tmp_path, monkeypatch):
    from fused_render.server.routers import apps as apps_mod
    _copilot_default()
    page = tmp_path / "app" / "index.html"
    page.parent.mkdir()
    page.write_text("<html></html>")
    monkeypatch.setattr(schedule, "run_now", lambda entry_id: None)
    monkeypatch.setattr(apps_mod, "_await_sent", lambda entry_id, entry: entry)
    entry, error = apps_mod._create_app_task(str(page), "build it")
    assert error is None
    assert entry["backend"] == "claude"


def test_a_bots_task_is_claude_whatever_the_default(tmp_path, client, monkeypatch):
    from fused_render.bots import store as bot_store
    _copilot_default()
    monkeypatch.setattr(bot_store, "list_ids", lambda: ["bot-1"])
    target = tmp_path / "p"
    target.mkdir()
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "due": due,
                          "bot": "bot-1"})
    assert r.status_code == 200, r.text
    stored = next(e for e in schedule.list_entries()
                  if e["id"] == r.json()["entry_id"])
    assert stored["backend"] == "claude"
    # Bots run on Claude: naming another agent for one is refused, whole body.
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "due": due,
                          "bot": "bot-1", "backend": "copilot"})
    assert r.status_code == 400
    assert r.json() == {"error": "bots run on Claude", "reason": "unsupported",
                        "backend": "copilot"}


def test_the_user_plugin_sync_follows_the_claude_switch(tmp_path, monkeypatch):
    """`user_plugin.start()` spawns `claude plugin install/update`: nothing of
    Claude's starts with Claude switched off (D17)."""
    import asyncio

    from fused_render import user_plugin
    from fused_render.server import app as app_mod

    captured = {}
    real = app_mod._lifespan

    def spy(startup, shutdown):
        captured["startup"] = startup
        return real(startup, shutdown)

    monkeypatch.setattr(app_mod, "_lifespan", spy)
    app_mod.create_app(start_dir=str(tmp_path))
    handler, = [h for h in captured["startup"]
                if h.__name__ == "_startup_sync_user_plugin"]
    started = []
    monkeypatch.setattr(user_plugin, "start", lambda: started.append(1))
    _claude_off()
    asyncio.run(handler())
    assert started == []
    prefs_mod.write_pref("claude_enabled", True)
    asyncio.run(handler())
    assert started == [1]


def test_schedule_create_pins_a_bots_task_to_claude(tmp_path):
    _copilot_default()
    target = tmp_path / "p"
    target.mkdir()
    # Omitted: Claude, never the Preferences default.
    assert schedule.create(str(target), "go", _past(),
                           bot="bot-1")["backend"] == "claude"
    assert schedule.create(str(target), "go", _past(), bot="bot-1",
                           backend="claude")["backend"] == "claude"
    with pytest.raises(schedule.BackendRefused) as info:
        schedule.create(str(target), "go", _past(), bot="bot-1",
                        backend="copilot")
    assert info.value.body == {"error": "bots run on Claude",
                               "reason": "unsupported", "backend": "copilot"}


def _claude_off():
    prefs_mod.write_pref("copilot_enabled", True)
    prefs_mod.write_pref("claude_enabled", False)


def test_a_bots_claude_task_passes_the_claude_switch(tmp_path, client, monkeypatch):
    """D17 / owner Q7: Bots are not gated by the Claude switch — they keep
    using Claude. At create AND at send; a non-bot Claude task still meets
    the gate."""
    from fused_render.bots import store as bot_store
    _claude_off()
    monkeypatch.setattr(bot_store, "list_ids", lambda: ["bot-1"])
    target = tmp_path / "p"
    target.mkdir()
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "due": due,
                          "bot": "bot-1"})
    assert r.status_code == 200, r.text
    stored = next(e for e in schedule.list_entries()
                  if e["id"] == r.json()["entry_id"])
    assert stored["backend"] == "claude"
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "due": due,
                          "backend": "claude"})
    assert r.status_code == 400
    assert r.json() == registry.disabled_error("claude")

    calls = []
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **kw: calls.append(kw) or {"run_id": "r"})
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    # As `/api/tasks/create` stores a bot's build: pinned to Claude.
    entry = schedule.create(str(target), "go", _past(), bot="bot-1",
                            backend="claude")
    schedule.tick()
    assert calls and calls[0]["backend"] == "claude"
    # …and the bot rides to `_start`, which lets it through the same switch.
    assert calls[0]["bot"] == "bot-1"
    stored = next(e for e in schedule.list_entries() if e["id"] == entry["id"])
    assert stored["state"] != schedule.ERROR, stored
    # An explicit "claude" from a bot passes the door with Claude off too.
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "go", "target": str(target), "due": due,
                          "bot": "bot-1", "backend": "claude"})
    assert r.status_code == 200, r.text


def test_a_non_bot_claude_entry_still_fails_at_send_with_claude_off(
        tmp_path, monkeypatch):
    target = tmp_path / "p"
    target.mkdir()
    entry = schedule.create(str(target), "go", _past())
    assert entry["backend"] == "claude"
    _claude_off()
    spawned = []
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: spawned.append(1) or {"run_id": "r"})
    schedule.tick()
    stored = next(e for e in schedule.list_entries() if e["id"] == entry["id"])
    assert stored["state"] == schedule.ERROR
    assert stored["error"] == "Claude disabled"
    assert spawned == []


def test_erase_finds_the_cli_store_under_copilot_home(
        tmp_path, claude_projects, copilot_projects, client, monkeypatch):
    target = tmp_path / "p"
    target.mkdir()
    path = _write_transcript(copilot_projects, "sess-copilot", str(target), [_user("two")])
    copilot_home = tmp_path / "copilot-home"
    (copilot_home / "session-state" / "sess-copilot").mkdir(parents=True)
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    assert tasks_mod._copilot_session_state_dir() == str(copilot_home / "session-state")
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": "sess-copilot"})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert not (copilot_home / "session-state" / "sess-copilot").exists()


def test_a_session_bound_message_cannot_name_another_agent(
        tmp_path, copilot_projects, client):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    _write_transcript(copilot_projects, "sess-cop", str(target), [_user("hi")])
    with pytest.raises(schedule.BackendRefused) as info:
        schedule.create(str(target), "go", _past(), session_id="sess-cop",
                        backend="claude")
    assert info.value.body == {"error": "this session belongs to Copilot",
                               "reason": "backend_mismatch", "backend": "copilot"}
    # Naming the owner is fine, and so is naming nothing.
    assert schedule.create(str(target), "go", _past(), session_id="sess-cop",
                           backend="copilot")["backend"] == "copilot"
    # The schedule door answers with the whole body.
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/api/schedule", headers=FUSED,
                    json={"target": str(target), "message": "go", "due": due,
                          "session_id": "sess-cop", "backend": "claude"})
    assert r.status_code == 400
    assert r.json()["reason"] == "backend_mismatch"
    assert r.json()["backend"] == "copilot"


def test_a_session_nothing_has_recorded_yet_is_nobodys_to_refuse(
        tmp_path, copilot_projects):
    """No record and no transcript is no evidence: a Copilot run seconds old
    must not be refused as "belongs to Claude" on send/resend/resay. The
    Claude fallback only fills a blank."""
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    assert schedule.create(str(target), "go", _past(), session_id="sess-new",
                           backend="copilot")["backend"] == "copilot"
    assert schedule.create(str(target), "go", _past(),
                           session_id="sess-new")["backend"] == "claude"
    # …and a resend of a Copilot entry whose session has no evidence yet.
    entry = schedule.create(str(target), "go", _past(), backend="copilot")
    schedule._update(entry["id"], state=schedule.SENT, turn="failed",
                     error="the turn died", claude_session_id="sess-young")
    result = schedule.resend(entry["id"])
    assert result["ok"] is True, result
    assert result["entry"]["backend"] == "copilot"


def test_the_session_owner_tries_the_targets_bucket_before_any_glob(
        tmp_path, copilot_projects, monkeypatch):
    """The hot paths into `create` hand the target through, so a pre-record
    session is answered by a stat in its own bucket, never a glob over every
    bucket; the glob stays the fallback for a session filed elsewhere."""
    target = tmp_path / "p"
    target.mkdir()
    page = target / "index.html"
    page.write_text("<html></html>")
    _write_transcript(copilot_projects, "sess-cop", str(target), [_user("hi")])
    real_glob = roots._shared.glob.glob
    globbed = []
    monkeypatch.setattr(roots._shared.glob, "glob",
                        lambda pattern: globbed.append(pattern) or real_glob(pattern))
    # A folder target and a file target name the same bucket.
    assert schedule.session_owner("sess-cop", str(target)) == "copilot"
    assert schedule.session_owner("sess-cop", str(page)) == "copilot"
    assert globbed == []
    # No target, or the wrong one: the glob still finds it.
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert schedule.session_owner("sess-cop", str(other)) == "copilot"
    assert schedule.session_owner("sess-cop") == "copilot"
    assert globbed
    # `create` passes its target through.
    globbed.clear()
    _enable_copilot()
    assert schedule.create(str(target), "go", _past(),
                           session_id="sess-cop")["backend"] == "copilot"
    assert globbed == []


def test_a_follower_cannot_name_another_agent_than_its_leader(tmp_path):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    leader = schedule.create(str(target), "first", _past(), backend="copilot")
    with pytest.raises(schedule.BackendRefused) as info:
        schedule.create(str(target), "second", _past(), follow_of=leader["id"],
                        backend="claude")
    assert info.value.body == {"error": "this conversation belongs to Copilot",
                               "reason": "backend_mismatch", "backend": "copilot"}
    assert schedule.create(str(target), "second", _past(), follow_of=leader["id"],
                           backend="copilot")["backend"] == "copilot"


def test_plan_mode_is_refused_for_an_agent_without_one(tmp_path):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    with pytest.raises(schedule.BackendRefused) as info:
        schedule.create(str(target), "go", _past(), backend="copilot",
                        permission_mode="plan")
    assert info.value.body == {"error": "plan mode is not available for Copilot",
                               "reason": "unsupported", "backend": "copilot"}
    assert schedule.create(str(target), "go", _past(), backend="claude",
                           permission_mode="plan")["permission_mode"] == "plan"


def test_a_model_from_the_other_agents_list_is_refused_at_create(tmp_path, client):
    _enable_copilot()
    target = tmp_path / "p"
    target.mkdir()
    with pytest.raises(schedule.BackendRefused) as info:
        schedule.create(str(target), "go", _past(), backend="copilot", model="opus")
    assert info.value.body == {"error": "'opus' is a Claude model, not one Copilot runs",
                               "reason": "unsupported", "backend": "copilot"}
    with pytest.raises(schedule.BackendRefused):
        schedule.create(str(target), "go", _past(), backend="claude", model="gpt-5.4")
    # The page door answers the same body before anything is stored.
    r = client.post("/api/tasks/create", headers=FUSED,
                    json={"prompt": "page go", "target": str(target),
                          "backend": "copilot", "model": "sonnet"})
    assert r.status_code == 400, r.text
    assert r.json()["reason"] == "unsupported"
    assert not [e for e in schedule.list_entries() if e.get("message") == "page go"]
    # Its own models, and ids on nobody's list, pass through as before.
    assert schedule.create(str(target), "go", _past(), backend="copilot",
                           model="gpt-5.4")["model"] == "gpt-5.4"
    assert schedule.create(str(target), "go", _past(), backend="copilot",
                           model="gpt-6-preview")["model"] == "gpt-6-preview"
    assert schedule.create(str(target), "go", _past(), backend="claude",
                           model="claude-opus-5-5")["model"] == "claude-opus-5-5"


def test_the_settings_door_refuses_a_model_from_the_other_agents_list(client):
    tasks_store.record_settings("sess-m", backend="copilot")
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-m", "model": "opus"})
    assert r.status_code == 400, r.text
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-m", "model": "gpt-5.4"})
    assert r.status_code == 200, r.text
    # A session nothing says is anyone's reads as Claude's.
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-blank", "model": "auto"})
    assert r.status_code == 400, r.text


def test_the_schedule_doors_disabled_refusal_is_the_whole_body(tmp_path, client):
    target = tmp_path / "p"
    target.mkdir()
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/api/schedule", headers=FUSED,
                    json={"target": str(target), "message": "go", "due": due,
                          "backend": "copilot"})
    assert r.status_code == 400
    assert r.json() == registry.disabled_error("copilot")


def test_the_settings_door_reads_the_owner_off_the_transcript_root(
        tmp_path, copilot_projects, client):
    target = tmp_path / "p"
    target.mkdir()
    # No record, but a transcript under the Copilot root: that IS the owner.
    _write_transcript(copilot_projects, "sess-cop", str(target), [_user("hi")])
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-cop", "backend": "claude"})
    assert r.status_code == 400, r.text
    r = client.post("/api/tasks/settings", headers=FUSED,
                    json={"session_id": "sess-cop", "backend": "copilot"})
    assert r.status_code == 200, r.text


def test_a_pid_in_both_registries_names_the_newer_file(tmp_path, home, monkeypatch):
    claude_sessions = tmp_path / "claude-sessions"
    claude_sessions.mkdir()
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(claude_sessions))
    copilot_sessions = home / "copilot" / "sessions"
    copilot_sessions.mkdir(parents=True)
    monkeypatch.setattr(tasks_watch, "_pid_alive", lambda pid: True)
    old = claude_sessions / "33.json"
    new = copilot_sessions / "33.json"
    old.write_text(json.dumps({"sessionId": "s-dead", "pid": 33}))
    new.write_text(json.dumps({"sessionId": "s-live", "pid": 33}))
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert tasks_watch.session_for_pid(33) == "s-live"
    # …and the other way round, so the order of the dirs is not the answer.
    os.utime(old, (3000, 3000))
    assert tasks_watch.session_for_pid(33) == "s-dead"


def test_erase_through_a_symlinked_copilot_bucket_takes_the_cli_store_too(
        tmp_path, claude_projects, copilot_projects, client, monkeypatch):
    target = tmp_path / "p"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    path = _write_transcript(elsewhere, "sess-copilot", str(target), [_user("two")])
    (copilot_projects / path.parent.name).symlink_to(path.parent)
    state = tmp_path / "copilot-session-state"
    (state / "sess-copilot").mkdir(parents=True)
    monkeypatch.setattr(tasks_mod, "_copilot_session_state_dir", lambda: str(state))
    assert client.get("/api/tasks").json()["tasks"][0]["key"] == "sess-copilot"
    r = client.post("/api/tasks/erase", headers=FUSED, json={"key": "sess-copilot"})
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert not (state / "sess-copilot").exists()


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


# ------------------------------------------------------------------ invariant 16


#: Code (not prose) that resolves Codex CLI's own home: a `"~/.codex"` or a
#: `"CODEX_HOME"` string literal. Docstrings and comments may SAY either —
#: in backticks — and are not matched.
_CODEX_LITERAL = re.compile(r"""["'](?:~/\.codex|CODEX_HOME)\b""")

#: docs/CODEX_BACKEND.md invariant 16: the only modules that may name Codex's
#: home — the roots file, the health/login/install trio that probe and sign
#: in, erase (D15) and the recap's one-shot (D12).
_CODEX_HOME_ALLOWED = {
    "templates/shared/agent_roots.py", "codex_health.py", "codex_login.py",
    "codex_install.py", "server/routers/tasks.py",
    "server/routers/claude_sessions.py",
}


def test_only_the_allowed_modules_name_codexs_own_home():
    """Invariant 16 (invariant 7's twin for Codex): Codex's `CODEX_HOME` /
    `~/.codex` is named in code only where the contract allows it — every
    other module reaches it through one of those. Walks the WHOLE package,
    the hosts and agent.py included, because a new reader is exactly what
    this exists to catch."""
    import fused_render
    base = os.path.dirname(fused_render.__file__)
    offenders = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if rel in _CODEX_HOME_ALLOWED:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]
                    if _CODEX_LITERAL.search(code):
                        offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == []


def test_the_codex_literal_pattern_catches_code_and_spares_prose():
    assert _CODEX_LITERAL.search('os.environ.get("CODEX_HOME")')
    assert _CODEX_LITERAL.search("expanduser('~/.codex')")
    assert _CODEX_LITERAL.search('os.path.expanduser("~/.codex/sessions")')
    assert not _CODEX_LITERAL.search("leaves `CODEX_HOME` exactly as found")
    assert not _CODEX_LITERAL.search("under `~/.codex`")
    assert not _CODEX_LITERAL.search('"CODEX_HOME_X"')


#: Code (not prose) that resolves Gemini CLI's own home: a `"~/.gemini"` /
#: `".gemini"` or a `"GEMINI_CLI_HOME"` string literal.
_GEMINI_LITERAL = re.compile(r"""["'](?:~/\.gemini|\.gemini|GEMINI_CLI_HOME)\b""")

#: docs/GEMINI_BACKEND.md invariant 16: the only modules that may name
#: Gemini's home — the health module (which the others reach it through) and
#: the roots file's docstring-free code (which names none; listed for the
#: day it does).
_GEMINI_HOME_ALLOWED = {
    "templates/shared/agent_roots.py", "gemini_health.py", "gemini_login.py",
    "gemini_install.py",
}


def test_only_the_allowed_modules_name_geminis_own_home():
    """Invariant 16's Gemini twin: `GEMINI_CLI_HOME` / `.gemini` is named in
    code only in `gemini_health` (erase and the recap reach it through
    `config_dir`); every other module is caught here."""
    import fused_render
    base = os.path.dirname(fused_render.__file__)
    offenders = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if rel in _GEMINI_HOME_ALLOWED:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]
                    if _GEMINI_LITERAL.search(code):
                        offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == []


def test_the_gemini_literal_pattern_catches_code_and_spares_prose():
    assert _GEMINI_LITERAL.search('os.environ.get("GEMINI_CLI_HOME")')
    assert _GEMINI_LITERAL.search('os.path.join(home, ".gemini")')
    assert _GEMINI_LITERAL.search("expanduser('~/.gemini/tmp')")
    assert not _GEMINI_LITERAL.search("leaves `GEMINI_CLI_HOME` exactly as found")
    assert not _GEMINI_LITERAL.search("under `~/.gemini`")
    assert not _GEMINI_LITERAL.search('"GEMINI_CLI_HOMES"')


def test_spawn_helper_forwards_isolate_mcp_only_when_true(monkeypatch):
    """A fake `_start` without the kwarg must keep working, so the default
    call shape is unchanged and only an asked-for lean run says so."""
    seen = []

    class _Agent:
        @staticmethod
        def _start(*a, **kw):
            seen.append(kw)
            return {"run_id": "r"}

    monkeypatch.setattr(claude_spawn, "load_agent", lambda: _Agent)
    claude_spawn.spawn_helper("/t", "p", "auto")
    claude_spawn.spawn_helper("/t", "p", "auto", isolate_mcp=True)
    assert "isolate_mcp" not in seen[0]
    assert seen[1]["isolate_mcp"] is True


def _account_models():
    """What `copilot_health.normalize_models` stores for an account whose
    plan carries a GPT id Codex also lists, a Claude id that takes no
    effort, and a GPT id with its own effort list and default."""
    return [
        {"id": "gpt-6-luna", "label": "GPT-6 Luna", "reasoning": True,
         "efforts": ["low", "medium", "high", "xhigh"], "default_effort": "high",
         "category": "powerful", "vendor": "Azure OpenAI", "multiplier": 1},
        {"id": "claude-sonnet-4.5", "label": "Claude Sonnet 4.5", "reasoning": False,
         "efforts": [], "default_effort": "", "category": "versatile",
         "vendor": "Anthropic", "multiplier": 1},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini", "reasoning": True,
         "efforts": ["low", "medium", "high"], "default_effort": "",
         "category": "lightweight", "vendor": None, "multiplier": 0},
    ]


def test_the_catalog_serves_the_accounts_models_when_the_snapshot_has_them(
        client, monkeypatch):
    """Copilot's model list is per account: once the health snapshot carries
    the CLI's `models.list`, the catalog row serves THAT, `auto` first, each
    model with the efforts it takes — and the static shortlist otherwise."""
    from fused_render import claude_health, codex_health, copilot_health
    monkeypatch.setattr(claude_health, "cached", lambda: None)
    monkeypatch.setattr(codex_health, "cached", lambda: None)
    snap = {"found": True, "signed_in": True, "fingerprint": "x",
            "models": _account_models()}
    monkeypatch.setattr(copilot_health, "cached", lambda: snap)
    _enable_copilot()
    row = next(b for b in client.get("/api/agent-backends").json()["all"]
               if b["id"] == "copilot")
    assert [m["id"] for m in row["models"]] == [
        "auto", "gpt-6-luna", "claude-sonnet-4.5", "gpt-5.4-mini"]
    auto, luna, sonnet, mini = row["models"]
    assert auto["reasoning"] is False and auto["efforts"] == []
    assert luna == {"id": "gpt-6-luna", "label": "GPT-6 Luna", "reasoning": True,
                    "efforts": ["low", "medium", "high", "xhigh"],
                    "default_effort": "high", "category": "powerful",
                    "vendor": "Azure OpenAI", "multiplier": 1.0}
    assert sonnet["reasoning"] is False and sonnet["efforts"] == []
    assert mini["efforts"] == ["low", "medium", "high"] and mini["multiplier"] == 0.0
    # The row's efforts list is still the agent's whole vocabulary; the
    # per-model lists are the narrowing. The health row does not repeat the
    # models it was read from.
    assert row["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert "models" not in row["health"] and row["health"]["signed_in"] is True
    # Static when nothing is cached, and in the static catalog.
    monkeypatch.setattr(copilot_health, "cached", lambda: None)
    monkeypatch.setattr(copilot_health, "cached_models", lambda: None)
    row = next(b for b in client.get("/api/agent-backends").json()["all"]
               if b["id"] == "copilot")
    assert [m["id"] for m in row["models"]] == [m.id for m in registry.get("copilot").models]
    static = registry.catalog(with_health=False)["all"][1]
    assert [m["id"] for m in static["models"]] == [m.id for m in registry.get("copilot").models]


def test_the_catalog_falls_back_to_the_raw_cache_for_the_model_list(monkeypatch):
    """`cached()` says None after a PATH change (its fingerprint); the model
    list is still the account's, so the row reads it off the raw cache."""
    from fused_render import claude_health, codex_health, copilot_health
    monkeypatch.setattr(claude_health, "cached", lambda: None)
    monkeypatch.setattr(codex_health, "cached", lambda: None)
    monkeypatch.setattr(copilot_health, "cached", lambda: None)
    monkeypatch.setattr(copilot_health, "cached_models", _account_models)
    row = registry.catalog()["all"][1]
    assert row["health"] is None
    assert [m["id"] for m in row["models"]] == [
        "auto", "gpt-6-luna", "claude-sonnet-4.5", "gpt-5.4-mini"]


def test_a_discovered_model_passes_the_foreign_model_gate(monkeypatch):
    """A GPT id Codex's static list also names is NOT foreign to Copilot once
    the account's own list carries it — the gate would otherwise 400 a model
    the CLI itself offered. An id on nobody's list still passes; an id only
    Codex lists is still refused."""
    from fused_render import copilot_health
    monkeypatch.setattr(copilot_health, "cached_models", lambda: None)
    assert registry.foreign_model_error("copilot", "gpt-6-luna")["reason"] == "unsupported"
    monkeypatch.setattr(copilot_health, "cached_models", _account_models)
    assert registry.foreign_model_error("copilot", "gpt-6-luna") is None
    assert registry.foreign_model_error("copilot", "gpt-5.4-mini") is None
    assert registry.foreign_model_error("copilot", "gpt-9") is None
    assert registry.foreign_model_error("copilot", "gpt-6.1-sol")["reason"] == "unsupported"
    # The static shortlist still passes its own ids (an old record).
    assert registry.foreign_model_error("copilot", "gemini-3-pro") is None
    # Nothing leaks the other way: a Codex task cannot run Copilot's models.
    assert registry.foreign_model_error("codex", "claude-sonnet-4.5")["reason"] == "unsupported"


def test_discovered_models_skips_malformed_rows_and_keeps_auto_first():
    from fused_render.agent_backends import copilot
    rows = [{"id": "gpt-5.4", "label": "GPT-5.4", "efforts": ["high", "bogus"]},
            {"id": "auto", "label": "the server never lists it, but if it did"},
            {"id": "", "label": "no id"}, "garbage",
            {"id": "x-1", "label": "no efforts key", "reasoning": False}]
    models = copilot.discovered_models({"models": rows})
    assert [m.id for m in models] == ["auto", "gpt-5.4", "x-1"]
    assert models[0] is copilot.AUTO
    assert models[1].efforts == ("high",) and models[1].reasoning is True
    assert models[2].reasoning is False and models[2].efforts == ()
    assert copilot.discovered_models({"models": []}) is None
    assert copilot.discovered_models({"models": ["garbage"]}) is None
