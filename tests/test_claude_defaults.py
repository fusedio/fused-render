"""The `defaults` action for a chat with NO SESSION YET — a brand-new chat,
from the file explorer composer or from the New task modal.

It answers with the GLOBAL Claude preference and nothing else: `model` and
`effortLevel` in ~/.claude/settings.json, the pair the app's Claude settings
page writes (Akshil, 2026-09-21: "only factor in global model/effort for all
new chats … model and effort picked from global config").

It used to walk a folder ladder first — the newest five transcripts in the
folder's project store, then that folder's .claude/settings.local.json, then
its .claude/settings.json — so a new chat inherited whatever the last chat in
that folder happened to run with, or whatever Claude Code's own per-project
config said. These tests pin that ladder GONE: a hardcoded "default" in the
selector tells the user nothing, and neither does a neighbour's model.

A chat that names a session is a different question and is answered from that
conversation alone (tests/test_claude_sessions_merged.py).

D1310: the page half of this file (source pins and node probes over the
retired iframe chat page, templates/claude/template.html) went with that
page; the native chat under frontend/src/apps/claude owns it now. What
stays is the backend half.
"""
import importlib.util
import json
import os
import re

import pytest


def _load_agent():
    path = os.path.join("fused_render", "claude_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _agent_in(tmp_path, monkeypatch):
    agent = _load_agent()
    claude_dir = tmp_path / "claude"
    projects = claude_dir / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(agent, "CLAUDE_DIR", str(claude_dir))
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    return agent


def _target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "index.html"
    f.write_text("<html></html>")
    return str(f), str(d)


def _write_transcript(agent, workdir, rows, name="s1.jsonl"):
    proj = os.path.join(agent.PROJECTS, agent._munge(workdir))
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, name)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _write_global(agent, **keys):
    os.makedirs(agent.CLAUDE_DIR, exist_ok=True)
    with open(os.path.join(agent.CLAUDE_DIR, "settings.json"), "w") as f:
        json.dump(keys, f)


def test_the_global_settings_file_is_what_a_new_chat_opens_on(tmp_path,
                                                              monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    file, _ = _target(tmp_path)
    _write_global(agent, model="opusplan", effortLevel="low")
    out = agent.main(action="defaults", file=file)
    # The alias is collapsed to the name the picker actually offers, and
    # `recorded` is the app's OWN per-session record — there is no conversation
    # here to have one.
    assert out == {"model": "opus", "effort": "low", "source": "settings",
                   "backend": "claude", "recorded": {"model": "", "effort": ""}}


def test_transcripts_in_the_folder_no_longer_speak_for_a_new_chat(tmp_path,
                                                                  monkeypatch):
    """THE LADDER'S FIRST RUNG, GONE. One experiment on Fable in this folder
    used to pin every later chat opened there to Fable."""
    agent = _agent_in(tmp_path, monkeypatch)
    file, workdir = _target(tmp_path)
    _write_transcript(agent, workdir, [
        {"effort": "xhigh", "message": {"model": "claude-fable-5"}},
    ])
    _write_global(agent, model="sonnet", effortLevel="low")
    out = agent.main(action="defaults", file=file)
    assert (out["model"], out["effort"]) == ("sonnet", "low")
    assert out["source"] == "settings"


def test_a_folder_settings_file_is_ignored_even_when_the_global_one_answers(
        tmp_path, monkeypatch):
    """THE LADDER'S OTHER RUNGS, GONE. `.claude/settings.json` (and its
    `.local` sibling) beside the project is Claude Code's own per-project
    config, not a statement about what the reader wants THIS new chat to run."""
    agent = _agent_in(tmp_path, monkeypatch)
    file, workdir = _target(tmp_path)
    proj_cfg = os.path.join(workdir, ".claude")
    os.makedirs(proj_cfg)
    with open(os.path.join(proj_cfg, "settings.json"), "w") as f:
        json.dump({"model": "haiku", "effortLevel": "max"}, f)
    with open(os.path.join(proj_cfg, "settings.local.json"), "w") as f:
        json.dump({"model": "opus", "effortLevel": "high"}, f)
    _write_global(agent, model="fable", effortLevel="low")
    out = agent.main(action="defaults", file=file)
    assert (out["model"], out["effort"]) == ("fable", "low")


def test_a_field_the_global_file_omits_stays_empty(tmp_path, monkeypatch):
    """PER FIELD, and nothing backfills the other half — not the folder's
    settings, not a neighbour chat. "" leaves the page's own constant
    speaking."""
    agent = _agent_in(tmp_path, monkeypatch)
    file, workdir = _target(tmp_path)
    proj_cfg = os.path.join(workdir, ".claude")
    os.makedirs(proj_cfg)
    with open(os.path.join(proj_cfg, "settings.json"), "w") as f:
        json.dump({"effortLevel": "max"}, f)
    _write_transcript(agent, workdir, [
        {"effort": "xhigh", "message": {"model": "claude-fable-5"}},
    ])
    _write_global(agent, model="sonnet")
    out = agent.main(action="defaults", file=file)
    assert (out["model"], out["effort"]) == ("sonnet", "")


def test_one_reader_answers_the_new_task_card_too(tmp_path, monkeypatch):
    """`GET /api/claude-sessions/defaults` reads the same file through the same
    helper, so the card and the chat it books cannot disagree."""
    agent = _agent_in(tmp_path, monkeypatch)
    _write_global(agent, model="claude-fable-5-1", effortLevel="high")
    assert agent._global_defaults() == ("fable", "high")


def test_the_new_task_endpoint_answers_off_the_same_helper(tmp_path,
                                                           monkeypatch):
    """`GET /api/claude-sessions/defaults` — what the New task modal asks — is
    the same read, not a second copy of it. Two readers of one file drift, and
    a card that promises a model the chat it books then opens on something else
    is exactly that drift showing."""
    from fused_render.server.routers import claude_sessions, tasks

    agent = _agent_in(tmp_path, monkeypatch)
    _write_global(agent, model="claude-fable-5-1", effortLevel="xhigh")
    monkeypatch.setattr(tasks, "_agent_module", lambda: agent)
    assert claude_sessions.claude_defaults() == {"model": "fable",
                                                 "effort": "xhigh"}


def test_a_broken_or_absent_global_file_is_not_a_crash(tmp_path, monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    assert agent._global_defaults() == ("", "")
    os.makedirs(agent.CLAUDE_DIR, exist_ok=True)
    with open(os.path.join(agent.CLAUDE_DIR, "settings.json"), "w") as f:
        f.write("{not json")
    assert agent._global_defaults() == ("", "")
    with open(os.path.join(agent.CLAUDE_DIR, "settings.json"), "w") as f:
        f.write("[]")
    assert agent._global_defaults() == ("", "")


def test_nothing_detected_returns_empty_not_a_guess(tmp_path, monkeypatch):
    """The page owns the fallback; the agent must not invent one — an empty
    field is the honest answer when there is no history and no settings."""
    agent = _agent_in(tmp_path, monkeypatch)
    file, _ = _target(tmp_path)
    out = agent.main(action="defaults", file=file)
    assert out == {"model": "", "effort": "", "source": "",
                   "backend": "claude", "recorded": {"model": "", "effort": ""}}


def test_unknown_values_never_leak_into_the_answer(tmp_path, monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    file, _ = _target(tmp_path)
    _write_global(agent, model="gpt-42", effortLevel="turbo")
    out = agent.main(action="defaults", file=file)
    assert out["model"] == "" and out["effort"] == ""
    assert out["source"] == ""


def test_short_model_collapses_every_spelling(tmp_path, monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    assert agent._short_model("claude-fable-5") == "fable"
    assert agent._short_model("opusplan") == "opus"
    assert agent._short_model("SONNET") == "sonnet"
    assert agent._short_model("claude-haiku-4-5-20251001") == "haiku"
    assert agent._short_model("gpt-42") == ""
    assert agent._short_model("") == ""


# ── NO WRITE HALF: the Claude settings page is the ONE editor ────────────────
#
# Akshil, 2026-10-28: "from fused-render only settings page should be able to
# edit global claude code config". A PUT on this route used to let the New app
# composer's model / effort pickers rewrite `~/.claude/settings.json`, which
# moved every later Claude run on the machine from a dropdown that read as
# local to the app being made.


def test_the_defaults_route_is_read_only():
    """GET answers the global pair; no method on the route can write it."""
    from fused_render.server.routers import claude_sessions

    methods = set()
    for route in claude_sessions.router.routes:
        if getattr(route, "path", "") == "/api/claude-sessions/defaults":
            methods |= set(route.methods)
    assert methods == {"GET"}


# Who may write `~/.claude/settings.json` at all: the Claude settings page's
# own modules, plus the canvas path's one-key retirement of the plugin the app
# itself once told users to install (D374). Anything else that grows a writer
# of the file — a picker, a route, a helper — fails here first.
_SETTINGS_WRITERS_ALLOWED = {
    os.path.join("fused_render", "claude_config", "preferences.py"),
    os.path.join("fused_render", "claude_config", "plugins.py"),
    os.path.join("fused_render", "claude_config", "marketplaces.py"),
    os.path.join("fused_render", "skill_plugin.py"),
}


def test_only_the_settings_page_writes_the_global_settings_file():
    """Every `fused_render` module that writes `settings.json` is on the
    allowlist above, and every allowlisted module still does."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = os.path.join(root, "fused_render")
    writers = set()
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames
                       if d not in ("static", "node_modules", "__pycache__")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            if re.search(r"write_json\(\s*(?:lib\.)?SETTINGS_PATH\b", src):
                writers.add(os.path.relpath(path, root))
    assert writers == _SETTINGS_WRITERS_ALLOWED
