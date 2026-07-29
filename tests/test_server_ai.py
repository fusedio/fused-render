"""Tests for fused.ai (SPEC RH-11): the /api/ai endpoint backed by the claude
(Claude Code) CLI, the runtime surface that calls it, and the binary
resolution (FUSED_RENDER_CLAUDE_BIN / PATH).

The endpoint is driven through module-level `_ai_relay` with the subprocess
hop (`_run_claude_cli`) mocked (the "avoid starlette TestClient" discipline of
test_server_fs_write.py) — no test ever runs a real CLI. The runtime checks
are string-contract checks over the shipped static/runtime.js, like
test_runtime_cancellation.py.
"""
import asyncio
import json
from pathlib import Path

import pytest

import fused_render
from fused_render import server
from fused_render.export import plan_export

_STATIC = Path(fused_render.__file__).parent / "static"
RUNTIME = (_STATIC / "runtime.js").read_text(encoding="utf-8")


def _relay(body):
    return asyncio.run(server._ai_relay(body))


def _data(resp) -> dict:
    return json.loads(bytes(resp.body))


# The real output shape of `claude -p ... --output-format json` (2.1.220),
# trimmed to the fields the server reads plus a few it must ignore.
_CLI_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1234,
    "result": "hi there",
    "total_cost_usd": 0.0011,
    "usage": {"input_tokens": 3, "output_tokens": 2,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 3}},
}


class _FakeCLI:
    """Stands in for server._run_claude_cli: canned (rc, stdout, stderr), or raises."""

    def __init__(self, stdout="", returncode=0, stderr="", exc=None):
        self._result = (returncode, stdout, stderr)
        self._exc = exc
        self.calls = []  # (argv, env, timeout, stdin_text) of every run

    async def __call__(self, argv, env, timeout, stdin_text=""):
        self.calls.append((argv, env, timeout, stdin_text))
        if self._exc is not None:
            raise self._exc
        return self._result


def _cli_ok(monkeypatch, payload=None, **kwargs):
    if payload is not None:
        kwargs["stdout"] = json.dumps(payload)
    fake = _FakeCLI(**kwargs)
    monkeypatch.setattr(server, "_run_claude_cli", fake)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    return fake


def _flag(argv, name):
    return argv[argv.index(name) + 1]


# -- happy path -----------------------------------------------------------------


def test_relay_happy_path(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 200
    data = _data(resp)
    assert data["ok"] is True
    assert data["result"]["text"] == "hi there"
    assert data["result"]["model"] == "claude-haiku-4-5-20251001"
    assert data["result"]["usage"] == {"input_tokens": 3, "output_tokens": 2}
    # One CLI invocation: the prompt over stdin (argv has an OS size cap), the
    # default model, the default system prompt, a bare one-shot (no tools, no
    # settings, no session), and the medium effort default carried as the
    # max-output-tokens env var.
    (argv, env, timeout, stdin_text), = fake.calls
    assert argv[0] == "/usr/local/bin/claude"
    assert "-p" in argv
    assert stdin_text == "hello"
    assert "hello" not in argv  # prompt travels over stdin, never argv
    assert _flag(argv, "--output-format") == "json"
    assert _flag(argv, "--model") == server._AI_DEFAULT_MODEL
    assert _flag(argv, "--system-prompt") == server._AI_DEFAULT_SYSTEM_PROMPT
    assert _flag(argv, "--tools") == ""
    assert _flag(argv, "--setting-sources") == ""
    assert "--no-session-persistence" in argv
    assert _flag(argv, "--max-turns") == "1"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    assert timeout == server._AI_TIMEOUT_S


def test_relay_options_reach_the_cli(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    _relay({"prompt": "hello", "system_prompt": "be terse",
            "model": "claude-sonnet-5", "effort": "high"})
    (argv, env, _, _), = fake.calls
    assert _flag(argv, "--model") == "claude-sonnet-5"
    assert _flag(argv, "--system-prompt") == "be terse"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"  # effort: high


def test_relay_explicit_max_tokens_beats_effort(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    _relay({"prompt": "hello", "effort": "low", "max_tokens": 99})
    (_, env, _, _), = fake.calls
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "99"


def test_relay_usage_is_normalized_to_the_two_token_keys(monkeypatch):
    # The schema guarantee (RH-11): usage is null or EXACTLY
    # {input_tokens, output_tokens} — extra CLI keys stripped, and a missing
    # or malformed block degrades to null rather than leaking through.
    _cli_ok(monkeypatch, _CLI_RESULT)  # has cache_* extras
    usage = _data(_relay({"prompt": "x"}))["result"]["usage"]
    assert usage == {"input_tokens": 3, "output_tokens": 2}

    for bad in (None, "lots", [], {"input_tokens": 3},           # missing key
                {"input_tokens": "3", "output_tokens": 2},        # wrong type
                {"input_tokens": True, "output_tokens": 2}):      # bool
        _cli_ok(monkeypatch, {**_CLI_RESULT, "usage": bad})
        assert _data(_relay({"prompt": "x"}))["result"]["usage"] is None
    payload = dict(_CLI_RESULT)
    del payload["usage"]
    _cli_ok(monkeypatch, payload)
    assert _data(_relay({"prompt": "x"}))["result"]["usage"] is None


def test_relay_model_echo_prefers_the_resolved_id(monkeypatch):
    # A model alias goes to the CLI as-is, but the response echoes the full id
    # the CLI actually ran (the modelUsage key).
    fake = _cli_ok(monkeypatch, dict(
        _CLI_RESULT, modelUsage={"claude-sonnet-5-20250929": {}}))
    resp = _relay({"prompt": "hello", "model": "sonnet"})
    (argv, _, _, _), = fake.calls
    assert _flag(argv, "--model") == "sonnet"
    assert _data(resp)["result"]["model"] == "claude-sonnet-5-20250929"


# -- bad requests ---------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {},                       # missing prompt
    {"prompt": ""},           # empty
    {"prompt": "   "},        # whitespace-only
    {"prompt": 42},           # wrong type
])
def test_relay_rejects_bad_prompt(monkeypatch, body):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    resp = _relay(body)
    assert resp.status_code == 400
    data = _data(resp)
    assert data["ok"] is False
    assert data["error"]["type"] == "bad_request"
    assert fake.calls == []  # never reached the CLI


def test_relay_large_prompt_travels_over_stdin(monkeypatch):
    # The documented fused.ai pattern embeds JSON aggregates in the prompt; a
    # ~200KB one would blow the OS argv cap (~32K Windows), so it must never
    # appear in argv.
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    big = "x" * 200_000
    resp = _relay({"prompt": big})
    assert resp.status_code == 200
    (argv, _, _, stdin_text), = fake.calls
    assert stdin_text == big
    assert all(len(a) < 1000 for a in argv)


def test_relay_rejects_unknown_effort_and_bad_max_tokens(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    for body in ({"prompt": "x", "effort": "extreme"},
                 {"prompt": "x", "max_tokens": 0},
                 {"prompt": "x", "max_tokens": True},
                 {"prompt": "x", "max_tokens": "many"},
                 {"prompt": "x", "model": 42},
                 {"prompt": "x", "model": ""},
                 {"prompt": "x", "model": "   "},
                 {"prompt": "x", "model": ["claude-haiku-4-5-20251001"]}):
        resp = _relay(body)
        assert resp.status_code == 400
        assert _data(resp)["error"]["type"] == "bad_request"
    assert fake.calls == []


# -- CLI failures ---------------------------------------------------------------


def test_relay_missing_binary_is_ai_unavailable(monkeypatch):
    fake = _FakeCLI(stdout=json.dumps(_CLI_RESULT))
    monkeypatch.setattr(server, "_run_claude_cli", fake)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    # neutralize the install-dir fallbacks (the dev machine may really have one)
    monkeypatch.setattr(server.os.path, "isfile", lambda p: False)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_unavailable"
    # The message says how to fix it.
    assert "claude" in data["error"]["message"]
    assert "FUSED_RENDER_CLAUDE_BIN" in data["error"]["message"]
    assert fake.calls == []


def test_relay_nonzero_exit_is_ai_error(monkeypatch):
    _cli_ok(monkeypatch, returncode=1, stderr="Invalid model name: nope")
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "Invalid model name" in data["error"]["message"]


def test_relay_timeout_is_timeout(monkeypatch):
    _cli_ok(monkeypatch, exc=asyncio.TimeoutError())
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    assert _data(resp)["error"]["type"] == "timeout"


def test_relay_unparseable_stdout_is_ai_error(monkeypatch):
    _cli_ok(monkeypatch, stdout="not json at all")
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["error"]["type"] == "ai_error"


def test_relay_is_error_result_is_ai_error(monkeypatch):
    _cli_ok(monkeypatch, dict(
        _CLI_RESULT, is_error=True, subtype="error_during_execution",
        result="something broke"))
    resp = _relay({"prompt": "hello"})
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "something broke" in data["error"]["message"]


def test_relay_stderr_warnings_are_ignored_on_success(monkeypatch):
    # A zero exit with noise on stderr (connector notices etc.) is a success.
    _cli_ok(monkeypatch, stdout=json.dumps(_CLI_RESULT),
            stderr="Warning: some connector notice")
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True


# -- binary resolution ------------------------------------------------------------


def test_claude_bin_env_override_beats_path(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", "/opt/custom/claude")
    assert server._claude_bin() == "/opt/custom/claude"
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN")
    assert server._claude_bin() == "/usr/bin/claude"


def test_claude_bin_falls_back_to_install_dirs(monkeypatch, tmp_path):
    # A Finder/Dock-launched .app inherits a stripped PATH; the resolver must
    # then try the usual install dirs (executable files only).
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bin_path = home / ".local" / "bin" / "claude"
    bin_path.write_text("#!/bin/sh\n")

    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(server.os.path, "expanduser",
                        lambda p: p.replace("~", str(home), 1))

    assert server._claude_bin() is None  # exists but not executable
    bin_path.chmod(0o755)
    assert server._claude_bin() == str(bin_path)


def test_claude_bin_fallback_dirs_match_the_chat_template(monkeypatch):
    # server._claude_bin deliberately duplicates the chat template's resolver
    # (templates are standalone user-forkable code the server never imports).
    # Pin the two candidate lists together so they can't drift (the D146
    # discipline: a duplicate is held by a test, not a comment).
    import inspect

    from fused_render.templates.claude import agent

    def candidates(fn):
        src = inspect.getsource(fn)
        return [c for c in ("~/.local/bin/claude", "/opt/homebrew/bin/claude",
                            "/usr/local/bin/claude") if c in src]

    assert candidates(server._claude_bin) == candidates(agent._claude_bin)
    assert len(candidates(server._claude_bin)) == 3


def test_relay_uses_the_overridden_binary(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", "/opt/custom/claude")
    _relay({"prompt": "hello"})
    (argv, _, _, _), = fake.calls
    assert argv[0] == "/opt/custom/claude"


# -- runtime surface --------------------------------------------------------------


def test_runtime_ships_ai():
    assert "function ai(prompt, opts)" in RUNTIME
    assert '"/api/ai"' in RUNTIME
    assert "ai," in RUNTIME  # registered on window.fused


def test_runtime_ai_rejects_empty_prompt_client_side():
    # The empty-prompt guard runs before any fetch, tagged bad_request like the
    # server's own rejection.
    assert 'err.type = "bad_request"' in RUNTIME


# -- export stance -----------------------------------------------------------------


def test_export_rejects_ai(tmp_path):
    html = "<script>fused.ai('summarize this');</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("fused.ai() is not supported on a hosted page" in e
               for e in plan.errors)
