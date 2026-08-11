import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from fastapi import APIRouter, Body, Header
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from fused_render.server.common import _require_fused
from fused_render.shell.prefs import default_model

router = APIRouter()



# --- /api/ai — inference through the Claude Code CLI --------------------------
#
# fused.ai(prompt, opts) lands here. The shell invokes the `claude` binary the
# user already has (Claude Code — its login is the credential) rather than
# pages fetching a model directly: the page stays origin-clean (no API key or
# endpoint baked into authored HTML), and the server is one place to grow
# config/limits later. Wire shape is the house {ok, result,
# error:{type,message}} contract /api/run set; {"stream": true} switches the
# response to NDJSON chunks (see _ai_relay).
#
# The CLI is driven as a bare completion engine: --tools= disables every
# built-in tool, --setting-sources= skips user/project settings and
# CLAUDE.md, --system-prompt-file REPLACES the shipped agent prompt, and
# --no-session-persistence keeps everything off disk.
#
# LATENCY (D168/D169): the CLI is a Node program whose startup alone costs
# ~1.5-2.5s — it dominated every call at haiku sizes. ONE persistent process
# is therefore kept alive in --input-format stream-json mode and RECONFIGURED
# per request over its stdin protocol (all probed on 2.1.220):
#   /clear (a plain user message)        -> wipes conversation context, ~0.7s
#   set_model control_request            -> swaps model AND system_prompt, ~0ms
#   set_max_thinking_tokens ctrl_request -> 0 clamps thinking on ANY model,
#                                           null resets to session default
#   apply_flag_settings control_request  -> sets effortLevel, ~10ms
# Every call therefore sees an empty context (the /clear is what preserves
# D159's isolation property), and only the first call after a crash or server
# start pays a spawn. Requests are SERIALIZED through the one process (a
# local single-user app; calls are seconds) rather than pooled.

# `effort` medium/high/xhigh passes through to Claude Code's own effort
# semantics (the same code path as the interactive /effort command) — only
# effort-capable models (sonnet/opus class) honor effortLevel. Absent or
# "low" means NO THINKING, enforced with the thinking-budget clamp, which
# works on every model including haiku (the default, which otherwise thinks
# by default in stream-json mode). See _AiSession.configure.
_AI_EFFORTS = ("low", "medium", "high", "xhigh")
_AI_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# The default-model PREFERENCE (shell/prefs.py) speaks short names, because its
# other consumer — the claude chat template's model chip — does: one preference
# cannot have two vocabularies. This relay hands its value to the CLI, which is
# happier with a full id, so the short→id mapping lives here, at the one call
# site that needs it, rather than in the pref (which would make the store know
# about a model catalogue it has no other reason to track).
#
# Ids are unsuffixed aliases, not dated snapshots: a dated id pins a model
# version the user did not choose, and "opus" in the picker meaning last
# quarter's opus is a worse surprise than the alias moving. The hardcoded
# `_AI_DEFAULT_MODEL` above keeps its date because it is a DIFFERENT promise —
# the cheap, fast model this relay has always used for its small utility
# completions when nobody has expressed a preference at all.
#
# Keys must cover VALID_DEFAULT_MODELS minus its "" (unset) member; the values
# are argv, so they live inside `_AI_MODEL_RE`'s charset boundary. Both are
# asserted in tests/test_server_ai.py.
_AI_SHORT_MODEL_IDS = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}
_AI_DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
_AI_TIMEOUT_S = 600.0
# A reconfiguration step (/clear, set_model, effort) is local work; one that
# takes longer than this means a wedged process — kill and respawn.
_AI_CTRL_TIMEOUT_S = 10.0
_AI_BIN_ENV = "FUSED_RENDER_CLAUDE_BIN"
# Model ids/aliases are a closed charset. This is a SECURITY boundary, not
# just validation: on the Windows .cmd-shim path argv is re-parsed by cmd.exe
# (whose quoting cannot be escaped reliably), so every argv element must be a
# static literal, a tempdir path, or a value this regex admitted.
# A cloud alias ("opus") or a Hugging Face repo id ("mlx-community/Qwen3-8B-4bit").
# The slash is the SEAM (SPEC §40): a model id with an org in it names a repo on
# disk, which means local inference, and one without it is a Claude alias. That
# is not a heuristic — a Hub id always has the form `org/name`, and no Claude
# alias ever contains a slash — and it is what lets `fused.ai(prompt, {model})`
# reach a local model with no new parameter and no change to any existing caller.
_AI_MODEL_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _is_local_model(model: str) -> bool:
    return "/" in model


def _ai_error(type_: str, message: str, status: int = 502) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"type": type_, "message": message}},
        status_code=status,
    )


# Where Claude Code installs `claude`, for when it isn't on the PATH this
# process inherited — the packaged app's PATH is the supervisor's, not a
# shell's, and a Finder/Dock-launched .app misses ~/.local/bin and Homebrew.
# On Windows it is worse: a GUI launch inherits the PATH of its login session,
# so an install that appended to the *user* PATH afterwards stays invisible
# until the next sign-in.
#
# The claude chat template (templates/claude/agent.py) resolves the CLI the
# same way and this list looks much like its own. That is deliberate
# duplication, not a missing import: a template is standalone user-forkable
# code, and the only thing the server and a template share is the fused api.
# Neither side is authoritative for the other, so neither is held to the
# other's list.
#
# Ordered most-canonical first, `.exe` ahead of any `.cmd` shim: a shim has to
# be run through cmd.exe, which re-parses the command line (see _popen_argv).
_CLAUDE_WINDOWS_CANDIDATES = (
    # native installer (irm https://claude.ai/install.ps1 | iex) — recommended
    r"%USERPROFILE%\.local\bin\claude.exe",
    # winget install Anthropic.ClaudeCode, via winget's own shim dir
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe",
    # npm install -g @anthropic-ai/claude-code, in npm's global prefix
    r"%APPDATA%\npm\claude.exe",
    r"%APPDATA%\npm\claude.cmd",
    # legacy local npm install, written by older Claude Code versions
    r"%USERPROFILE%\.claude\local\claude.exe",
)
_CLAUDE_POSIX_CANDIDATES = ("~/.local/bin/claude", "/opt/homebrew/bin/claude",
                            "/usr/local/bin/claude")


def _claude_bin() -> str | None:
    """Path to the claude CLI: FUSED_RENDER_CLAUDE_BIN overrides, else PATH,
    else the platform's known install locations. None when nothing is found —
    the caller turns that into an `ai_unavailable` error."""
    forced = os.environ.get(_AI_BIN_ENV)
    if forced:
        return forced
    found = shutil.which("claude")
    if found:
        return found
    candidates = (_CLAUDE_WINDOWS_CANDIDATES if os.name == "nt"
                  else _CLAUDE_POSIX_CANDIDATES)
    for candidate in candidates:
        # expandvars for the %VAR% Windows entries, expanduser for the ~ POSIX
        # ones; each is a no-op on the other platform's shape.
        path = os.path.expanduser(os.path.expandvars(candidate))
        if os.path.isfile(path):
            return path
    return None


def _needs_cmd_shim(bin_path: str) -> bool:
    """Whether `bin_path` can only be started through cmd.exe.

    npm installs claude as a .cmd/.bat shim, which CreateProcess (and so
    create_subprocess_exec) cannot run directly — only cmd.exe can."""
    return sys.platform == "win32" and bin_path.lower().endswith((".cmd", ".bat"))


def _cmd_quote(arg: str) -> str:
    """Quote one argument for the verbatim payload of `cmd /d /s /c "..."`.

    EVERY element is quoted, not just the ones with spaces: /s stops cmd from
    re-parsing the payload's quotes, but it does NOT stop cmd from acting on
    metacharacters (& | > < ^), and a quoted run is where those are literal.
    Windows paths cannot contain `"` and every other element here is a static
    literal or charset-validated, so there is no inner quote to escape —
    assert rather than silently produce a line that means something else."""
    if '"' in arg:
        raise ValueError(f"argument may not contain a double quote: {arg!r}")
    return f'"{arg}"'


def _popen_cmd(bin_path: str, args: list[str]) -> list[str] | str:
    """How to spawn the CLI: an argv list, or — behind a Windows .cmd/.bat
    shim — one command STRING for the cmd.exe hop.

    A shim can only be started through cmd.exe, and the naive form of that is
    the argv list ["cmd.exe", "/c", bin_path, *args]. It does not work.
    Windows has no argv: CreateProcess takes a command line, which asyncio
    builds with subprocess.list2cmdline, quoting each element that needs it.
    cmd.exe preserves that inner quoting only when the rest of its line holds
    exactly TWO quote characters; a shim path with spaces plus any quoted
    argument makes four, cmd falls through to its strip-the-outermost-pair
    rule and re-splits at the spaces — so a `C:\\Users\\John Doe\\...` install
    never runs:

        >>> subprocess.list2cmdline(["cmd.exe", "/c", r"C:\\p ath\\claude.cmd",
        ...                          "-p", r"C:\\Users\\John Doe\\t.txt"])
        'cmd.exe /c "C:\\\\p ath\\\\claude.cmd" -p "C:\\\\Users\\\\John Doe\\\\t.txt"'

    Nor can the fixed line be smuggled through as one argv ELEMENT, because
    list2cmdline would escape the quotes we just added. So the shim path
    returns a string and is spawned as a command line instead (see
    _spawn_claude_stream), which CPython wraps as `comspec /c "<payload>"` — one
    outer quote pair around a payload in which every element is quoted. cmd
    then strips exactly that outer pair and reads the rest as written.

    Every element is quoted, not only the ones with spaces: the outer pair
    stops cmd re-parsing QUOTES, not metacharacters (& | > < ^), and a quoted
    run is where those stay literal. Nothing here can contain a `"` — Windows
    paths cannot, and the rest is static or charset-validated — and
    _cmd_quote raises rather than emit a line that means something else."""
    if not _needs_cmd_shim(bin_path):
        return [bin_path] + args
    return " ".join(_cmd_quote(a) for a in [bin_path] + args)


def _kill_process_tree(proc) -> None:
    """Kill `proc` and, on Windows, its whole descendant tree.

    A .cmd shim runs through cmd.exe, so proc.kill() there terminates only
    cmd.exe and orphans the node/claude child — which keeps running (and
    billing) after we've answered timeout. taskkill /T walks the tree;
    proc.kill() stays as the POSIX path and the Windows fallback."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _ai_cmd(bin_path: str, model: str, sp_file: str) -> list[str] | str:
    """The stream-json spawn command for the persistent completion process.

    --input-format stream-json is what makes the warm instance possible: the
    process starts, loads Node + the CLI, and then WAITS for messages on
    stdin — so the expensive startup happens once, before any prompt exists.
    --verbose is required by the CLI whenever --output-format stream-json is
    used (it exits 1 without it). --include-partial-messages is always on:
    the extra stream_event lines cost nothing to skip in non-streaming mode,
    and one spawn shape serves both modes. No --max-turns: the instance is
    multi-turn by design (per-request isolation comes from /clear, not from
    process death).

    No user-controlled STRING may enter argv: on the Windows .cmd-shim path
    cmd.exe re-parses the whole line, and cmd-escaping arbitrary text is not
    reliably possible. The user prompt travels over stdin as a stream-json
    message (which also dodges the OS argv size cap for the documented
    embed-JSON-aggregates pattern), the system prompt goes via
    --system-prompt-file (`sp_file`, our own tempdir path) at spawn and via
    the set_model control_request afterwards, and the model is
    charset-validated (_AI_MODEL_RE). _popen_cmd turns the result into an
    argv list, or one fully-quoted command string behind a .cmd/.bat shim."""
    return _popen_cmd(bin_path, [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model", model,
        "--system-prompt-file", sp_file,
        # Single-token equals form, never a separate "" argv element: the
        # cmd.exe %* expansion behind a .cmd shim drops empty args — the
        # flags would then swallow the next token and leave tools/settings
        # enabled. (Verified against claude 2.1.220: parses identically,
        # same 544 input tokens.)
        "--tools=",
        "--setting-sources=",
        "--no-session-persistence",
    ])


async def _spawn_claude_stream(cmd: list[str] | str, env: dict):
    """Spawn one claude CLI process in stream-json mode; return the process.

    The single subprocess hop, module-level so tests can patch it — the same
    discipline as _fs_stat/_fs_write. The caller writes the user message to
    stdin later (possibly much later, for a prewarmed process) and reads
    events off stdout line by line.

    A list `cmd` is exec'd directly. A string is the Windows .cmd-shim case
    (_popen_cmd): it must go through create_subprocess_shell, whose comspec
    wrapping is what gives cmd.exe the single outer quote pair it can parse
    deterministically. That is NOT a shell-injection surface — the payload is
    ours, fully quoted, and holds no user text (the prompt is on stdin, the
    system prompt in a file, the model charset-validated)."""
    kwargs = dict(
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        # a result event carrying a big completion is one stdout line; the
        # default 64KiB StreamReader limit would make readline() blow up on it
        limit=16 * 1024 * 1024,
        # close_fds=False forces the posix_spawn path instead of fork()+exec:
        # fork() runs PROJ's pthread_atfork child handler against the server's
        # live proj.db SQLite handle and SIGSEGVs the child (exit -11). Same
        # fix as executor.py's worker spawn — see the full story there and in
        # tests/test_worker_forksafe.py.
        close_fds=False,
        # a windowless server must not flash a console window per call
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if isinstance(cmd, str):
        return await asyncio.create_subprocess_shell(cmd, **kwargs)
    return await asyncio.create_subprocess_exec(*cmd, **kwargs)


async def _ai_spawn(bin_path: str, model: str, system_prompt: str):
    """Write the system-prompt file, build the command and spawn the
    stream-json process; the sp file's path rides on the process object so
    _ai_reap can delete it when the process is reaped (the instance outlives
    any single request, so the file must too).

    The env is os.environ untouched: effort/thinking are Claude Code's own
    semantics now (the effortLevel flag setting), not env-var overrides."""
    sp_file = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".txt",
        prefix="fused_render_ai_sp_", delete=False)
    try:
        sp_file.write(system_prompt)
        sp_file.close()
        proc = await _spawn_claude_stream(
            _ai_cmd(bin_path, model, sp_file.name), dict(os.environ))
    except BaseException:
        try:
            os.unlink(sp_file.name)
        except OSError:
            pass
        raise
    proc._fused_ai_sp_file = sp_file.name
    return proc


async def _ai_reap(proc) -> None:
    """Kill a claude process, wait for it, and remove its system-prompt file.

    Shielded: a reap is cleanup that must complete even when the caller is
    being cancelled (client disconnect, server shutdown) — an interrupted
    wait() leaves a zombie and an interrupted unlink leaks the temp file."""

    async def reap():
        try:
            if proc.returncode is None:
                _kill_process_tree(proc)
            await proc.wait()
        except (OSError, ProcessLookupError):
            pass
        sp_file = getattr(proc, "_fused_ai_sp_file", None)
        if sp_file:
            try:
                os.unlink(sp_file)
            except OSError:
                pass

    await asyncio.shield(reap())


class _AiProcFailure(Exception):
    """The claude process died or misbehaved (stdin write failed, stdout hit
    EOF before an expected event, a control request errored or timed out)."""


class _AiSession:
    """ONE persistent claude process, reconfigured per request.

    The process is spawned once (at server startup, or lazily on the first
    call) and then RESET between requests over its stdin protocol instead of
    being killed: /clear wipes the conversation context (~0.7s — what keeps
    one page's prompt out of another's completion), a set_model
    control_request applies the request's model and system prompt (~0ms,
    sent unconditionally — every request fully specifies its own config, so
    the instance carries NO config state between requests, only the process
    handle), and the thinking budget/effort pair is applied per request
    (clamp to 0 for absent/low effort, reset-then-effortLevel otherwise —
    see configure). All probed on claude 2.1.220.

    Requests are SERIALIZED by `lock`: a second concurrent fused.ai call
    waits for the first. Accepted tradeoff — this is a local single-user app,
    calls complete in seconds, and one flat ~350MB Node process beats a
    spawn-per-call churn where every config change paid a 2s cold start.

    Health: a dead instance (crash, auth expiry) is respawned on the next
    request; a request that finds the instance wedged (control timeout,
    write failure) kills it and retries once on a fresh spawn. If a future
    CLI drops the set_model system_prompt field, that same path degrades
    gracefully: the control error triggers a respawn whose argv carries the
    requested config."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self._proc = None
        self._ctrl_seq = 0
        self._spawn_task = None  # startup prewarm; ref so it isn't GC'd

    # -- lifecycle ---------------------------------------------------------

    async def _spawn(self, model: str, system_prompt: str):
        bin_path = _claude_bin()
        if not bin_path:
            raise _AiProcFailure(
                "claude binary not found on PATH; install Claude Code or "
                f"set {_AI_BIN_ENV} to its location")
        self._proc = await _ai_spawn(bin_path, model, system_prompt)
        return self._proc

    async def _discard(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            await _ai_reap(proc)

    def prewarm_default(self) -> None:
        """Spawn the instance ahead of the first request (startup hook).
        Fire-and-forget; a missing binary or failed spawn just means the
        first call pays the cold start."""

        async def go():
            async with self.lock:
                if self._proc is None:
                    try:
                        await self._spawn(_AI_DEFAULT_MODEL,
                                          _AI_DEFAULT_SYSTEM_PROMPT)
                    except (_AiProcFailure, OSError):
                        pass

        self._spawn_task = asyncio.ensure_future(go())

    async def shutdown(self) -> None:
        task = self._spawn_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        async with self.lock:
            await self._discard()

    # -- the stdin reconfiguration protocol ---------------------------------

    def _write(self, obj: dict) -> None:
        try:
            self._proc.stdin.write(
                (json.dumps(obj) + "\n").encode("utf-8"))
        except (OSError, ValueError) as exc:
            raise _AiProcFailure(f"could not write to the claude CLI: {exc}")

    async def _read_event(self, timeout: float) -> dict:
        """The next JSON event line off stdout (non-JSON noise skipped)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                raise _AiProcFailure(
                    f"could not read from the claude CLI: {exc}")
            if not line:
                raise _AiProcFailure(
                    f"claude CLI exited with code {self._proc.returncode}")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                return event

    async def _control(self, request: dict) -> None:
        """Send one control_request and await its control_response.

        A wedged reconfiguration (no response within _AI_CTRL_TIMEOUT_S) or
        an error response is an _AiProcFailure — the caller kills and
        respawns. stdin is processed sequentially by the CLI, but each
        response is awaited anyway: determinism, and the error branch."""
        self._ctrl_seq += 1
        request_id = f"fused-{self._ctrl_seq}"
        self._write({"type": "control_request", "request_id": request_id,
                     "request": request})
        deadline = asyncio.get_running_loop().time() + _AI_CTRL_TIMEOUT_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                event = await self._read_event(max(remaining, 0.001))
            except asyncio.TimeoutError:
                raise _AiProcFailure(
                    f"claude CLI control request ({request.get('subtype')}) "
                    f"did not answer within {_AI_CTRL_TIMEOUT_S:.0f}s")
            if event.get("type") != "control_response":
                continue  # unrelated events (system/*) may interleave
            response = event.get("response") or {}
            if response.get("request_id") not in (None, request_id):
                continue
            if response.get("subtype") == "error" or event.get("error"):
                detail = response.get("error") or event.get("error")
                raise _AiProcFailure(
                    f"claude CLI rejected {request.get('subtype')}: "
                    f"{str(detail)[:300]}")
            return

    async def _clear(self) -> None:
        """Wipe the conversation context: /clear as a plain user message.
        The CLI answers with a conversation_reset event and a zero-cost
        local result (num_turns:0); await the result so the next write
        starts from a settled process. Resets effortLevel, NOT the system
        prompt (probed)."""
        self._write({"type": "user", "message": {
            "role": "user", "content": "/clear"}})
        deadline = asyncio.get_running_loop().time() + _AI_CTRL_TIMEOUT_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                event = await self._read_event(max(remaining, 0.001))
            except asyncio.TimeoutError:
                raise _AiProcFailure(
                    "claude CLI /clear did not settle within "
                    f"{_AI_CTRL_TIMEOUT_S:.0f}s")
            if event.get("type") == "result":
                return

    async def configure(self, model: str, system_prompt: str,
                        effort: str | None):
        """Make the instance ready for one request; return its process.

        Spawns (or respawns a dead instance) if needed, then: /clear always —
        context isolation between fused.ai calls is not optional; set_model
        with model AND system_prompt unconditionally (~0ms — every request
        fully specifies its own config, no state carried between requests);
        then the thinking budget, unconditionally too (probed on 2.1.220):

        - effort absent or "low": set_max_thinking_tokens 0 — the universal
          no-thinking switch. It works on EVERY model, unlike effortLevel,
          which non-effort-capable models (haiku, the default) silently
          ignore — left to itself, haiku THINKS by default in stream-json
          mode (a one-word answer measured 159 output tokens / ~5s). The
          clamp alone suffices for "low"; no apply_flag_settings is sent
          (the budget clamp overrides effortLevel anyway).
        - effort medium|high|xhigh: set_max_thinking_tokens null — resets
          the budget to the session default, mandatory every such request
          because the clamp PERSISTS across /clear (unlike effortLevel,
          which /clear resets) — then apply_flag_settings{effortLevel}.

        Raises _AiProcFailure/OSError — the caller discards and retries
        once on a fresh spawn."""
        if self._proc is None or self._proc.returncode is not None:
            await self._discard()
            await self._spawn(model, system_prompt)
        await self._clear()
        # system_prompt is always non-empty here (the relay defaults it):
        # the CLI rejects an empty string, and there is no revert-to-default
        # form — the full prompt travels on every request.
        await self._control({"subtype": "set_model", "model": model,
                             "system_prompt": system_prompt})
        if effort is None or effort == "low":
            await self._control({"subtype": "set_max_thinking_tokens",
                                 "max_thinking_tokens": 0})
        else:
            await self._control({"subtype": "set_max_thinking_tokens",
                                 "max_thinking_tokens": None})
            await self._control({"subtype": "apply_flag_settings",
                                 "settings": {"effortLevel": effort}})
        return self._proc


_AI_SESSION = _AiSession()


async def _ai_drive(proc, prompt: str, timeout: float, on_delta=None) -> dict:
    """Write the user message to a stream-json process and read events until
    the terminal `result` line; return it parsed.

    `on_delta(text)` is called per text_delta when streaming (thinking_delta
    and every other event type are skipped). The timeout covers message-write
    to result. Raises asyncio.TimeoutError or _AiProcFailure (died/EOF/
    garbage before a result — the process is reaped on EOF; the session
    notices its death via returncode on the next request)."""
    message = json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": prompt}]}})
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        proc.stdin.write((message + "\n").encode("utf-8"))
        await proc.stdin.drain()
    except (OSError, ValueError) as exc:
        raise _AiProcFailure(f"could not write to the claude CLI: {exc}")
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), remaining)
        except asyncio.TimeoutError:
            raise
        except (OSError, ValueError) as exc:  # ValueError: line over limit
            raise _AiProcFailure(f"could not read from the claude CLI: {exc}")
        if not line:  # EOF before a result event: the process died on us
            stderr = b""
            try:
                stderr = await asyncio.wait_for(proc.stderr.read(), 5)
            except (asyncio.TimeoutError, OSError, ValueError):
                pass
            await _ai_reap(proc)
            tail = stderr.decode("utf-8", "replace").strip()[-500:]
            raise _AiProcFailure(
                f"claude CLI exited with code {proc.returncode}"
                + (f": {tail}" if tail else ""))
        try:
            event = json.loads(line)
        except ValueError:
            continue  # tolerate non-JSON noise between events
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            return event
        if on_delta is not None and event.get("type") == "stream_event":
            inner = event.get("event")
            if isinstance(inner, dict) \
                    and inner.get("type") == "content_block_delta":
                delta = inner.get("delta")
                if isinstance(delta, dict) \
                        and delta.get("type") == "text_delta" \
                        and isinstance(delta.get("text"), str):
                    on_delta(delta["text"])


def _ai_result_payload(data: dict, requested_model: str):
    """Map a terminal `result` event to the RH-11 result payload.
    Returns (payload, None) on success or (None, error_message)."""
    try:
        text = data["result"]
    except (LookupError, TypeError):
        return None, "claude CLI returned an unexpected response shape"
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        return None, f"claude CLI reported an error: {str(text)[:500]}"
    # The requested model may be an alias (haiku/sonnet/opus); modelUsage is
    # keyed by the full id the CLI actually ran, so prefer that for the echo.
    model_usage = data.get("modelUsage")
    used_model = requested_model
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        used_model = next(iter(model_usage))
    return {"text": text, "model": used_model,
            "usage": _ai_usage(data.get("usage"))}, None


def _local_relay(model: str, prompt: str, system_prompt: str, stream: bool,
                 body: dict):
    """One completion from a model resident on THIS machine (SPEC §40).

    Same wire shape as the Claude path, deliberately: `{ok, result:{text, model,
    usage}}`, or NDJSON `{"type":"chunk","text"}` lines closed by `{"type":"done"}`
    when streaming. A page swapping `model: "opus"` for
    `model: "mlx-community/Qwen3-8B-4bit"` should have to change nothing else,
    and `fused.ai`'s own streaming reader is already written against that shape.

    A model that is not resident answers **409 with the job id of the load this
    call just started** — see `supervisor.generate_text`. That is a real state,
    not an error to swallow: the page can show the download it just caused.
    """
    from fused_render.ai import supervisor

    messages = [{"role": "user", "content": prompt}]
    if system_prompt and system_prompt != _AI_DEFAULT_SYSTEM_PROMPT:
        messages.insert(0, {"role": "system", "content": system_prompt})
    request = {
        "messages": messages,
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
    }
    request = {k: v for k, v in request.items() if v is not None}

    try:
        events = supervisor.generate_text(model, request)
        first = next(events, None)
    except supervisor.ModelNotReady as e:
        return JSONResponse(
            {"ok": False, "error": {"type": "model_loading", "message": str(e),
                                    "jobId": e.job_id}},
            status_code=409)
    except supervisor.SupervisorError as e:
        return _ai_error("ai_unavailable", str(e), status=502)

    def walk():
        """The events, with the one already pulled off put back in front."""
        if first is not None:
            yield first
        yield from events

    if not stream:
        text, usage = [], {}
        for event in walk():
            if event.get("type") == "chunk":
                text.append(event.get("text") or "")
            elif event.get("type") == "done":
                if not event.get("ok", True):
                    return _ai_error("ai_error", str(event.get("error") or "generation failed"),
                                     status=502)
                usage = {"output_tokens": event.get("tokens"),
                         "seconds": event.get("seconds")}
        return JSONResponse(
            {"ok": True, "result": {"text": "".join(text), "model": model, "usage": usage}})

    def lines():
        # Errors after the first byte are demoted to an ok:false done frame on a
        # 200, exactly as the Claude path does — the status is already sent.
        try:
            for event in walk():
                if event.get("type") == "chunk":
                    yield json.dumps({"type": "chunk", "text": event.get("text") or ""}) + "\n"
                elif event.get("type") == "done":
                    ok = bool(event.get("ok", True))
                    yield json.dumps({
                        "type": "done", "ok": ok, "model": model,
                        "usage": {"output_tokens": event.get("tokens"),
                                  "seconds": event.get("seconds")},
                        **({} if ok else {"error": {
                            "type": "ai_error",
                            "message": str(event.get("error") or "generation failed")}}),
                    }) + "\n"
        except supervisor.SupervisorError as e:
            yield json.dumps({"type": "done", "ok": False,
                              "error": {"type": "ai_unavailable", "message": str(e)}}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


async def _ai_relay(body: dict):
    """Validate an /api/ai body and run one claude CLI completion.

    Module-level (not a closure) so tests can drive it directly and mock the
    subprocess hop (_spawn_claude_stream). Returns a JSONResponse, or — when
    the body carries {"stream": true} — an NDJSON StreamingResponse of
    {"type":"chunk","text"} lines closed by a {"type":"done"} line. All
    validation happens BEFORE any streaming starts, so 400s and the
    binary-missing 502 are always proper JSON; only an error after the first
    byte is demoted to an ok:false done frame on a 200."""
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _ai_error(
            "bad_request", "request body must include 'prompt': a non-empty string",
            status=400)

    model = body.get("model")
    if model is not None and (
            not isinstance(model, str) or not _AI_MODEL_RE.fullmatch(model)):
        return _ai_error(
            "bad_request",
            "'model' must be a model id or alias (letters, digits, . _ -)",
            status=400)
    # Precedence: the caller's explicit model, then the user's preference, then
    # this relay's own default. Read per request (like the engine pref), so a
    # change on the Preferences page applies to the next call with no restart.
    # `default_model` is module-level so the mapping is the only thing between
    # the pref and argv — an unmapped value falls through to the default rather
    # than being passed along.
    model = model or _AI_SHORT_MODEL_IDS.get(default_model()) or _AI_DEFAULT_MODEL
    effort = body.get("effort")
    if effort is not None and effort not in _AI_EFFORTS:
        return _ai_error(
            "bad_request",
            "'effort' must be one of: %s" % ", ".join(_AI_EFFORTS),
            status=400)
    system_prompt = body.get("system_prompt")
    if not (isinstance(system_prompt, str) and system_prompt):
        system_prompt = _AI_DEFAULT_SYSTEM_PROMPT

    stream = body.get("stream")
    if stream is not None and not isinstance(stream, bool):
        return _ai_error(
            "bad_request", "'stream' must be a boolean", status=400)

    # The fork. Everything above is shared validation — a prompt is a prompt and
    # a stream flag is a stream flag wherever the tokens come from — and
    # everything below this line is the Claude CLI's own path.
    if _is_local_model(model):
        # In a THREAD. `_local_relay` is blocking I/O to a worker process: it
        # waits for the first token before it can answer, and the non-streaming
        # path waits for the whole completion. On a local model that is seconds
        # to minutes, and on the event loop it is the whole server frozen for
        # that long. (The StreamingResponse it returns is fine — Starlette
        # iterates a sync generator in a threadpool of its own.)
        return await asyncio.to_thread(
            _local_relay, model, prompt, system_prompt, bool(stream), body)

    if not _claude_bin():
        return _ai_error(
            "ai_unavailable",
            "claude binary not found on PATH; install Claude Code or set "
            f"{_AI_BIN_ENV} to its location")

    async def run_once(on_delta=None):
        """One completion through the shared instance, start to finish.

        Holds the session lock for the whole call: reconfigure (/clear,
        set_model, effort), send the user message, read to the result.
        Serialized on purpose — see _AiSession. A failure before anything
        was DELIVERED (instance died idle, wedged reconfig, write error)
        gets ONE retry on a fresh spawn; once a delta reached the client,
        replaying the prompt could emit text twice, so no retry. Deltas
        that arrive with no on_delta reader (non-streaming — the events
        flow regardless) never block the retry. On failure or timeout the
        instance is discarded so the next request starts clean."""
        delivered = False

        def deliver(text):
            nonlocal delivered
            if on_delta is not None:
                delivered = True
                on_delta(text)

        async with _AI_SESSION.lock:
            try:
                proc = await _AI_SESSION.configure(model, system_prompt,
                                                   effort)
                return await _ai_drive(proc, prompt, _AI_TIMEOUT_S,
                                       on_delta=deliver)
            except asyncio.CancelledError:
                # Client went away mid-turn: the process is still emitting
                # this turn's events, and a later request's /clear loop would
                # misread them. Discard — the next call spawns fresh.
                await _AI_SESSION._discard()
                raise
            except (_AiProcFailure, OSError, asyncio.TimeoutError) as exc:
                await _AI_SESSION._discard()
                if delivered or isinstance(exc, asyncio.TimeoutError):
                    raise
                # one fresh-spawn retry: covers an instance that died idle
                # or wedged in ways the returncode check can't see
                try:
                    proc = await _AI_SESSION.configure(
                        model, system_prompt, effort)
                    return await _ai_drive(proc, prompt, _AI_TIMEOUT_S,
                                           on_delta=deliver)
                except asyncio.CancelledError:
                    # Cancelled mid-retry: the respawned instance is left
                    # mid-reconfig/mid-turn. Discard it too, or the next
                    # call's /clear would misread its leftover events.
                    await _AI_SESSION._discard()
                    raise
                except (_AiProcFailure, OSError, asyncio.TimeoutError):
                    await _AI_SESSION._discard()
                    raise

    if not stream:
        try:
            data = await run_once()
        except asyncio.TimeoutError:
            return _ai_error(
                "timeout",
                f"claude CLI did not answer within {_AI_TIMEOUT_S:.0f}s")
        except OSError as exc:
            return _ai_error(
                "ai_unavailable", f"could not run the claude CLI: {exc}")
        except _AiProcFailure as exc:
            return _ai_error("ai_error", str(exc))
        payload, err = _ai_result_payload(data, model)
        if err is not None:
            return _ai_error("ai_error", err)
        return JSONResponse({"ok": True, "result": payload})

    # Streaming: NDJSON over a chunked 200. Anything that goes wrong after
    # the first chunk left the wire cannot change the status code, so errors
    # become the terminal done frame instead.
    async def ndjson():
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.ensure_future(
            run_once(on_delta=queue.put_nowait))
        try:
            while True:
                get = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield json.dumps(
                        {"type": "chunk", "text": get.result()}) + "\n"
                    continue
                get.cancel()
                # the process finished: flush any deltas that raced the result
                while not queue.empty():
                    yield json.dumps(
                        {"type": "chunk", "text": queue.get_nowait()}) + "\n"
                break
            try:
                data = task.result()
            except asyncio.TimeoutError:
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "timeout",
                    "message": "claude CLI did not answer within "
                               f"{_AI_TIMEOUT_S:.0f}s"}}) + "\n"
                return
            except OSError as exc:
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_unavailable",
                    "message": f"could not run the claude CLI: {exc}"}}) + "\n"
                return
            except _AiProcFailure as exc:
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_error", "message": str(exc)}}) + "\n"
                return
            payload, err = _ai_result_payload(data, model)
            if err is not None:
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_error", "message": err}}) + "\n"
                return
            yield json.dumps(
                {"type": "done", "ok": True, "result": payload}) + "\n"
        finally:
            if not task.done():
                # Client went away mid-stream: cancel AND await, so
                # run_once's cancel branch (discard the now-mid-turn
                # instance) actually runs before the generator is dropped.
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(ndjson(), media_type="text/x-ndjson")


def _ai_usage(raw) -> dict | None:
    """Normalize CLI usage to exactly {input_tokens, output_tokens} or None.

    The response schema GUARANTEES this shape (Anthropic-style names, NOT
    OpenAI's prompt_tokens/completion_tokens — see RH-11): pages read
    usage.output_tokens without guarding, so a CLI whose usage block gains,
    loses or retypes fields must degrade to null rather than leak an unknown
    shape through."""
    if not isinstance(raw, dict):
        return None
    tokens = {k: raw.get(k) for k in ("input_tokens", "output_tokens")}
    if any(not isinstance(v, int) or isinstance(v, bool)
           for v in tokens.values()):
        return None
    return tokens


# Warm claude instance for fused.ai (D168/D169): pay the ~2s Node/CLI
# startup before the first request instead of inside it. Fire-and-forget —
# server readiness never waits on it, and a missing binary just skips it.
def prewarm_ai():
    _AI_SESSION.prewarm_default()

async def shutdown_ai_session():
    await _AI_SESSION.shutdown()


@router.post("/api/ai")
async def api_ai(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # fused.ai() — validation and the claude CLI hop live in _ai_relay
    # (module-level so tests can drive it with the subprocess mocked).
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return await _ai_relay(body)
