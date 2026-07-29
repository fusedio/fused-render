"""Tests for fused.ai (SPEC RH-11): the /api/ai endpoint backed by the claude
(Claude Code) CLI, the runtime surface that calls it, and the binary
resolution (FUSED_RENDER_CLAUDE_BIN / PATH).

The endpoint is driven through module-level `_ai_relay` with the subprocess
hop (`_spawn_claude_stream`) mocked (the "avoid starlette TestClient"
discipline of test_server_fs_write.py) — no test ever runs a real CLI. The
mock is a fake process that SPEAKS the stdin reconfiguration protocol D167
drives the persistent instance with: it answers /clear with a
conversation_reset + local result, control_requests with control_responses,
and a user turn with its scripted delta/result lines. The runtime checks are
string-contract checks over the shipped static/runtime.js, like
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


# The real terminal `result` event of `claude -p ... --output-format
# stream-json` (2.1.220), trimmed to the fields the server reads plus a few
# it must ignore. Identical schema to the old --output-format json blob.
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


def _delta_line(text):
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text}}})


def _result_lines(payload=None, deltas=()):
    """Scripted stdout for one turn: stream_event deltas, then the result."""
    lines = [_delta_line(t) for t in deltas]
    lines.append(json.dumps(payload if payload is not None else _CLI_RESULT))
    return lines


class _FakeStdin:
    def __init__(self, proc):
        self._proc = proc
        self.written = b""

    def write(self, data):
        if self._proc.stdin_broken:
            raise OSError("broken pipe")
        self.written += data
        self._proc._on_stdin(data)

    async def drain(self):
        pass


class _FakeStdout:
    def __init__(self, proc):
        self._proc = proc

    async def readline(self):
        if self._proc._out:
            return self._proc._out.pop(0).encode("utf-8") + b"\n"
        if self._proc.hang:  # alive but silent (reconfig answered; turn not)
            await asyncio.sleep(3600)
        self._proc.returncode = self._proc.exit_code  # EOF: process is done
        return b""


class _FakeStderr:
    def __init__(self, proc):
        self._proc = proc

    async def read(self):
        return self._proc.stderr_bytes


class _FakeProc:
    """Stands in for the persistent process _spawn_claude_stream returns,
    speaking the D167 stdin protocol: /clear -> conversation_reset + local
    result; control_request -> control_response; a user turn -> the next
    scripted `lines` batch (deltas + result). Also simulates dead/hung/
    broken-pipe states and records everything written for assertions."""

    def __init__(self, lines=None, exit_code=0, stderr=b"", hang=False,
                 stdin_broken=False, returncode=None, control_error=None,
                 clear_silent=False, turns=None):
        # `turns`: list of line-batches, one per user turn (a persistent
        # process answers many). `lines` is the single-turn shorthand.
        if turns is None:
            turns = [list(lines) if lines is not None else _result_lines()]
        self._turns = [list(t) for t in turns]
        self._out = []               # lines queued for stdout
        self.exit_code = exit_code
        self.stderr_bytes = stderr
        self.hang = hang
        self.stdin_broken = stdin_broken
        self.returncode = returncode  # None = alive
        self.control_error = control_error  # subtype -> error message
        self.clear_silent = clear_silent    # /clear never answers (wedged)
        self.pid = 4242
        self.killed = False
        self.writes = []             # every parsed stdin JSON, in order
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.stderr = _FakeStderr(self)

    def _on_stdin(self, data):
        for raw in data.decode("utf-8").splitlines():
            if not raw.strip():
                continue
            msg = json.loads(raw)
            self.writes.append(msg)
            self._respond(msg)

    def _respond(self, msg):
        if msg.get("type") == "user":
            content = msg.get("message", {}).get("content")
            if content == "/clear":
                if self.clear_silent:
                    return
                self._out.append(json.dumps(
                    {"type": "conversation_reset"}))
                self._out.append(json.dumps({
                    "type": "result", "subtype": "success",
                    "is_error": False, "num_turns": 0, "result": ""}))
                return
            # a real user turn: play the next scripted batch (a hung process
            # answers reconfiguration but never the turn)
            if self._turns and not self.hang:
                self._out.extend(self._turns.pop(0))
            return
        if msg.get("type") == "control_request":
            subtype = msg.get("request", {}).get("subtype")
            rid = msg.get("request_id")
            err = (self.control_error or {}).get(subtype)
            if err:
                self._out.append(json.dumps({
                    "type": "control_response", "response": {
                        "subtype": "error", "request_id": rid,
                        "error": err}}))
            else:
                self._out.append(json.dumps({
                    "type": "control_response", "response": {
                        "subtype": "success", "request_id": rid}}))

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    # -- assertion helpers ---------------------------------------------------

    @property
    def user_turns(self):
        """Real user messages (excluding /clear), in write order."""
        return [w for w in self.writes
                if w.get("type") == "user"
                and w.get("message", {}).get("content") != "/clear"]

    @property
    def clears(self):
        return [w for w in self.writes
                if w.get("type") == "user"
                and w.get("message", {}).get("content") == "/clear"]

    @property
    def controls(self):
        """control_request payloads, in write order."""
        return [w["request"] for w in self.writes
                if w.get("type") == "control_request"]

    @property
    def message(self):
        """The single real user message written, parsed."""
        turn, = self.user_turns
        return turn

    @property
    def prompt(self):
        return self.message["message"]["content"][0]["text"]


class _FakeSpawn:
    """Stands in for server._spawn_claude_stream: hands out _FakeProcs (one
    per spawn, from `factory`), or raises. `cmd` is an argv list, or one
    command string on the Windows .cmd-shim path (matching _popen_cmd)."""

    def __init__(self, factory=_FakeProc, exc=None):
        self._factory = factory
        self._exc = exc
        self.calls = []  # (cmd, env) of every spawn
        self.procs = []  # every proc handed out
        self.system_prompts = []  # --system-prompt-file content per spawn

    async def __call__(self, cmd, env):
        self.calls.append((cmd, env))
        if isinstance(cmd, list) and "--system-prompt-file" in cmd:
            # capture now: the temp file is unlinked when the proc is reaped
            path = cmd[cmd.index("--system-prompt-file") + 1]
            self.system_prompts.append(
                Path(path).read_text(encoding="utf-8"))
        if self._exc is not None:
            raise self._exc
        proc = self._factory()
        self.procs.append(proc)
        return proc


def _cli_ok(monkeypatch, payload=None, **kwargs):
    if payload is not None:
        kwargs["lines"] = _result_lines(payload)
    fake = _FakeSpawn(factory=lambda: _FakeProc(**kwargs)) \
        if "exc" not in kwargs else _FakeSpawn(exc=kwargs["exc"])
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    monkeypatch.setattr(server, "_AI_SESSION", server._AiSession())
    monkeypatch.setattr(server.shutil, "which",
                        lambda name: "/usr/local/bin/claude")
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    return fake


def _seed_session(proc, model=None, system_prompt=None):
    """Put a live process in the session as a previous request left it."""
    server._AI_SESSION._proc = proc
    server._AI_SESSION._model = model or server._AI_DEFAULT_MODEL
    server._AI_SESSION._system_prompt = (
        system_prompt or server._AI_DEFAULT_SYSTEM_PROMPT)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def _stream(body):
    """Drive a streaming _relay to completion; return (resp, ndjson frames)."""
    async def go():
        resp = await server._ai_relay(body)
        frames = []
        async for chunk in resp.body_iterator:
            frames.extend(json.loads(l) for l in chunk.splitlines() if l)
        return resp, frames
    return asyncio.run(go())


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
    # One CLI spawn: stream-json in and out (the D167 persistent-instance
    # spawn shape; --verbose is mandatory with stream-json output), the
    # prompt over stdin as a JSON user message (argv has an OS size cap),
    # the default model, the default system prompt, a bare completion engine
    # (no tools, no settings, no session persistence) — and NO --max-turns:
    # the instance is multi-turn, isolation comes from /clear.
    (argv, env), = fake.calls
    assert argv[0] == "/usr/local/bin/claude"
    assert "-p" in argv
    assert _flag(argv, "--input-format") == "stream-json"
    assert _flag(argv, "--output-format") == "stream-json"
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv
    assert _flag(argv, "--model") == server._AI_DEFAULT_MODEL
    # No user text in argv: the system prompt travels via a temp file
    # (cmd.exe on the shim path re-parses argv; see _ai_cmd).
    assert fake.system_prompts == [server._AI_DEFAULT_SYSTEM_PROMPT]
    # Equals form as ONE token — a separate "" element is dropped by cmd.exe's
    # %* expansion behind a Windows .cmd shim, re-enabling tools/settings.
    assert "--tools=" in argv
    assert "--setting-sources=" in argv
    assert "" not in argv
    assert "--no-session-persistence" in argv
    assert "--max-turns" not in argv
    assert "hello" not in argv  # prompt travels over stdin, never argv
    # Effort/thinking are Claude Code's own semantics now — the env-var
    # overrides are gone (their values fought the effortLevel flag).
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    assert "MAX_THINKING_TOKENS" not in env
    # The instance survives the call (persistent, not use-once)...
    proc, = fake.procs
    assert not proc.killed
    assert server._AI_SESSION._proc is proc
    # ...and the request was preceded by /clear (context isolation), an
    # unconditional set_model (every request specifies its own config), and
    # the thinking clamp: absent effort means low/no-thinking, enforced with
    # set_max_thinking_tokens 0 (works on every model — haiku ignores
    # effortLevel and otherwise thinks by default in stream-json mode).
    # No apply_flag_settings: the clamp alone is the "low" semantics.
    assert len(proc.clears) == 1
    assert [c["subtype"] for c in proc.controls] == [
        "set_model", "set_max_thinking_tokens"]
    assert proc.controls[1]["max_thinking_tokens"] == 0


def test_relay_writes_the_stream_json_user_message(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    _relay({"prompt": "hello"})
    proc, = fake.procs
    assert proc.stdin.written.endswith(b"\n")
    assert proc.message == {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "hello"}]}}


def test_relay_options_become_reconfiguration_requests(monkeypatch):
    # The request's model/system_prompt is applied to the RUNNING instance
    # via set_model (both fields, one request) instead of a respawn. Real
    # effort (medium+) first RESETS the thinking budget — a previous low
    # request's clamp survives /clear, so null is mandatory — then applies
    # effortLevel. Order: /clear, set_model, budget, effort, user turn.
    fake = _cli_ok(monkeypatch)
    proc = _FakeProc(turns=[_result_lines(), _result_lines()])
    _seed_session(proc)  # live instance
    _relay({"prompt": "hello", "system_prompt": "be terse",
            "model": "claude-sonnet-5", "effort": "high"})
    assert fake.calls == []  # reconfigured, not respawned

    def kind(w):
        if w.get("type") == "control_request":
            return w["request"]["subtype"]
        if w.get("message", {}).get("content") == "/clear":
            return "clear"
        return "turn"

    assert [kind(w) for w in proc.writes] == [
        "clear", "set_model", "set_max_thinking_tokens",
        "apply_flag_settings", "turn"]
    set_model, budget, effort = proc.controls
    assert set_model == {"subtype": "set_model", "model": "claude-sonnet-5",
                         "system_prompt": "be terse"}
    assert budget == {"subtype": "set_max_thinking_tokens",
                      "max_thinking_tokens": None}  # reset, not clamp
    assert effort == {"subtype": "apply_flag_settings",
                      "settings": {"effortLevel": "high"}}


def test_relay_set_model_is_sent_on_every_request(monkeypatch):
    # No config tracking: every request fully specifies its own config, so
    # set_model rides EVERY call — a repeat with identical options included.
    # (~0ms per probe; simpler invariant than skip-when-unchanged state.)
    fake = _cli_ok(monkeypatch, turns=[_result_lines(), _result_lines()])
    _relay({"prompt": "one"})
    _relay({"prompt": "two"})
    proc, = fake.procs
    assert len(proc.clears) == 2
    set_models = [c for c in proc.controls if c["subtype"] == "set_model"]
    assert len(set_models) == 2
    assert all(c == {"subtype": "set_model",
                     "model": server._AI_DEFAULT_MODEL,
                     "system_prompt": server._AI_DEFAULT_SYSTEM_PROMPT}
               for c in set_models)


def test_relay_effort_maps_to_thinking_budget_and_flag(monkeypatch):
    # The per-request thinking/effort protocol (all probed on 2.1.220):
    # absent or "low" -> clamp the thinking budget to 0 (the universal
    # switch — haiku ignores effortLevel and thinks by default otherwise),
    # no effortLevel; medium+ -> budget null (mandatory: a previous low
    # request's clamp SURVIVES /clear) then effortLevel.
    fake = _cli_ok(monkeypatch, turns=[_result_lines(), _result_lines(),
                                       _result_lines(), _result_lines()])
    proc_controls = lambda: fake.procs[0].controls

    def last_call(subtype):
        return [c for c in proc_controls() if c["subtype"] == subtype]

    _relay({"prompt": "a", "effort": "low"})
    assert last_call("set_max_thinking_tokens")[-1] == {
        "subtype": "set_max_thinking_tokens", "max_thinking_tokens": 0}
    assert last_call("apply_flag_settings") == []  # clamp IS the low path

    _relay({"prompt": "b"})  # absent effort == low: clamp again, no flag
    assert last_call("set_max_thinking_tokens")[-1][
        "max_thinking_tokens"] == 0
    assert last_call("apply_flag_settings") == []

    _relay({"prompt": "c", "effort": "xhigh"})  # xhigh is a valid level
    assert last_call("set_max_thinking_tokens")[-1][
        "max_thinking_tokens"] is None  # un-clamp before effortLevel
    assert last_call("apply_flag_settings")[-1]["settings"] == {
        "effortLevel": "xhigh"}

    _relay({"prompt": "d", "effort": "medium"})
    assert last_call("apply_flag_settings")[-1]["settings"] == {
        "effortLevel": "medium"}


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
    (argv, _), = fake.calls
    assert _flag(argv, "--model") == "sonnet"
    assert _data(resp)["result"]["model"] == "claude-sonnet-5-20250929"


def test_relay_skips_stream_events_when_not_streaming(monkeypatch):
    # Warm processes always run --include-partial-messages (one spawn shape
    # serves both modes); the extra stream_event lines are just skipped.
    _cli_ok(monkeypatch,
            lines=_result_lines(deltas=["hi ", "there"]))
    data = _data(_relay({"prompt": "hello"}))
    assert data["ok"] is True
    assert data["result"]["text"] == "hi there"


# -- streaming ------------------------------------------------------------------


def test_relay_streams_ndjson_chunks_and_done(monkeypatch):
    _cli_ok(monkeypatch,
            lines=_result_lines(deltas=["hi ", "there"]))
    resp, frames = _stream({"prompt": "hello", "stream": True})
    assert resp.status_code == 200
    assert resp.media_type == "text/x-ndjson"
    assert frames[:2] == [{"type": "chunk", "text": "hi "},
                          {"type": "chunk", "text": "there"}]
    done = frames[-1]
    assert done["type"] == "done" and done["ok"] is True
    # Same result schema as the non-streaming response.
    assert done["result"] == {
        "text": "hi there", "model": "claude-haiku-4-5-20251001",
        "usage": {"input_tokens": 3, "output_tokens": 2}}


def test_relay_stream_skips_thinking_deltas(monkeypatch):
    thinking = json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "thinking": "hmm"}}})
    _cli_ok(monkeypatch,
            lines=[thinking, _delta_line("hi"), json.dumps(_CLI_RESULT)])
    _, frames = _stream({"prompt": "x", "stream": True})
    assert [f for f in frames if f["type"] == "chunk"] == [
        {"type": "chunk", "text": "hi"}]


def test_relay_stream_error_is_a_done_frame(monkeypatch):
    # The process dies mid-stream: HTTP status is already 200, so the error
    # travels as the terminal ok:false done frame.
    _cli_ok(monkeypatch,
            lines=[_delta_line("hi")],  # then EOF, no result event
            exit_code=1, stderr=b"something broke")
    resp, frames = _stream({"prompt": "x", "stream": True})
    assert resp.status_code == 200
    assert frames[0] == {"type": "chunk", "text": "hi"}
    done = frames[-1]
    assert done["type"] == "done" and done["ok"] is False
    assert done["error"]["type"] == "ai_error"
    assert "something broke" in done["error"]["message"]


def test_relay_stream_validation_errors_are_plain_json(monkeypatch):
    # Validation happens before any streaming starts, so a bad body is the
    # ordinary 400 JSON even with stream requested.
    fake = _cli_ok(monkeypatch)
    resp = _relay({"prompt": "", "stream": True})
    assert resp.status_code == 400
    assert _data(resp)["error"]["type"] == "bad_request"
    resp = _relay({"prompt": "x", "stream": "yes"})
    assert resp.status_code == 400
    assert fake.calls == []


# -- the persistent instance ------------------------------------------------------


def test_relay_reuses_the_live_instance(monkeypatch):
    # A second request rides the SAME process: no new spawn, one more /clear.
    fake = _cli_ok(monkeypatch, turns=[_result_lines(), _result_lines()])
    _relay({"prompt": "one"})
    _relay({"prompt": "two"})
    assert len(fake.calls) == 1  # one spawn served both
    proc, = fake.procs
    assert [t["message"]["content"][0]["text"] for t in proc.user_turns] \
        == ["one", "two"]
    assert len(proc.clears) == 2  # /clear before EVERY turn, no exceptions
    assert not proc.killed


def test_relay_uses_the_startup_prewarmed_instance(monkeypatch):
    # prewarm_default() spawns ahead of the first request; that request
    # then pays no spawn.
    fake = _cli_ok(monkeypatch)
    asyncio.run(_prewarmed_relay(fake, {"prompt": "hello"}))


async def _prewarmed_relay(fake, body):
    server._AI_SESSION.prewarm_default()
    await server._AI_SESSION._spawn_task
    assert len(fake.calls) == 1
    resp = await server._ai_relay(body)
    assert json.loads(bytes(resp.body))["ok"] is True
    assert len(fake.calls) == 1  # served by the prewarmed instance
    proc, = fake.procs
    assert proc.prompt == "hello"


def test_relay_dead_instance_respawns(monkeypatch):
    fake = _cli_ok(monkeypatch)
    dead = _FakeProc(returncode=1)  # died while idle (auth expiry, crash)
    _seed_session(dead)
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert len(fake.calls) == 1  # the respawn
    assert dead.writes == []     # the corpse saw no prompt
    assert fake.procs[0].prompt == "hello"


def test_relay_instance_failing_midcall_retries_fresh_once(monkeypatch):
    # returncode can't see every way an idle instance went bad; a write
    # failure before anything was delivered gets one fresh-spawn retry,
    # invisible to the caller.
    fake = _cli_ok(monkeypatch)
    wedged = _FakeProc(stdin_broken=True)
    _seed_session(wedged)
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert len(fake.calls) == 1  # the retry spawn
    assert fake.procs[0].prompt == "hello"
    assert wedged.killed  # the wedged instance was discarded, not kept


def test_relay_wedged_clear_kills_and_respawns(monkeypatch):
    # A /clear that never settles within the control timeout means a wedged
    # process: kill, respawn, retry the request.
    fake = _cli_ok(monkeypatch)
    wedged = _FakeProc(clear_silent=True, hang=True)
    _seed_session(wedged)
    monkeypatch.setattr(server, "_AI_CTRL_TIMEOUT_S", 0.05)
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert wedged.killed
    assert len(fake.calls) == 1
    assert fake.procs[0].prompt == "hello"


def test_relay_control_error_respawns_with_argv_config(monkeypatch):
    # A control error (e.g. a CLI that rejects the probed set_model
    # system_prompt field) discards the instance and retries once on a
    # fresh spawn whose ARGV carries the requested config.
    fake = _cli_ok(monkeypatch)
    live = _FakeProc(control_error={
        "set_model": "unexpected field: system_prompt"})
    _seed_session(live)
    resp = _relay({"prompt": "hello", "system_prompt": "be terse",
                   "model": "claude-sonnet-5"})
    assert _data(resp)["ok"] is True
    assert live.killed  # the control error discarded the instance
    assert len(fake.calls) == 1  # the config-carrying respawn
    (argv2, _), = fake.calls
    assert _flag(argv2, "--model") == "claude-sonnet-5"
    assert fake.system_prompts == ["be terse"]
    # set_model still rides the retry (unconditional per request), followed
    # by the thinking clamp (no effort given = low); here the fresh instance
    # accepts them and the turn proceeds.
    retry, = fake.procs
    assert [c["subtype"] for c in retry.controls] == [
        "set_model", "set_max_thinking_tokens"]
    assert retry.prompt == "hello"


def test_relay_thinking_clamp_error_takes_the_respawn_path(monkeypatch):
    # A rejected set_max_thinking_tokens goes down the same discard +
    # respawn-once path as any other control error.
    fake = _cli_ok(monkeypatch)
    live = _FakeProc(control_error={
        "set_max_thinking_tokens": "unknown control subtype"})
    _seed_session(live)
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert live.killed
    assert len(fake.calls) == 1  # one respawn, then success
    assert fake.procs[0].prompt == "hello"


def test_relay_persistent_control_rejection_is_an_ai_error(monkeypatch):
    # If EVERY instance rejects set_model (a future CLI dropping the field
    # for good), the one retry also fails and the caller gets a clean
    # ai_error naming the rejection — not a hang, not a crashloop.
    fake = _cli_ok(monkeypatch, control_error={
        "set_model": "unexpected field: system_prompt"})
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "set_model" in data["error"]["message"]
    assert len(fake.calls) == 2  # initial spawn + the single retry, no loop


def test_relay_nonstream_failure_after_deltas_still_retries(monkeypatch):
    # --include-partial-messages means deltas flow even when nobody streams
    # them. A non-streaming call has no client that saw any text, so an
    # instance dying AFTER emitting deltas must still get the fresh retry —
    # only text delivered to an actual onChunk reader blocks it.
    fake = _cli_ok(monkeypatch)
    dying = _FakeProc(lines=[_delta_line("partial ")])  # deltas, then EOF
    _seed_session(dying)
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert _data(resp)["result"]["text"] == "hi there"
    assert len(fake.calls) == 1  # the retry spawn ran


def test_relay_stream_failure_after_delivery_does_not_retry(monkeypatch):
    # Streaming is the case the retry guard exists for: the client already
    # rendered "partial ", so replaying the prompt would emit text twice.
    fake = _cli_ok(monkeypatch)
    dying = _FakeProc(lines=[_delta_line("partial ")], exit_code=1,
                      stderr=b"died mid-answer")
    _seed_session(dying)
    _, frames = _stream({"prompt": "hello", "stream": True})
    assert frames[0] == {"type": "chunk", "text": "partial "}
    done = frames[-1]
    assert done["type"] == "done" and done["ok"] is False
    assert fake.calls == []  # no retry once text reached the client
    # the dead instance was discarded: the session holds nothing
    assert server._AI_SESSION._proc is None


def test_relay_concurrent_requests_serialize_through_the_instance(monkeypatch):
    # ONE process, requests queued behind the session lock: no interleaved
    # writes, both answered, still no second spawn. Accepted tradeoff of the
    # single-instance design (local single-user app).
    fake = _cli_ok(monkeypatch, turns=[_result_lines(), _result_lines()])

    async def go():
        return await asyncio.gather(
            server._ai_relay({"prompt": "a"}),
            server._ai_relay({"prompt": "b"}))

    r1, r2 = asyncio.run(go())
    assert _data(r1)["ok"] is True and _data(r2)["ok"] is True
    assert len(fake.calls) == 1
    proc, = fake.procs
    # strict per-request ordering: clear, turn, clear, turn — a second
    # request never writes before the first one's result was read
    seq = [("clear" if w.get("message", {}).get("content") == "/clear"
            else "turn") for w in proc.writes if w.get("type") == "user"]
    assert seq == ["clear", "turn", "clear", "turn"]
    assert {t["message"]["content"][0]["text"]
            for t in proc.user_turns} == {"a", "b"}


def test_session_prewarm_default_skips_when_binary_missing(monkeypatch):
    # Startup must not error on a machine without Claude Code installed.
    fake = _FakeSpawn()
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(server.os.path, "isfile", lambda p: False)
    session = server._AiSession()

    async def go():
        session.prewarm_default()
        await session._spawn_task

    asyncio.run(go())
    assert session._proc is None
    assert fake.calls == []


def test_session_shutdown_reaps_the_instance(monkeypatch):
    session = server._AiSession()
    proc = _FakeProc()
    session._proc = proc
    asyncio.run(session.shutdown())
    assert proc.killed
    assert session._proc is None


def test_session_shutdown_awaits_an_inflight_prewarm(monkeypatch):
    # shutdown() during the startup prewarm: the spawn task is cancelled AND
    # awaited, and whatever process it filed is reaped — nothing orphaned.
    proc = _FakeProc()

    async def slow_spawn(cmd, env):
        await asyncio.sleep(0)
        return proc

    monkeypatch.setattr(server, "_spawn_claude_stream", slow_spawn)
    monkeypatch.setattr(server.shutil, "which",
                        lambda name: "/usr/local/bin/claude")
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    session = server._AiSession()

    async def go():
        session.prewarm_default()
        await session.shutdown()

    asyncio.run(go())
    assert session._proc is None
    assert session._spawn_task.done()


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
    (argv, _), = fake.calls
    assert all(len(a) < 1000 for a in argv)
    proc, = fake.procs
    assert proc.message["message"]["content"][0]["text"] == big


def test_relay_rejects_unknown_effort_and_bad_model(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT)
    for body in ({"prompt": "x", "effort": "extreme"},
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
    fake = _FakeSpawn()
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    monkeypatch.setattr(server, "_AI_SESSION", server._AiSession())
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


def test_relay_spawn_oserror_is_ai_unavailable(monkeypatch):
    _cli_ok(monkeypatch, exc=OSError("exec format error"))
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    assert _data(resp)["error"]["type"] == "ai_unavailable"


def test_relay_nonzero_exit_is_ai_error(monkeypatch):
    _cli_ok(monkeypatch, lines=[], exit_code=1,
            stderr=b"Invalid model name: nope")
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "Invalid model name" in data["error"]["message"]


def test_relay_timeout_is_timeout(monkeypatch):
    _cli_ok(monkeypatch, hang=True)
    monkeypatch.setattr(server, "_AI_TIMEOUT_S", 0.05)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    assert _data(resp)["error"]["type"] == "timeout"


def test_relay_unparseable_stdout_is_ai_error(monkeypatch):
    # Non-JSON noise between events is tolerated; a stream that ENDS without
    # ever producing a result event is an error.
    _cli_ok(monkeypatch, lines=["not json at all"])
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
    # A clean result with noise on stderr (connector notices etc.) is a
    # success — stderr is only consulted when the process dies.
    _cli_ok(monkeypatch, _CLI_RESULT,
            stderr=b"Warning: some connector notice")
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
    (argv, _), = fake.calls
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
    # the reap path goes through taskkill on "win32" — neutralize it here
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(server.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                        raising=False)
    # The event loop must exist before sys.platform reads "win32", or
    # asyncio.run tries to build the real Windows proactor loop on this box.
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(server.sys, "platform", "win32")
    try:
        loop.run_until_complete(server._ai_relay({"prompt": "hello"}))
    finally:
        loop.close()
    (cmd, _), = fake.calls
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

    async def fake_shell(cmd, **kw):
        seen["shell"] = cmd
        return _Proc()

    async def fake_exec(*argv, **kw):
        seen["exec"] = list(argv)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    payload = '"C:\\p ath\\claude.cmd" "-p"'
    asyncio.run(server._spawn_claude_stream(payload, dict(os.environ)))
    assert seen == {"shell": payload}

    seen.clear()
    asyncio.run(server._spawn_claude_stream(["/usr/local/bin/claude", "-p"],
                                            dict(os.environ)))
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


def test_runtime_ai_streams_on_onchunk():
    # opts.onChunk switches the request to stream:true and reads the NDJSON
    # body incrementally; partial lines are buffered across read() boundaries.
    assert "opts.onChunk" in RUNTIME
    assert "body.stream = true" in RUNTIME
    assert "getReader()" in RUNTIME
    assert 'buffer.split("\\n")' in RUNTIME


# -- export stance -----------------------------------------------------------------


def test_export_rejects_ai(tmp_path):
    html = "<script>fused.ai('summarize this');</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("fused.ai() is not supported on a hosted page" in e
               for e in plan.errors)
