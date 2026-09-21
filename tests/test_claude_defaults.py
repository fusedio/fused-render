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
"""
import importlib.util
import json
import os

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
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
                   "recorded": {"model": "", "effort": ""}}


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
                   "recorded": {"model": "", "effort": ""}}


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


def test_the_page_asks_and_ranks_detection_below_an_explicit_choice():
    html = open(os.path.join("fused_render", "templates", "claude",
                             "template.html"), encoding="utf-8").read()
    assert '{ action: "defaults", file: FILE }' in html
    # explicit pane param > detected config > user preference > hardcoded
    # fallback. Detection is this chat's own record and transcript when it names
    # a session, and ~/.claude/settings.json when it does not; the app's own
    # preference is what fills the gap when neither says anything.
    assert (
        'fused.params.get("model") || detectedModel || prefModel || DEFAULT_MODEL' in html
    )
    assert 'fused.params.get("effort") || detectedEffort || DEFAULT_EFFORT' in html
    # detected values are validated against the selector's own lists — the
    # model through `shortModel` first, so a transcript still naming the retired
    # pinned Fable id preselects the alias that replaced it instead of nothing.
    assert "const dm = d && shortModel(d.model);" in html
    assert "MODELS.includes(dm)" in html
    assert "EFFORTS.includes(d.effort)" in html
    # …and so is the preference, for the same reason: a name this build's
    # selector doesn't have cannot be shown as selected.
    assert 'fetch("/api/prefs")' in html
    assert "MODELS.includes(m)" in html


# ── THE WRITE HALF: `PUT /api/claude-sessions/defaults` ──────────────────────
#
# One value, two surfaces that both read AND write it (Akshil, 2026-09-21,
# after testing #1281: "I don't see this being followed"). The Explorer
# composer's pills for a chat with no session yet and the New task card's
# Model / Thinking dropdowns are two editors of the same setting, so a pick on
# either lands in `~/.claude/settings.json` — through the settings page's own
# writer, `claude_config.preferences.main("patch", …)`, and not a second copy
# of it.


@pytest.fixture()
def defaults_api(tmp_path, monkeypatch):
    """The endpoint pair, with BOTH readers of `~/.claude` repointed at a
    scratch dir.

    There are two, deliberately, and they read different env vars: agent.py is
    a template (it knows `CLAUDE_CONFIG_DIR` and imports nothing of
    fused_render), while `claude_config.lib` is the app's own and knows
    `CLAUDE_DIR`. Both are resolved at import, so the constants are what a test
    has to move — missing one writes into the developer's real config."""
    from fused_render.claude_config import lib
    from fused_render.server.routers import claude_sessions, tasks

    agent = _agent_in(tmp_path, monkeypatch)
    monkeypatch.setattr(tasks, "_agent_module", lambda: agent)
    monkeypatch.setattr(lib, "CLAUDE_DIR", agent.CLAUDE_DIR)
    monkeypatch.setattr(lib, "SETTINGS_PATH",
                        os.path.join(agent.CLAUDE_DIR, "settings.json"))
    monkeypatch.setattr(lib, "_LOCK_PATH",
                        os.path.join(str(tmp_path), ".config-ui.lock"))
    # The config dir is a git repo the writer commits into. Kept off the
    # developer's identity and global config, exactly as test_claude_config_api
    # does.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Fixture Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Fixture Author")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.com")
    return claude_sessions, agent


def _put(mod, **body):
    return mod.set_claude_defaults(mod.DefaultsPatch(**body), x_fused="1")


def _settings(agent):
    with open(os.path.join(agent.CLAUDE_DIR, "settings.json"), encoding="utf-8") as f:
        return json.load(f)


def test_a_pick_writes_the_global_pair_and_reads_it_back(defaults_api):
    mod, agent = defaults_api
    _write_global(agent, model="fable", effortLevel="low")
    assert _put(mod, model="opus") == {"model": "opus", "effort": "low"}
    assert _settings(agent)["model"] == "opus"
    # PER FIELD: moving the model left the effort exactly where it was, which
    # is what keeps the two dropdowns two decisions.
    assert _settings(agent)["effortLevel"] == "low"
    assert _put(mod, effort="max") == {"model": "opus", "effort": "max"}
    assert _settings(agent) == {"model": "opus", "effortLevel": "max"}


def test_every_other_key_in_the_file_survives_the_write(defaults_api):
    """The writer is the settings page's own — read-modify-write under the
    config lock — so this file is edited, never replaced."""
    mod, agent = defaults_api
    _write_global(agent, model="fable", effortLevel="low",
                  includeCoAuthoredBy=False, env={"FOO": "bar"})
    _put(mod, model="haiku")
    data = _settings(agent)
    assert data["includeCoAuthoredBy"] is False
    assert data["env"] == {"FOO": "bar"}
    assert data["model"] == "haiku"


def test_a_settings_file_that_does_not_exist_yet_is_created(defaults_api):
    mod, agent = defaults_api
    assert not os.path.exists(os.path.join(agent.CLAUDE_DIR, "settings.json"))
    assert _put(mod, model="sonnet", effort="high") == {"model": "sonnet",
                                                       "effort": "high"}
    assert _settings(agent) == {"model": "sonnet", "effortLevel": "high"}


def test_an_unknown_model_or_effort_is_refused_and_nothing_is_written(defaults_api):
    """The vocabularies are the ones the pickers offer — a value no <select>
    holds renders as a blank pill and is also what would reach the CLI."""
    from fastapi import HTTPException

    mod, agent = defaults_api
    _write_global(agent, model="fable", effortLevel="low")
    with pytest.raises(HTTPException) as bad_model:
        _put(mod, model="gpt-42")
    assert bad_model.value.status_code == 400
    with pytest.raises(HTTPException) as bad_effort:
        _put(mod, effort="turbo")
    assert bad_effort.value.status_code == 400
    assert _settings(agent) == {"model": "fable", "effortLevel": "low"}


def test_the_settings_page_spellings_are_accepted_and_folded_on_the_way_out(
        defaults_api):
    """The catalog offers `opus[1m]`; the pills cannot say it. Writing it is
    allowed — this is the same field that page edits — and it reads back as the
    family name, which is what the pills can show."""
    mod, agent = defaults_api
    assert _put(mod, model="opus[1m]") == {"model": "opus", "effort": ""}
    assert _settings(agent)["model"] == "opus[1m]"


def test_an_empty_value_resets_the_key_rather_than_writing_one(defaults_api):
    """"" is how the settings page clears a field: the key goes, and the CLI
    resolves its own default again."""
    mod, agent = defaults_api
    _write_global(agent, model="fable", effortLevel="low")
    assert _put(mod, model="") == {"model": "", "effort": "low"}
    assert _settings(agent) == {"effortLevel": "low"}


def test_a_write_without_the_fused_header_is_refused(defaults_api):
    """D3: a custom request header forces a CORS preflight, so a foreign page
    cannot fire this blind."""
    mod, agent = defaults_api
    _write_global(agent, model="fable", effortLevel="low")
    out = mod.set_claude_defaults(mod.DefaultsPatch(model="opus"), x_fused=None)
    assert out.status_code == 403
    assert _settings(agent) == {"model": "fable", "effortLevel": "low"}


def test_an_empty_body_changes_nothing_and_still_answers(defaults_api):
    mod, agent = defaults_api
    _write_global(agent, model="fable", effortLevel="low")
    assert _put(mod) == {"model": "fable", "effort": "low"}
    assert _settings(agent) == {"model": "fable", "effortLevel": "low"}
