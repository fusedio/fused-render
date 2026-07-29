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
import os
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
        self.sp_contents = {}  # --system-prompt-file path -> its text AT CALL TIME

    async def __call__(self, argv, env, timeout, stdin_text=""):
        self.calls.append((argv, env, timeout, stdin_text))
        # Snapshot the system-prompt file now — the relay deletes it after.
        if "--system-prompt-file" in argv:
            path = argv[argv.index("--system-prompt-file") + 1]
            self.sp_contents[path] = Path(path).read_text(encoding="utf-8")
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
    # System prompt rides a temp FILE, never argv (cmd.exe re-parses argv on
    # the Windows shim path; file content is immune). The file is gone after
    # the call.
    sp_path = _flag(argv, "--system-prompt-file")
    assert fake.sp_contents[sp_path] == server._AI_DEFAULT_SYSTEM_PROMPT
    assert not Path(sp_path).exists()
    assert server._AI_DEFAULT_SYSTEM_PROMPT not in argv
    # Equals form as ONE token — a separate "" element is dropped by cmd.exe's
    # %* expansion behind a Windows .cmd shim, re-enabling tools/settings.
    assert "--tools=" in argv
    assert "--setting-sources=" in argv
    assert "" not in argv
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
    sp_path = _flag(argv, "--system-prompt-file")
    assert fake.sp_contents[sp_path] == "be terse"
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
                 {"prompt": "x", "model": ["claude-haiku-4-5-20251001"]},
                 # closed charset — argv is re-parsed by cmd.exe behind a
                 # Windows .cmd shim, so metacharacters are a 400, not a pass
                 {"prompt": "x", "model": "haiku&calc.exe"},
                 {"prompt": "x", "model": "haiku|whoami"},
                 {"prompt": "x", "model": "%TEMP%"},
                 {"prompt": "x", "model": 'haiku"'},
                 {"prompt": "x", "model": "haiku sonnet"}):
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
    fake = _cli_ok(monkeypatch, exc=asyncio.TimeoutError())
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    assert _data(resp)["error"]["type"] == "timeout"
    # The system-prompt temp file is cleaned up on the failure path too.
    (argv, _, _, _), = fake.calls
    assert not Path(_flag(argv, "--system-prompt-file")).exists()


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
    # then try the usual install dirs.
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bin_path = home / ".local" / "bin" / "claude"

    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(server.os.path, "expanduser",
                        lambda p: p.replace("~", str(home), 1))
    monkeypatch.setattr(server.os, "name", "posix")

    assert server._claude_bin() is None  # nothing installed anywhere
    bin_path.write_text("#!/bin/sh\n")
    assert server._claude_bin() == str(bin_path)


def test_posix_candidates_are_the_documented_install_locations():
    assert server._CLAUDE_POSIX_CANDIDATES == (
        "~/.local/bin/claude", "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude")


def test_windows_candidates_cover_the_windows_install_locations():
    """The bug this list fixes: it used to be the POSIX paths with Windows
    suffixes bolted on, which matches nothing a Windows install produces."""
    joined = "\n".join(server._CLAUDE_WINDOWS_CANDIDATES).lower()
    # native installer (irm https://claude.ai/install.ps1 | iex)
    assert r"%userprofile%\.local\bin\claude.exe" in joined
    assert "winget" in joined            # winget install Anthropic.ClaudeCode
    assert r"%appdata%\npm" in joined   # npm install -g @anthropic-ai/claude-code
    assert r"%userprofile%\.claude\local" in joined   # legacy local npm install
    # every entry is rooted in an environment variable, never a bare relative
    # path that would resolve against the server's cwd
    assert all(c.startswith("%") for c in server._CLAUDE_WINDOWS_CANDIDATES)
    # .exe ahead of any .cmd shim: a shim needs the cmd.exe hop
    exts = [c.lower().rsplit(".", 1)[1]
            for c in server._CLAUDE_WINDOWS_CANDIDATES]
    assert exts.index("exe") < exts.index("cmd")


def test_claude_bin_probes_the_windows_candidates_on_windows(monkeypatch,
                                                             tmp_path):
    """A Windows GUI launch inherits its login session's PATH, so an install
    that appended to the user PATH afterwards is invisible until sign-out."""
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(server.os, "name", "nt")
    installed = tmp_path / "npm" / "claude.cmd"
    installed.parent.mkdir()
    installed.write_text("@echo off\n")
    monkeypatch.setattr(server, "_CLAUDE_WINDOWS_CANDIDATES",
                        (str(tmp_path / "missing.exe"), str(installed)))
    assert server._claude_bin() == str(installed)
    # POSIX-only candidates are not consulted on nt, and vice versa
    monkeypatch.setattr(server.os, "name", "posix")
    monkeypatch.setattr(server, "_CLAUDE_POSIX_CANDIDATES", ())
    assert server._claude_bin() is None


def test_windows_candidates_expand_environment_variables(monkeypatch, tmp_path):
    """The %VAR% entries are literal until expanded — an unexpanded candidate
    would silently never match anything.

    os.path.expandvars only understands %VAR% on Windows (it is ntpath there),
    which is fine in production because the %VAR% list is only consulted when
    os.name == "nt" — but it means this host cannot expand them, so ntpath's
    version is substituted to stand in for the Windows interpreter."""
    import ntpath

    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(server.os, "name", "nt")
    monkeypatch.setattr(server.os.path, "expandvars", ntpath.expandvars)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm" / "claude.exe").write_text("")
    # a forward-slash candidate so the joined path is valid on this host too;
    # only the %VAR% expansion is under test
    monkeypatch.setattr(server, "_CLAUDE_WINDOWS_CANDIDATES",
                        ("%APPDATA%/npm/claude.exe",))
    assert server._claude_bin() == str(tmp_path / "npm" / "claude.exe")


def test_the_resolver_never_imports_the_chat_template():
    """The claude chat template resolves the CLI too, and this list looks much
    like its own. That duplication is the POINT: a template is standalone
    user-forkable code, and the only thing the server and a template share is
    the fused api. A test that pinned the two lists together (or an import)
    would recreate exactly the coupling that is not wanted."""
    import inspect
    src = inspect.getsource(server)
    # templates_api (the registry/listing router) is a normal server module and
    # is imported; a TEMPLATE's own backend must never be.
    assert "templates.claude" not in src
    assert "from fused_render.templates." not in src
    assert "import agent" not in src


def test_relay_uses_the_overridden_binary(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", "/opt/custom/claude")
    _relay({"prompt": "hello"})
    (argv, _, _, _), = fake.calls
    assert argv[0] == "/opt/custom/claude"


# -- the windows .cmd shim hop ----------------------------------------------------

def test_a_plain_binary_is_execed_as_an_argv_list(monkeypatch):
    # Only a .cmd/.bat shim needs the cmd.exe hop. A real .exe — and anything
    # at all off win32 — is exec'd directly, where argv needs no quoting rules.
    monkeypatch.setattr(server.sys, "platform", "win32")
    assert server._popen_cmd(r"C:\u\claude.exe", ["-p"]) == [
        r"C:\u\claude.exe", "-p"]
    monkeypatch.setattr(server.sys, "platform", "darwin")
    # a .cmd off win32 is not a shim, it is just a file with a funny name
    assert server._popen_cmd("/usr/local/bin/claude.cmd", ["-p"]) == [
        "/usr/local/bin/claude.cmd", "-p"]


def test_a_shim_becomes_one_fully_quoted_command_string(monkeypatch):
    """The paths-with-spaces bug. Handing the exec path the argv list
    ["cmd.exe", "/c", shim, ...] lets list2cmdline quote each element that
    needs it, and cmd.exe only preserves inner quoting when the rest of its
    line holds exactly two quote characters — a shim path with spaces plus any
    quoted argument makes four, cmd strips the outermost pair instead and
    re-splits at the spaces.

    So a shim becomes a STRING, spawned through the shell so CPython's comspec
    wrapping supplies the single outer quote pair cmd can parse."""
    monkeypatch.setattr(server.sys, "platform", "win32")
    shim = r"C:\Users\John Doe\AppData\Roaming\npm\claude.cmd"
    sp = r"C:\Users\John Doe\AppData\Local\Temp\ai_sp_x.txt"
    cmd = server._popen_cmd(shim, ["-p", "--system-prompt-file", sp])

    assert isinstance(cmd, str), (
        "a shim invocation must be a command string — an argv list would be "
        "re-joined by list2cmdline and mis-parsed by cmd.exe")
    # every element quoted, not just the ones with spaces: the outer pair stops
    # cmd re-parsing quotes, not metacharacters, and a quoted run is where
    # & | > < ^ stay literal
    assert cmd == f'"{shim}" "-p" "--system-prompt-file" "{sp}"'
    # what cmd.exe receives once CPython wraps it, and what it makes of that:
    # strip exactly the first and last quote, take the rest as written
    line = f'cmd.exe /c "{cmd}"'
    assert line[len("cmd.exe /c "):][1:-1] == cmd


def test_a_double_quote_in_an_argument_is_refused_not_smuggled(monkeypatch):
    """Nothing we send can contain a `"` — Windows paths cannot hold one and
    the rest is static or charset-validated. If that ever changes, fail loudly
    rather than emit a line that means something else."""
    monkeypatch.setattr(server.sys, "platform", "win32")
    with pytest.raises(ValueError):
        server._popen_cmd(r"C:\npm\claude.cmd", ["--model", 'a"b'])


def test_relay_runs_windows_shims_through_cmd(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    shim = r"C:\Users\John Doe\npm\claude.cmd"
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", shim)
    # The event loop must exist before sys.platform reads "win32", or
    # asyncio.run tries to build the real Windows proactor loop on this box.
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(server.sys, "platform", "win32")
        loop.run_until_complete(server._ai_relay({"prompt": "hello"}))
    finally:
        loop.close()
    (cmd, _, _, _), = fake.calls
    assert isinstance(cmd, str)
    assert cmd.startswith(f'"{shim}" ')
    # the temp system-prompt path is quoted too — it lives under a Windows
    # profile dir, which is exactly where the spaces come from
    sp = [t for t in cmd.split('"') if t.endswith(".txt")]
    assert sp and f'"{sp[0]}"' in cmd


def test_a_shim_is_spawned_through_the_shell_and_a_binary_is_not(monkeypatch):
    """The string form only parses correctly because CPython wraps it as
    `comspec /c "<payload>"` — so it must go to create_subprocess_shell, and a
    list must NOT."""
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self, input=None):
            return b"{}", b""

    async def fake_shell(cmd, **kw):
        seen["shell"] = cmd
        return _Proc()

    async def fake_exec(*argv, **kw):
        seen["exec"] = list(argv)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    payload = '"C:\\p ath\\claude.cmd" "-p"'
    asyncio.run(server._run_claude_cli(payload, dict(os.environ), 5))
    assert seen == {"shell": payload}

    seen.clear()
    asyncio.run(server._run_claude_cli(["/usr/local/bin/claude", "-p"],
                                       dict(os.environ), 5))
    assert seen == {"exec": ["/usr/local/bin/claude", "-p"]}


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
