"""The `defaults` action: preselecting the model/effort the user ACTUALLY last
used with Claude Code for this project — read from the project's session
transcripts (shared by the CLI and this template, both key sessions on the same
cwd munge) and, failing that, from settings files. A hardcoded "default" in the
selector tells the user nothing; the real config does.
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


def test_last_used_comes_from_the_newest_transcript_rows(tmp_path, monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    file, workdir = _target(tmp_path)
    _write_transcript(agent, workdir, [
        {"effort": "medium", "message": {"model": "claude-sonnet-5"}},
        # sidechain rows are subagents — the user never chose their model
        {"isSidechain": True, "message": {"model": "claude-haiku-4-5-20251001"}},
        {"effort": "xhigh", "message": {"model": "claude-fable-5"}},
    ])
    out = agent.main(action="defaults", file=file)
    assert out == {"model": "fable", "effort": "xhigh", "source": "session"}


def test_settings_fill_in_when_no_transcript_speaks(tmp_path, monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    file, workdir = _target(tmp_path)
    # project settings outrank the global file, most-specific first
    proj_cfg = os.path.join(workdir, ".claude")
    os.makedirs(proj_cfg)
    with open(os.path.join(proj_cfg, "settings.json"), "w") as f:
        json.dump({"model": "opusplan"}, f)
    os.makedirs(agent.CLAUDE_DIR, exist_ok=True)
    with open(os.path.join(agent.CLAUDE_DIR, "settings.json"), "w") as f:
        json.dump({"model": "sonnet", "effortLevel": "low"}, f)
    out = agent.main(action="defaults", file=file)
    assert out["model"] == "opus"      # project wins, alias collapsed
    assert out["effort"] == "low"      # global fills what the project omits
    assert out["source"] == "settings"


def test_nothing_detected_returns_empty_not_a_guess(tmp_path, monkeypatch):
    """The page owns the fallback; the agent must not invent one — an empty
    field is the honest answer when there is no history and no settings."""
    agent = _agent_in(tmp_path, monkeypatch)
    file, _ = _target(tmp_path)
    out = agent.main(action="defaults", file=file)
    assert out == {"model": "", "effort": "", "source": ""}


def test_unknown_values_never_leak_into_the_answer(tmp_path, monkeypatch):
    agent = _agent_in(tmp_path, monkeypatch)
    file, workdir = _target(tmp_path)
    _write_transcript(agent, workdir, [
        {"effort": "turbo", "message": {"model": "gpt-42"}},
    ])
    out = agent.main(action="defaults", file=file)
    assert out["model"] == "" and out["effort"] == ""


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
    # explicit pane param > detected config > hardcoded fallback
    assert 'fused.params.get("model") || detectedModel || DEFAULT_MODEL' in html
    assert 'fused.params.get("effort") || detectedEffort || DEFAULT_EFFORT' in html
    # detected values are validated against the selector's own lists
    assert "MODELS.includes(d.model)" in html
    assert "EFFORTS.includes(d.effort)" in html
