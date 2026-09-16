"""The `defaults` action: preselecting the model/effort the user ACTUALLY last
used with Claude Code for this project — read from the project's session
transcripts (shared by the CLI and this app, both key sessions on the same
cwd munge) and, failing that, from settings files. A hardcoded "default" in the
selector tells the user nothing; the real config does.
"""
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest


#: The native composer's own vocabularies, the TypeScript half of the parity
#: the `defaults` action lives or dies by (03 §4e).
_COMPOSER = (Path(__file__).resolve().parent.parent / "frontend" / "src" /
             "apps" / "claude" / "ui" / "composer-defaults.ts")


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


def _ts_list(src: str, name: str) -> list:
    """The string members of an `export const <name> = [ ... ]` TS array.

    Deliberately textual: a Python test cannot import TypeScript, so the wire
    vocabulary is read the same way tests/test_trouble_parity.py reads the
    shell's own constants. Tolerates line breaks, trailing commas, `as const`
    and either quote style.
    """
    m = re.search(r"export\s+const\s+%s\s*(?::[^=]*)?=\s*\[(.*?)\]" % name,
                  src, re.DOTALL)
    assert m, f"no `export const {name} = [...]` in the native source"
    return re.findall(r"""['"]([^'"]+)['"]""", m.group(1))


def test_every_model_detection_can_return_is_one_the_native_picker_offers():
    """`defaults` preselects a pill. A value the pill's own list does not hold
    is dropped by the picker (`resolveModel` falls back to DEFAULT_MODEL), so a
    spelling agent.py can return and ModelSelect cannot show is a preselect
    that silently never happens — the exact failure `_short_model`'s docstring
    describes, with the JS half now in TypeScript."""
    agent = _load_agent()
    models = _ts_list(_COMPOSER.read_text(encoding="utf-8"), "MODELS")
    detectable = set(agent._MODEL_SHORT) | {v for _, v in agent._MODEL_PINNED}
    assert detectable <= set(models), detectable - set(models)
    # The pinned ids lead: someone opening the menu is usually after a specific
    # model, and every pinned id contains its own family name.
    assert models[0] == "claude-fable-5-1"


def test_every_effort_detection_can_return_is_one_the_native_picker_offers():
    agent = _load_agent()
    efforts = _ts_list(_COMPOSER.read_text(encoding="utf-8"), "EFFORTS")
    assert list(agent._EFFORT_LEVELS) == efforts


def test_the_stored_default_model_pref_is_spelled_the_picker_s_way():
    """`prefs.VALID_DEFAULT_MODELS` is what the Preferences page stores and the
    composer reads as the third-ranked answer (param > detected > pref). A
    pref value the picker's list does not hold resolves to DEFAULT_MODEL, so
    the user's saved default would be silently ignored."""
    from fused_render.shell import prefs
    models = set(_ts_list(_COMPOSER.read_text(encoding="utf-8"), "MODELS"))
    stored = {m for m in prefs.VALID_DEFAULT_MODELS if m}
    assert stored <= models, stored - models
    # "" is the "no stored default" sentinel, never an option on the pill.
    assert "" in prefs.VALID_DEFAULT_MODELS and "" not in models
