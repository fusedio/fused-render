"""Tests for fused.ai (SPEC RH-11): the /api/ai endpoint backed by the claude
(Claude Code) CLI, the runtime surface that calls it, and the binary
resolution (FUSED_RENDER_CLAUDE_BIN / PATH).

The endpoint is driven through module-level `_ai_relay` with the subprocess
hop (`_spawn_claude_stream`) mocked (the "avoid starlette TestClient"
discipline of test_server_fs_write.py) — no test ever runs a real CLI. The
mock is a fake process with scripted stdout lines, since D166 drives the CLI
in stream-json mode (spawn first, prompt later over stdin) to make the warm
pool possible. The runtime checks are string-contract checks over the shipped
static/runtime.js, like test_runtime_cancellation.py.
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

    async def drain(self):
        pass


class _FakeStdout:
    def __init__(self, proc):
        self._proc = proc

    async def readline(self):
        if self._proc.hang:
            await asyncio.sleep(3600)
        if self._proc._lines:
            return self._proc._lines.pop(0).encode("utf-8") + b"\n"
        self._proc.returncode = self._proc.exit_code  # EOF: process is done
        return b""


class _FakeStderr:
    def __init__(self, proc):
        self._proc = proc

    async def read(self):
        return self._proc.stderr_bytes


class _FakeProc:
    """Stands in for the process _spawn_claude_stream returns: scripted
    stdout lines, captured stdin, and alive/dead simulation."""

    def __init__(self, lines=None, exit_code=0, stderr=b"", hang=False,
                 stdin_broken=False, returncode=None):
        self._lines = list(lines if lines is not None else _result_lines())
        self.exit_code = exit_code
        self.stderr_bytes = stderr
        self.hang = hang
        self.stdin_broken = stdin_broken
        self.returncode = returncode  # None = alive
        self.pid = 4242
        self.killed = False
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.stderr = _FakeStderr(self)

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    @property
    def message(self):
        """The stream-json user message written to stdin, parsed."""
        return json.loads(self.stdin.written)


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


def _cli_ok(monkeypatch, payload=None, prewarm=True, **kwargs):
    if payload is not None:
        kwargs["lines"] = _result_lines(payload)
    fake = _FakeSpawn(factory=lambda: _FakeProc(**kwargs)) \
        if "exc" not in kwargs else _FakeSpawn(exc=kwargs["exc"])
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    monkeypatch.setattr(server, "_AI_POOL", server._AiWarmPool())
    if not prewarm:
        monkeypatch.setattr(server._AI_POOL, "prewarm", lambda config: None)
    monkeypatch.setattr(server.shutil, "which",
                        lambda name: "/usr/local/bin/claude")
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    return fake


def _default_config(bin_path="/usr/local/bin/claude"):
    return (bin_path, server._AI_DEFAULT_MODEL,
            server._AI_DEFAULT_SYSTEM_PROMPT,
            server._AI_EFFORT_TOKENS["medium"])


def _seed_warm(proc, config):
    """Put a warm process in the pool as take() would find it."""
    server._AI_POOL._proc = proc
    server._AI_POOL._config = config


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
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 200
    data = _data(resp)
    assert data["ok"] is True
    assert data["result"]["text"] == "hi there"
    assert data["result"]["model"] == "claude-haiku-4-5-20251001"
    assert data["result"]["usage"] == {"input_tokens": 3, "output_tokens": 2}
    # One CLI spawn: stream-json in and out (the D166 warm-pool spawn shape;
    # --verbose is mandatory with stream-json output), the prompt over stdin
    # as a JSON user message (argv has an OS size cap), the default model,
    # the default system prompt, a bare one-shot (no tools, no settings, no
    # session), and the medium effort default carried as the
    # max-output-tokens env var.
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
    assert _flag(argv, "--max-turns") == "1"
    assert "hello" not in argv  # prompt travels over stdin, never argv
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    assert env["MAX_THINKING_TOKENS"] == "0"  # stream-json defaults it ON
    # The used process is never reused (context accumulates in-process).
    proc, = fake.procs
    assert proc.killed


def test_relay_writes_the_stream_json_user_message(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
    _relay({"prompt": "hello"})
    proc, = fake.procs
    assert proc.stdin.written.endswith(b"\n")
    assert proc.message == {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "hello"}]}}


def test_relay_options_reach_the_cli(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
    _relay({"prompt": "hello", "system_prompt": "be terse",
            "model": "claude-sonnet-5", "effort": "high"})
    (argv, env), = fake.calls
    assert _flag(argv, "--model") == "claude-sonnet-5"
    assert fake.system_prompts == ["be terse"]
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"  # effort: high
    assert env["MAX_THINKING_TOKENS"] == "0"


def test_relay_explicit_max_tokens_beats_effort(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
    _relay({"prompt": "hello", "effort": "low", "max_tokens": 99})
    (_, env), = fake.calls
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "99"
    assert env["MAX_THINKING_TOKENS"] == "0"


def test_relay_usage_is_normalized_to_the_two_token_keys(monkeypatch):
    # The schema guarantee (RH-11): usage is null or EXACTLY
    # {input_tokens, output_tokens} — extra CLI keys stripped, and a missing
    # or malformed block degrades to null rather than leaking through.
    _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)  # has cache_* extras
    usage = _data(_relay({"prompt": "x"}))["result"]["usage"]
    assert usage == {"input_tokens": 3, "output_tokens": 2}

    for bad in (None, "lots", [], {"input_tokens": 3},           # missing key
                {"input_tokens": "3", "output_tokens": 2},        # wrong type
                {"input_tokens": True, "output_tokens": 2}):      # bool
        _cli_ok(monkeypatch, {**_CLI_RESULT, "usage": bad}, prewarm=False)
        assert _data(_relay({"prompt": "x"}))["result"]["usage"] is None
    payload = dict(_CLI_RESULT)
    del payload["usage"]
    _cli_ok(monkeypatch, payload, prewarm=False)
    assert _data(_relay({"prompt": "x"}))["result"]["usage"] is None


def test_relay_model_echo_prefers_the_resolved_id(monkeypatch):
    # A model alias goes to the CLI as-is, but the response echoes the full id
    # the CLI actually ran (the modelUsage key).
    fake = _cli_ok(monkeypatch, dict(
        _CLI_RESULT, modelUsage={"claude-sonnet-5-20250929": {}}),
        prewarm=False)
    resp = _relay({"prompt": "hello", "model": "sonnet"})
    (argv, _), = fake.calls
    assert _flag(argv, "--model") == "sonnet"
    assert _data(resp)["result"]["model"] == "claude-sonnet-5-20250929"


def test_relay_skips_stream_events_when_not_streaming(monkeypatch):
    # Warm processes always run --include-partial-messages (one spawn shape
    # serves both modes); the extra stream_event lines are just skipped.
    _cli_ok(monkeypatch, prewarm=False,
            lines=_result_lines(deltas=["hi ", "there"]))
    data = _data(_relay({"prompt": "hello"}))
    assert data["ok"] is True
    assert data["result"]["text"] == "hi there"


# -- streaming ------------------------------------------------------------------


def test_relay_streams_ndjson_chunks_and_done(monkeypatch):
    _cli_ok(monkeypatch, prewarm=False,
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
    _cli_ok(monkeypatch, prewarm=False,
            lines=[thinking, _delta_line("hi"), json.dumps(_CLI_RESULT)])
    _, frames = _stream({"prompt": "x", "stream": True})
    assert [f for f in frames if f["type"] == "chunk"] == [
        {"type": "chunk", "text": "hi"}]


def test_relay_stream_error_is_a_done_frame(monkeypatch):
    # The process dies mid-stream: HTTP status is already 200, so the error
    # travels as the terminal ok:false done frame.
    _cli_ok(monkeypatch, prewarm=False,
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
    fake = _cli_ok(monkeypatch, prewarm=False)
    resp = _relay({"prompt": "", "stream": True})
    assert resp.status_code == 400
    assert _data(resp)["error"]["type"] == "bad_request"
    resp = _relay({"prompt": "x", "stream": "yes"})
    assert resp.status_code == 400
    assert fake.calls == []


# -- warm pool ------------------------------------------------------------------


def test_relay_uses_the_prewarmed_process(monkeypatch):
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc()
    _seed_warm(warm, _default_config())
    prewarms = []
    monkeypatch.setattr(server._AI_POOL, "prewarm", prewarms.append)
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert fake.calls == []  # no fresh spawn: the warm process served it
    assert warm.message["message"]["content"][0]["text"] == "hello"
    assert warm.killed  # used once, then reaped
    # A replacement prewarm was triggered for the config just used.
    assert prewarms == [_default_config()]


def test_relay_config_mismatch_spawns_fresh(monkeypatch):
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc()
    _seed_warm(warm, _default_config())
    resp = _relay({"prompt": "hello", "model": "claude-sonnet-5"})
    assert _data(resp)["ok"] is True
    assert len(fake.calls) == 1  # fresh spawn, warm process not usable
    assert warm.stdin.written == b""  # the mismatched process saw no prompt
    assert warm.killed  # ...and was reaped, not kept


def test_relay_dead_warm_process_spawns_fresh(monkeypatch):
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc(returncode=1)  # died while idle (auth expiry, crash)
    _seed_warm(warm, _default_config())
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert len(fake.calls) == 1
    assert warm.stdin.written == b""


def test_relay_warm_process_failing_midcall_retries_fresh_once(monkeypatch):
    # take()'s returncode check can't see every way an idle process went bad;
    # a warm process that fails before producing anything gets one fresh
    # retry, invisible to the caller.
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc(stdin_broken=True)
    _seed_warm(warm, _default_config())
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert len(fake.calls) == 1  # the retry spawn
    assert fake.procs[0].message["message"]["content"][0]["text"] == "hello"


def test_relay_nonstream_warm_failure_after_deltas_still_retries(monkeypatch):
    # --include-partial-messages means deltas flow even when nobody streams
    # them. A non-streaming call has no client that saw any text, so a warm
    # process dying AFTER emitting deltas must still get the fresh retry —
    # only text delivered to an actual onChunk reader blocks it.
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc(lines=[_delta_line("partial ")])  # deltas, then EOF
    _seed_warm(warm, _default_config())
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["ok"] is True
    assert _data(resp)["result"]["text"] == "hi there"
    assert len(fake.calls) == 1  # the retry spawn ran


def test_relay_stream_warm_failure_after_delivery_does_not_retry(monkeypatch):
    # Streaming is the case the retry guard exists for: the client already
    # rendered "partial ", so replaying the prompt would emit text twice.
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc(lines=[_delta_line("partial ")], exit_code=1,
                     stderr=b"died mid-answer")
    _seed_warm(warm, _default_config())
    _, frames = _stream({"prompt": "hello", "stream": True})
    assert frames[0] == {"type": "chunk", "text": "partial "}
    done = frames[-1]
    assert done["type"] == "done" and done["ok"] is False
    assert fake.calls == []  # no retry once text reached the client


def test_relay_concurrent_requests_do_not_share_the_warm_process(monkeypatch):
    fake = _cli_ok(monkeypatch, prewarm=False)
    warm = _FakeProc()
    _seed_warm(warm, _default_config())

    async def go():
        return await asyncio.gather(
            server._ai_relay({"prompt": "a"}),
            server._ai_relay({"prompt": "b"}))

    r1, r2 = asyncio.run(go())
    assert _data(r1)["ok"] is True and _data(r2)["ok"] is True
    # Exactly one of them got the warm process; the other spawned fresh.
    assert len(fake.calls) == 1
    prompts = {warm.message["message"]["content"][0]["text"],
               fake.procs[0].message["message"]["content"][0]["text"]}
    assert prompts == {"a", "b"}


def test_pool_prewarm_spawns_and_take_consumes(monkeypatch):
    fake = _FakeSpawn()
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    pool = server._AiWarmPool()
    config = _default_config()

    async def go():
        pool.prewarm(config)
        await asyncio.gather(*pool._spawn_tasks)
        assert pool._proc is fake.procs[0]
        # matching take() hands it out and empties the slot
        assert await pool.take(config) is fake.procs[0]
        assert await pool.take(config) is None
        await pool.shutdown()

    asyncio.run(go())
    (argv, env), = fake.calls
    assert _flag(argv, "--model") == server._AI_DEFAULT_MODEL
    assert fake.system_prompts == [server._AI_DEFAULT_SYSTEM_PROMPT]
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    assert env["MAX_THINKING_TOKENS"] == "0"


def test_pool_prewarm_default_skips_when_binary_missing(monkeypatch):
    # Startup must not error on a machine without Claude Code installed.
    fake = _FakeSpawn()
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(server.os.path, "isfile", lambda p: False)
    pool = server._AiWarmPool()
    pool.prewarm_default()
    assert pool._spawn_tasks == set()
    assert fake.calls == []


def test_pool_shutdown_reaps_the_warm_process(monkeypatch):
    pool = server._AiWarmPool()
    warm = _FakeProc()
    pool._proc, pool._config = warm, _default_config()
    asyncio.run(pool.shutdown())
    assert warm.killed
    assert pool._proc is None


def test_pool_shutdown_reaps_an_inflight_prewarm(monkeypatch):
    # shutdown() while a prewarm holds a LIVE process but hasn't filed it in
    # the slot yet (cancelled at the lock await): cancelling the task is not
    # enough — that process must be reaped, not orphaned.
    proc = _FakeProc()
    fake = _FakeSpawn(factory=lambda: proc)
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    pool = server._AiWarmPool()

    async def go():
        # hold the pool lock so _spawn completes its spawn and then parks at
        # `async with self._lock` — the exact window the leak lived in
        async with pool._lock:
            pool.prewarm(_default_config())
            while not fake.procs:  # spawn ran, task now blocked on the lock
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            for task in pool._spawn_tasks:
                task.cancel()
        await pool.shutdown()

    asyncio.run(go())
    assert proc.killed  # the in-flight prewarm's process did not leak
    assert pool._spawn_tasks == set()
    assert pool._proc is None


def test_pool_repeated_prewarms_leak_no_process(monkeypatch):
    # Back-to-back prewarms (every request fires one): the slot holds ONE
    # process and every displaced spawn reaps its own — nothing orphaned.
    fake = _FakeSpawn()
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    pool = server._AiWarmPool()

    async def go():
        for _ in range(3):
            pool.prewarm(_default_config())
        await asyncio.gather(*pool._spawn_tasks)
        await pool.shutdown()

    asyncio.run(go())
    assert len(fake.procs) == 3
    assert all(p.killed for p in fake.procs)  # slot winner reaped by shutdown


# -- bad requests ---------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {},                       # missing prompt
    {"prompt": ""},           # empty
    {"prompt": "   "},        # whitespace-only
    {"prompt": 42},           # wrong type
])
def test_relay_rejects_bad_prompt(monkeypatch, body):
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
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
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
    big = "x" * 200_000
    resp = _relay({"prompt": big})
    assert resp.status_code == 200
    (argv, _), = fake.calls
    assert all(len(a) < 1000 for a in argv)
    proc, = fake.procs
    assert proc.message["message"]["content"][0]["text"] == big


def test_relay_rejects_unknown_effort_and_bad_max_tokens(monkeypatch):
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
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
    fake = _FakeSpawn()
    monkeypatch.setattr(server, "_spawn_claude_stream", fake)
    monkeypatch.setattr(server, "_AI_POOL", server._AiWarmPool())
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
    _cli_ok(monkeypatch, prewarm=False, exc=OSError("exec format error"))
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    assert _data(resp)["error"]["type"] == "ai_unavailable"


def test_relay_nonzero_exit_is_ai_error(monkeypatch):
    _cli_ok(monkeypatch, prewarm=False, lines=[], exit_code=1,
            stderr=b"Invalid model name: nope")
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "Invalid model name" in data["error"]["message"]


def test_relay_timeout_is_timeout(monkeypatch):
    _cli_ok(monkeypatch, prewarm=False, hang=True)
    monkeypatch.setattr(server, "_AI_TIMEOUT_S", 0.05)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    assert _data(resp)["error"]["type"] == "timeout"


def test_relay_unparseable_stdout_is_ai_error(monkeypatch):
    # Non-JSON noise between events is tolerated; a stream that ENDS without
    # ever producing a result event is an error.
    _cli_ok(monkeypatch, prewarm=False, lines=["not json at all"])
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["error"]["type"] == "ai_error"


def test_relay_is_error_result_is_ai_error(monkeypatch):
    _cli_ok(monkeypatch, dict(
        _CLI_RESULT, is_error=True, subtype="error_during_execution",
        result="something broke"), prewarm=False)
    resp = _relay({"prompt": "hello"})
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "something broke" in data["error"]["message"]


def test_relay_stderr_warnings_are_ignored_on_success(monkeypatch):
    # A clean result with noise on stderr (connector notices etc.) is a
    # success — stderr is only consulted when the process dies.
    _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False,
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
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
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
    fake = _cli_ok(monkeypatch, _CLI_RESULT, prewarm=False)
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
