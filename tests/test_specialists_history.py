"""The home's git history: app commits, the log, and the breakout guard that puts protected files back (Q, F)."""
from _specialists_conftest import *  # noqa: F401,F403
import os


from fused_render.specialists import chat, harness, history

pytestmark = needs_git


def _write(spec, rel, text):
    p = os.path.join(spec.home, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def _read(spec, rel):
    with open(os.path.join(spec.home, rel)) as f:
        return f.read()


def test_create_commits_and_log(app_home):
    s = make_specialist()
    log = history.log(s.id)
    assert len(log) == 1 and log[0]["message"].startswith("create") and "CLAUDE.md" in log[0]["files"]
    assert history.commit(s.id, "nothing") is None  # nothing changed
    _write(s, "memory.md", "# Memory\n- [m-1a2b] A fact. {basis=user-said}\n")
    _write(s, "inbox/n-1.json", "{}")  # ignored: inbox is untracked
    sha = history.commit(s.id, "main chat turn")
    assert sha and history.diff_files(s.id, sha) == ["memory.md"]
    assert [c["message"] for c in history.log(s.id, "memory.md")] == ["main chat turn", log[0]["message"]]


def test_memory_version_follows_content(app_home):
    s = make_specialist()
    v1 = history.memory_version(s.id)
    assert len(v1) == 8
    _write(s, "memory.md", "# Memory\n- x\n")
    v2 = history.memory_version(s.id)
    assert v2 != v1
    history.commit(s.id, "c")
    assert history.memory_version(s.id) == v2  # a commit does not change the version, the text does


def test_main_turn_commits(app_home):
    s = make_specialist()
    _write(s, "memory.md", "# Memory\n- remembered\n")
    history.after_turn(s.id, "main", "sess-abcdef12")
    assert history.log(s.id)[0]["message"] == "main chat turn sess-abc"


def test_breakout_guard_restores_protected_paths(app_home, monkeypatch):
    s = make_specialist()
    monkeypatch.setattr(history, "_main_turn_live", lambda spec_id, sid: False)
    before_mem, before_md = _read(s, "memory.md"), _read(s, "CLAUDE.md")
    _write(s, "memory.md", "# Memory\n- rewritten by a breakout\n")
    _write(s, "CLAUDE.md", "hijacked")
    _write(s, "knowledge/new.md", "sneaky")
    _write(s, ".claude/skills/evil/SKILL.md", "---\nname: evil\n---\n")
    _write(s, "scratch.txt", "allowed scratch")  # not protected: left alone, never committed by a breakout
    head = history.log(s.id)[0]["sha"]
    history.after_turn(s.id, "breakout", "sess-b")
    assert _read(s, "memory.md") == before_mem and _read(s, "CLAUDE.md") == before_md
    assert not os.path.exists(os.path.join(s.home, "knowledge", "new.md"))
    assert not os.path.exists(os.path.join(s.home, ".claude", "skills", "evil"))  # no empty folder left behind
    assert os.path.isdir(os.path.join(s.home, ".claude", "skills")) and os.path.isdir(os.path.join(s.home, "knowledge"))
    assert history.log(s.id)[0]["sha"] == head  # a breakout turn commits nothing
    assert _read(s, "scratch.txt") == "allowed scratch"
    guard = [r for r in harness.rows(s.id) if r["kind"] == "guard"]
    assert len(guard) == 1 and "memory.md" in guard[0]["answer"]


def test_breakout_guard_skips_while_main_chat_is_live(app_home, monkeypatch):
    """QA defect 1: with a main turn live, the breakout's Stop neither undoes nor commits anything (it cannot tell
    whose change is whose); the main turn's own Stop commits what is there, as main."""
    s = make_specialist()
    chat.record_start(s.id, "main-1")
    monkeypatch.setattr(chat, "session_live", lambda spec_id, sid: sid == "main-1")
    head = history.log(s.id)[0]["sha"]
    _write(s, "memory.md", "# Memory\n- written by main chat\n")
    history.after_turn(s.id, "breakout", "sess-b")
    assert "written by main chat" in _read(s, "memory.md")
    assert history.log(s.id)[0]["sha"] == head  # nothing committed on the breakout's behalf
    (row,) = [r for r in harness.rows(s.id) if r["kind"] == "guard"]
    assert row["status"] == "skipped" and "main chat was working" in row["answer"]
    monkeypatch.setattr(chat, "session_live", lambda spec_id, sid: False)
    history.after_turn(s.id, "main", "main-1")
    assert history.log(s.id)[0]["message"] == "main chat turn main-1"


def test_guard_stands_down_when_a_main_turn_ran_during_the_breakout_turn(app_home, monkeypatch):
    """A main turn that started and ended inside the breakout's turn: at the breakout's Stop nothing is live, yet
    the guard must not undo (or commit) anything either."""
    s = make_specialist()
    monkeypatch.setattr(chat, "session_live", lambda spec_id, sid: False)
    head = history.log(s.id)[0]["sha"]
    history.before_turn(s.id, "breakout", "sess-b")
    history.before_turn(s.id, "main", "main-1")
    assert history.main_turn_open(s.id)
    _write(s, "memory.md", "# Memory\n- main, uncommitted for a moment\n")
    history.after_turn(s.id, "breakout", "sess-b")  # main still open: hands off
    assert "main, uncommitted" in _read(s, "memory.md") and history.log(s.id)[0]["sha"] == head
    history.before_turn(s.id, "breakout", "sess-b")  # next breakout turn, main still live: no "pending" commit
    assert history.log(s.id)[0]["sha"] == head
    history.after_turn(s.id, "main", "main-1")
    assert not history.main_turn_open(s.id)
    assert history.log(s.id)[0]["message"] == "main chat turn main-1"
    _write(s, "memory.md", "# Memory\n- stray, by absolute path\n")
    history.after_turn(s.id, "breakout", "sess-b")  # main ended after this breakout turn began: still hands off
    assert "stray" in _read(s, "memory.md")
    history.before_turn(s.id, "breakout", "sess-b")  # a fresh turn with nothing else live: the stray is pending
    _write(s, "people.md", "rewritten\n")
    history.after_turn(s.id, "breakout", "sess-b")
    assert _read(s, "people.md") != "rewritten\n"


def test_settings_and_repo_config_are_the_apps(app_home):
    """R4: whatever a session wrote to the home's project settings never reaches history, and repository config a
    session planted (a clean filter runs a command inside the app's own `git add`) is dropped before a commit."""
    import json

    s = make_specialist()
    _write(s, ".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "touch /tmp/x"}]}]}}))
    _write(s, ".claude/settings.local.json", "{}")
    marker = os.path.join(s.home, "filter-ran")
    with open(os.path.join(s.home, ".git", "config"), "a") as f:
        f.write(f'[filter "evil"]\n\tclean = sh -c \'touch {marker}; cat\'\n')
    os.makedirs(os.path.join(s.home, ".git", "info"), exist_ok=True)
    with open(os.path.join(s.home, ".git", "info", "attributes"), "w") as f:
        f.write("* filter=evil\n")
    _write(s, "memory.md", "# Memory\n- main\n")
    history.after_turn(s.id, "main", "sess-m")
    assert not os.path.exists(marker)
    assert json.loads(_read(s, ".claude/settings.json")) == {"autoMemoryEnabled": False}
    assert not os.path.exists(os.path.join(s.home, ".claude", "settings.local.json"))
    assert history.log(s.id)[0]["files"] == ["memory.md"]
    assert "evil" not in _read(s, ".git/config")


def test_a_settings_save_commits_only_its_own_paths(app_home):
    s = make_specialist()
    _write(s, "memory.md", "# Memory\n- a stray change\n")
    _write(s, "people.md", "| Area | Owner |\n")
    sha = history.commit(s.id, "settings: people", ["people.md"])
    assert sha and history.diff_files(s.id, sha) == ["people.md"]
    assert history.commit(s.id, "nothing", ["knowledge/none.md"]) is None


def test_no_git_degrades(app_home, monkeypatch):
    s = make_specialist()
    monkeypatch.setattr(history, "available", lambda: False)
    assert history.commit(s.id, "x") is None and history.log(s.id) == [] and history.guard(s.id) == []
    history.after_turn(s.id, "breakout", "s")  # never raises


def test_guard_stands_down_while_the_nightly_job_runs(app_home, monkeypatch):
    s = make_specialist()
    monkeypatch.setattr(history, "_main_turn_live", lambda spec_id, sid: False)
    monkeypatch.setattr(history, "_nightly_running", lambda spec_id: True)
    head = history.log(s.id)[0]["sha"]
    _write(s, "memory.md", "# Memory\n- written by the nightly job\n")
    history.after_turn(s.id, "breakout", "sess-b")
    assert "nightly job" in _read(s, "memory.md")  # not undone
    assert history.log(s.id)[0]["sha"] == head  # and not committed as the breakout's
    (row,) = [r for r in harness.rows(s.id) if r["kind"] == "guard"]
    assert row["status"] == "skipped" and "nightly update was running" in row["answer"]
    history.before_turn(s.id, "breakout", "sess-b")
    assert history.log(s.id)[0]["sha"] == head


def test_before_turn_commits_pending_changes_so_the_guard_undoes_only_the_turn(app_home, monkeypatch):
    s = make_specialist()
    monkeypatch.setattr(history, "_main_turn_live", lambda spec_id, sid: False)
    monkeypatch.setattr(history, "_nightly_running", lambda spec_id: False)
    _write(s, "memory.md", "# Memory\n- an uncommitted main-chat edit\n")
    history.before_turn(s.id, "breakout", "sess-b")
    assert history.log(s.id)[0]["message"] == "pending changes before a breakout turn"
    _write(s, "memory.md", "# Memory\n- rewritten during the breakout turn\n")
    history.after_turn(s.id, "breakout", "sess-b")
    assert _read(s, "memory.md") == "# Memory\n- an uncommitted main-chat edit\n"
    history.before_turn(s.id, "main", "sess-m")  # a main turn commits nothing up front
    _write(s, "memory.md", "# Memory\n- main\n")
    history.before_turn(s.id, "main", "sess-m")
    assert history.log(s.id)[0]["message"] == "pending changes before a breakout turn"
    history.after_turn(s.id, "main", "sess-m")


def test_nightly_running_reads_stream_3(app_home, monkeypatch):
    from fused_render.specialists import nightly

    s = make_specialist()
    monkeypatch.setattr(nightly, "running", lambda spec_id: spec_id == s.id)
    assert history._nightly_running(s.id) and not history._nightly_running("other")
