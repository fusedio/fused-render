"""`agent_backends/opencode_config.build` — the config every OpenCode child
runs with, injected through `OPENCODE_CONFIG_CONTENT` (docs/OPENCODE_BACKEND.md
D5, D7, D8, D19, D20). Stdlib-only and importable by `claude_agent/agent.py`,
the health probe and the recap, so it is pinned here on its own.
"""
import importlib.util
import json
import os
import sys

import pytest

from fused_render.agent_backends import opencode_config as cfg

_HERE = os.path.dirname(os.path.abspath(__file__))


def test_the_module_is_stdlib_and_loads_by_path(tmp_path):
    """agent.py loads it by file path from a process where the package is
    not importable: the import lines must be stdlib only, and the lazy app
    imports must read as "nothing" there rather than raise."""
    src = open(cfg.__file__, encoding="utf-8").read()
    for line in src.splitlines():
        if line.startswith(("import ", "from ")):
            assert "fused_render" not in line, line
    spec = importlib.util.spec_from_file_location("opencode_config_by_path", cfg.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build(model=None, mode="auto", instructions_path=None, local=[],
                     origin=None, key=None) == {
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"}}


def test_the_mode_table_is_d5s():
    assert cfg.build(model=None, mode="prompt", instructions_path=None, local=[],
                     origin=None, key=None)["permission"] == {
        "edit": "ask", "bash": "ask", "webfetch": "ask"}
    assert cfg.build(model=None, mode="default", instructions_path=None, local=[],
                     origin=None, key=None)["permission"] == {
        "edit": "ask", "bash": "ask", "webfetch": "ask"}
    assert cfg.build(model=None, mode="acceptEdits", instructions_path=None, local=[],
                     origin=None, key=None)["permission"] == {
        "edit": "allow", "bash": "ask", "webfetch": "ask"}
    assert cfg.build(model=None, mode="auto", instructions_path=None, local=[],
                     origin=None, key=None)["permission"] == {
        "edit": "allow", "bash": "allow", "webfetch": "allow"}
    # An unknown or empty mode is Ask, the safest; `plan` is never a mode here.
    for mode in ("", "plan", "bogus", None):
        assert cfg.build(model=None, mode=mode, instructions_path=None, local=[],
                         origin=None, key=None)["permission"] == cfg.PERMISSIONS["prompt"]


def test_deny_all_tools_denies_every_known_tool():
    """The recap's rule (D12/D16): every tool OpenCode gates, read-only ones
    included, whatever the mode said."""
    out = cfg.build(model="anthropic/claude-sonnet-5-5", mode="auto",
                    instructions_path=None, local=[], origin=None, key=None,
                    deny_all_tools=True)
    assert out["permission"] == {t: "deny" for t in cfg.KNOWN_TOOLS}
    assert {"bash", "edit", "write", "read", "glob", "grep", "list", "webfetch",
            "websearch", "todowrite", "task", "patch"} == set(cfg.KNOWN_TOOLS)
    assert out["model"] == "anthropic/claude-sonnet-5-5"


def test_model_and_instructions_ride_only_when_given():
    base = cfg.build(model=None, mode="prompt", instructions_path=None, local=[],
                     origin=None, key=None)
    assert set(base) == {"permission"}
    out = cfg.build(model="openai/gpt-5.5", mode="prompt",
                    instructions_path="/runs/x/opencode/AGENTS.md", local=[],
                    origin=None, key=None)
    assert out["model"] == "openai/gpt-5.5"
    assert out["instructions"] == ["/runs/x/opencode/AGENTS.md"]
    # "" reads as not given.
    out = cfg.build(model="", mode="prompt", instructions_path="", local=[],
                    origin=None, key=None)
    assert "model" not in out and "instructions" not in out


_LOCAL = [
    {"id": "qwen3-8b", "label": "Qwen3 8B", "context": 32768, "max_output": 4096,
     "tools": True},
    {"id": "tiny", "label": "Tiny", "context": 2048, "max_output": 512, "tools": False},
    {"id": "", "label": "no id"}, "junk",
]


def test_the_fused_provider_needs_local_models_an_origin_and_a_key():
    """D20: the provider block is written only when all three are there —
    no origin means nothing to call, no key means every call would 401."""
    for local, origin, key in ((_LOCAL, None, "k"), (_LOCAL, "http://o", None),
                               ([], "http://o", "k"), (["junk"], "http://o", "k")):
        out = cfg.build(model=None, mode="prompt", instructions_path=None,
                        local=local, origin=origin, key=key)
        assert "provider" not in out, (local, origin, key)
    out = cfg.build(model="fused/qwen3-8b", mode="auto", instructions_path=None,
                    local=_LOCAL, origin="http://127.0.0.1:2777/", key="sk-secret-123")
    assert out["provider"] == {"fused": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Fused Render (local)",
        "options": {"baseURL": "http://127.0.0.1:2777/v1",
                    "apiKey": "{env:FUSED_RENDER_V1_KEY}"},
        "models": {
            "qwen3-8b": {"name": "Qwen3 8B (local)",
                         "limit": {"context": 32768, "output": 4096}},
            "tiny": {"name": "Tiny (local)", "limit": {"context": 2048, "output": 512},
                     "tool_call": False},
        }}}
    # The key VALUE never lands in the config: the env carries it.
    assert "sk-secret-123" not in json.dumps(out)
    assert cfg.KEY_ENV == "FUSED_RENDER_V1_KEY"
    assert cfg.CONTENT_ENV == "OPENCODE_CONFIG_CONTENT"
    # A row with no limits or label still lists, by id.
    out = cfg.build(model=None, mode="prompt", instructions_path=None,
                    local=[{"id": "bare", "context": "x"}], origin="http://o", key="k")
    assert out["provider"]["fused"]["models"] == {"bare": {"name": "bare (local)"}}


def test_the_lazy_app_hooks_never_raise(monkeypatch):
    """`local_models`, `session_key` and `release_key` read the package
    lazily and answer "nothing" when it, the function or the call is not
    there — the host process has none of them."""
    import fused_render.ai.catalog as catalog
    import fused_render.ai.gateway_keys as gateway_keys
    monkeypatch.setattr(catalog, "local_agent_models",
                        lambda: [{"id": "a", "label": "A"}, {"label": "no id"}, "junk"])
    assert cfg.local_models() == [{"id": "a", "label": "A"}]
    monkeypatch.setattr(catalog, "local_agent_models",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cfg.local_models() == []
    monkeypatch.delattr(catalog, "local_agent_models")
    assert cfg.local_models() == []
    monkeypatch.setattr(gateway_keys, "mint_session_key", lambda: "k9")
    assert cfg.session_key() == "k9"
    monkeypatch.setattr(gateway_keys, "mint_session_key",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cfg.session_key() is None
    released = []
    monkeypatch.setattr(gateway_keys, "release_key", released.append)
    cfg.release_key("k9")
    cfg.release_key(None)
    assert released == ["k9"]
    monkeypatch.setattr(gateway_keys, "release_key",
                        lambda k: (_ for _ in ()).throw(RuntimeError("boom")))
    cfg.release_key("k9")  # swallowed
    # The package not importable at all: still nothing, still no raise.
    monkeypatch.setitem(sys.modules, "fused_render.ai.catalog", None)
    monkeypatch.setitem(sys.modules, "fused_render.ai.gateway_keys", None)
    assert cfg.local_models() == [] and cfg.session_key() is None
    cfg.release_key("k9")


def test_config_dir_follows_the_override_then_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert cfg.config_dir() == str(tmp_path / "h" / ".config" / "opencode")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cfg.config_dir() == str(tmp_path / "xdg" / "opencode")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "~/own")
    assert cfg.config_dir() == str(tmp_path / "h" / "own")
