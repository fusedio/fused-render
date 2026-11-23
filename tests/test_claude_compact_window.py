"""The auto-compact window the user configured for Claude Code.

The CLI resolves it as `min(model window, configured)` where `configured` is
the `CLAUDE_CODE_AUTO_COMPACT_WINDOW` env var, else the `autoCompactWindow`
setting (layers user < project < local < managed, a layer's own value replacing
the one below, `"auto"` meaning "no setting"). `_compact_window` reads the
configured number; the page does the `min`.
"""

import importlib.util
import json
import os

import pytest

AGENT_PATH = os.path.join("fused_render", "claude_agent", "agent.py")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("claude_cw_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MODEL_WINDOWS = str(tmp_path / "model-windows.json")
    home = tmp_path / "claude-home"
    home.mkdir()
    monkeypatch.setattr(mod, "CLAUDE_DIR", str(home))
    monkeypatch.setattr(
        mod, "_managed_settings_path", lambda: str(tmp_path / "managed-settings.json")
    )
    for name in (
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "DISABLE_COMPACT",
        "DISABLE_AUTO_COMPACT",
    ):
        monkeypatch.delenv(name, raising=False)
    return mod


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)
        else:
            json.dump(data, fh)


def _user(agent, data):
    _write(os.path.join(agent.CLAUDE_DIR, "settings.json"), data)


def _project(tmp_path, name, data):
    cwd = tmp_path / "proj"
    _write(str(cwd / ".claude" / name), data)
    return str(cwd)


def test_no_setting_means_no_field(agent, tmp_path):
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) is None
    assert agent._compact_window("claude-opus-5-5", "") is None


def test_user_setting(agent, tmp_path):
    _user(agent, {"autoCompactWindow": 200000})
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) == 200000


def test_layers_later_wins_and_auto_resets(agent, tmp_path):
    _user(agent, {"autoCompactWindow": 200000})
    cwd = _project(tmp_path, "settings.json", {"autoCompactWindow": 300000})
    assert agent._compact_window("m", cwd) == 300000
    _project(tmp_path, "settings.local.json", {"autoCompactWindow": 400000})
    assert agent._compact_window("m", cwd) == 400000
    _write(agent._managed_settings_path(), {"autoCompactWindow": 150000})
    assert agent._compact_window("m", cwd) == 150000
    _project(tmp_path, "settings.local.json", {"autoCompactWindow": "auto"})
    _write(agent._managed_settings_path(), {})
    assert agent._compact_window("m", cwd) is None


def test_per_model_setting_beats_a_lower_layers_default(agent, tmp_path):
    _user(
        agent,
        {
            "autoCompactWindow": 200000,
            "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500000}},
        },
    )
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) == 500000
    assert agent._compact_window("claude-opus-5-5[1m]", str(tmp_path)) == 500000
    assert agent._compact_window("claude-sonnet-5", str(tmp_path)) == 200000


def test_a_layers_default_drops_lower_per_model_entries(agent, tmp_path):
    _user(agent, {"modelSettings": {"m": {"autoCompactWindow": 500000}}})
    cwd = _project(tmp_path, "settings.json", {"autoCompactWindow": 200000})
    assert agent._compact_window("m", cwd) == 200000


def test_env_beats_settings_and_parses_shorthand(agent, tmp_path, monkeypatch):
    _user(agent, {"autoCompactWindow": 200000})
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "300k")
    assert agent._compact_window("m", str(tmp_path)) == 300000
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1m")
    assert agent._compact_window("m", str(tmp_path)) == 1_000_000
    # Under the 100k minimum the CLI treats it as 100k.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "5000")
    assert agent._compact_window("m", str(tmp_path)) == 100000
    # Unusable: ignored, the setting stands.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "lots")
    assert agent._compact_window("m", str(tmp_path)) == 200000


def test_env_block_of_settings_counts_as_env(agent, tmp_path):
    _user(
        agent,
        {
            "autoCompactWindow": 200000,
            "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "250000"},
        },
    )
    assert agent._compact_window("m", str(tmp_path)) == 250000


@pytest.mark.parametrize("bad", ["lots", True, 0, -5, 50000, 1.5e300, None, [], {}])
def test_invalid_setting_values_are_ignored(agent, tmp_path, bad):
    _user(agent, {"autoCompactWindow": bad})
    assert agent._compact_window("m", str(tmp_path)) is None


def test_corrupt_files_are_ignored(agent, tmp_path):
    _user(agent, "{not json")
    cwd = _project(tmp_path, "settings.json", "[1, 2]")
    assert agent._compact_window("m", cwd) is None


def test_stamp_only_adds_the_field_when_set(agent, tmp_path):
    ctx = {"model": "claude-opus-5-5", "window": 1_000_000}
    assert agent._stamp_compact_window(ctx, str(tmp_path)) == ctx
    _user(agent, {"autoCompactWindow": 200000})
    out = agent._stamp_compact_window(ctx, str(tmp_path))
    assert out == {**ctx, "compact_window": 200000}
    assert "compact_window" not in ctx  # not mutated in place
    # A stale value is dropped when the setting is gone.
    _user(agent, {})
    assert "compact_window" not in agent._stamp_compact_window(out, str(tmp_path))
    assert agent._stamp_compact_window(None, str(tmp_path)) is None


def test_history_stamps_the_configured_window(agent, tmp_path, monkeypatch):
    target = tmp_path / "proj" / "page.html"
    os.makedirs(target.parent, exist_ok=True)
    target.write_text("<html></html>")
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    d = projects / agent._munge(str(target.parent))
    os.makedirs(d, exist_ok=True)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {
            "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "yo"}],
            "usage": {"input_tokens": 5, "output_tokens": 7}}},
    ]
    with open(d / "sess1.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    assert "compact_window" not in agent._history(str(target), "sess1")["context"]
    # The run's own folder settings apply, not just the user's.
    _write(str(target.parent / ".claude" / "settings.json"),
           {"autoCompactWindow": 200000})
    out = agent._history(str(target), "sess1")
    assert out["context"]["compact_window"] == 200000


def test_poll_stamps_the_configured_window(agent, tmp_path):
    target = tmp_path / "proj" / "page.html"
    os.makedirs(target.parent, exist_ok=True)
    target.write_text("<html></html>")
    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm", exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target)}))
    rows = [
        {"type": "assistant", "message": {
            "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "yo"}],
            "usage": {"input_tokens": 5, "output_tokens": 7}}},
        {"type": "result", "session_id": "s", "result": "yo"},
    ]
    with open(run_dir / "out.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    agent.RUNS = str(tmp_path)
    agent._alive = lambda _run_dir: True
    assert "compact_window" not in agent._poll("run")["context"]
    _user(agent, {"autoCompactWindow": 200000})
    assert agent._poll("run")["context"]["compact_window"] == 200000


# --- review fixes -----------------------------------------------------------


def test_auto_compact_disabled_in_settings_means_no_window(agent, tmp_path):
    _user(agent, {"autoCompactWindow": 200000, "autoCompactEnabled": False})
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) is None
    # a higher layer can turn it back on
    cwd = _project(tmp_path, "settings.json", {"autoCompactEnabled": True})
    assert agent._compact_window("claude-opus-5-5", cwd) == 200000


@pytest.mark.parametrize("name", ["DISABLE_COMPACT", "DISABLE_AUTO_COMPACT"])
def test_disable_env_means_no_window(agent, tmp_path, monkeypatch, name):
    _user(agent, {"autoCompactWindow": 200000})
    monkeypatch.setenv(name, "1")
    assert agent._compact_window("m", str(tmp_path)) is None
    monkeypatch.setenv(name, "false")
    assert agent._compact_window("m", str(tmp_path)) == 200000


def test_disable_env_in_settings_env_block(agent, tmp_path):
    _user(
        agent,
        {"autoCompactWindow": 200000, "env": {"DISABLE_AUTO_COMPACT": "true"}},
    )
    assert agent._compact_window("m", str(tmp_path)) is None


def test_env_window_also_off_when_disabled(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200k")
    monkeypatch.setenv("DISABLE_COMPACT", "1")
    assert agent._compact_window("m", str(tmp_path)) is None


def test_per_model_auto_is_the_model_default_not_the_top_level(agent, tmp_path):
    # The CLI: a per-model entry that is present wins, and "auto" means the
    # model's own default, not the layer's top-level number.
    _user(
        agent,
        {
            "autoCompactWindow": 200000,
            "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}},
        },
    )
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) is None
    assert agent._compact_window("claude-sonnet-5", str(tmp_path)) == 200000


@pytest.mark.parametrize("bad", [50000, "banana", True])
def test_invalid_per_model_entry_falls_through_to_default(agent, tmp_path, bad):
    # The CLI's schema drops an invalid per-model entry.
    _user(
        agent,
        {
            "autoCompactWindow": 200000,
            "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": bad}},
        },
    )
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) == 200000


def test_env_names_are_case_sensitive(agent, tmp_path):
    _user(agent, {"env": {"claude_code_auto_compact_window": "200k"}})
    assert agent._compact_window("m", str(tmp_path)) is None


def test_exact_per_model_key_beats_stripped_one_whatever_the_order(agent, tmp_path):
    for entries in (
        {"claude-opus-5-5[1m]": 300000, "claude-opus-5-5": 200000},
        {"claude-opus-5-5": 200000, "claude-opus-5-5[1m]": 300000},
    ):
        _user(
            agent,
            {
                "modelSettings": {
                    k: {"autoCompactWindow": v} for k, v in entries.items()
                }
            },
        )
        assert agent._compact_window("claude-opus-5-5", str(tmp_path)) == 200000
        assert agent._compact_window("claude-opus-5-5[1m]", str(tmp_path)) == 200000


def test_variant_key_alone_still_matches(agent, tmp_path):
    _user(
        agent,
        {"modelSettings": {"claude-opus-5-5[1m]": {"autoCompactWindow": 300000}}},
    )
    assert agent._compact_window("claude-opus-5-5", str(tmp_path)) == 300000


def test_settings_layers_are_cached_by_mtime(agent, tmp_path, monkeypatch):
    _user(agent, {"autoCompactWindow": 200000})
    cwd = str(tmp_path)
    assert agent._compact_window("m", cwd) == 200000
    opened = []
    real_open = open

    def spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    assert agent._compact_window("m", cwd) == 200000
    assert not [p for p in opened if p.endswith("settings.json")]
    # a rewrite (new mtime) is re-read
    path = os.path.join(agent.CLAUDE_DIR, "settings.json")
    _user(agent, {"autoCompactWindow": 300000})
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert agent._compact_window("m", cwd) == 300000
