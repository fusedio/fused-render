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

from fused_render import claude_health
from fused_render.server import ai_metrics
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
# The explicit override's name, owned by claude_health so the error text below,
# the resolver, and the health endpoint cannot come to disagree about what the
# user is being told to set.
_AI_BIN_ENV = claude_health.BIN_ENV
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


def _ai_failed(model: str, type_: str, message: str, status: int = 502):
    """An error response that is also COUNTED (AI-12): a call that reached for a
    model and got nothing back.

    Only for failures past the validation gate, and only for calls that asked a
    model for text and got none. A malformed body is refused with `_ai_error`
    and counted nowhere — nothing was asked of a model — and neither is a 409
    from a model that is still loading, which did the thing AI-5 designed it to
    do. Both would make the one number that means "the AI is not working" mean
    something else as well.
    """
    ai_metrics.record_failure(model, type_)
    return _ai_error(type_, message, status=status)


def _claude_seconds(data: dict) -> float | None:
    """How long the CLI says the turn took, in seconds.

    `duration_api_ms` when the result event carries it — it is the model's own
    time, without the CLI's process overhead around it, which is what makes a
    tokens/second figure a statement about the MODEL. `duration_ms` otherwise.
    """
    for key in ("duration_api_ms", "duration_ms"):
        ms = data.get(key)
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
            return ms / 1000.0
    return None


# Where Claude Code installs `claude`, for when it isn't on the PATH this
# process inherited — the packaged app's PATH is the supervisor's, not a
# shell's, and a Finder/Dock-launched .app misses ~/.local/bin and Homebrew.
# On Windows it is worse: a GUI launch inherits the PATH of its login session,
# so an install that appended to the *user* PATH afterwards stays invisible
# until the next sign-in.
#
# IMPORTED, not written here. These used to be two literal tuples, and the app
# held four such lists across this module, claude_config/lib.py,
# core_apps/learn/check_env.py and core_apps/sessions/analyze.py. Any directory
# only some of them knew about was a directory where the app disagreed with
# itself: a CLI in `~/.bun/bin` gave a working Preferences → Claude config tab
# and an `ai_unavailable` from `fused.ai()` on the same machine. claude_health
# owns the union now, and this name stays as the module-local alias every
# function and test below already reads.
#
# The claude chat template (templates/claude/agent.py) still keeps its own copy.
# That is deliberate duplication, not a missing import: a template is standalone
# user-forkable code and may not import the app (D166). It is pinned to this
# list by tests/test_claude_health.py rather than left to drift.
#
# Ordered most-canonical first, `.exe` ahead of any `.cmd` shim: a shim has to
# be run through cmd.exe, which re-parses the command line (see _popen_argv).
_CLAUDE_WINDOWS_CANDIDATES = claude_health.WINDOWS_CANDIDATES
_CLAUDE_POSIX_CANDIDATES = claude_health.POSIX_CANDIDATES


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
        # claude_health.executable, NOT a local isfile. This walks the SAME list
        # claude_health.resolve does, so a check that differed between them would
        # put the health report and the spawn on different binaries: a
        # non-executable dud early in the list (a truncated download, a botched
        # install) is skipped there and taken here, so the first-run strip would
        # call the install ready while every session died on the dud. That is the
        # exact contradiction the shared list was introduced to end.
        if claude_health.executable(path):
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


#: Who may speak in a supplied history. Deliberately not "system" — the system
#: prompt has its own parameter, and letting it arrive twice by two routes is
#: how a caller ends up with two contradictory ones and no way to tell.
_HISTORY_ROLES = ("user", "assistant")


#: Sampling parameter -> (low, high). The wire names, because the worker reads
#: these keys and one spelling should survive the whole trip.
#:
#: `max_tokens`'s ceiling is the load-bearing one and it is not politeness: ONE
#: model is resident per capability and it serves every page on this machine, so
#: an unbounded token budget is not one caller's slow request — it is every
#: other caller blocked behind it. 32k is past any chat turn and short of "this
#: laptop is busy until you notice".
_SAMPLING = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_tokens": (1, 32768),
}


def _sampling_problem(body: dict) -> str | None:
    """What is wrong with the sampling parameters, or None.

    Bools are refused explicitly: `True` is an `int` in Python and would sail
    through a numeric range check as `max_tokens: 1`, which is a one-token reply
    for a caller who typed something meaningless and deserves to be told.
    """
    for name, (low, high) in _SAMPLING.items():
        value = body.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name!r} must be a number"
        if not low <= value <= high:
            return f"{name!r} must be between {low} and {high}"
    return None


def _history_problem(history) -> str | None:
    """Why this history is unusable, or None. The message is the API's manners:
    a chat client passing the wrong shape should be told which turn and what
    was wrong with it, not handed a 500 from inside a worker."""
    if not isinstance(history, list):
        return "'history' must be a list of {role, content} turns"
    for index, turn in enumerate(history):
        if not isinstance(turn, dict):
            return f"'history[{index}]' must be an object with 'role' and 'content'"
        if turn.get("role") not in _HISTORY_ROLES:
            return (f"'history[{index}].role' must be one of: "
                    + ", ".join(_HISTORY_ROLES))
        if not isinstance(turn.get("content"), str):
            return f"'history[{index}].content' must be a string"
    return None


def _local_usage(event: dict) -> dict:
    """The `usage` a local worker's terminal frame becomes.

    Same Anthropic-style names the Claude tier promises (RH-11), from the
    worker's own vocabulary: it counts the tokens it GENERATED as `tokens` and
    the prompt it read as `input_tokens` (AI-3). `input_tokens` is None from a
    runner that cannot count it — a tokenizer that refused the string, or a
    worker built before the count existed — and `null` is the honest answer
    there, never a zero, which would state that the model read nothing.

    `seconds` rides along because the local tier is the one that measures it;
    the Claude tier's duration is passed to the counter separately (AI-12a).
    """
    return {"input_tokens": event.get("input_tokens"),
            "output_tokens": event.get("tokens"),
            "seconds": event.get("seconds")}


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

    # Prior turns first, then the one being asked. Validated by the caller, so
    # only the two fields the worker's chat template reads are passed on —
    # anything else a client kept on its own turns (timestamps, ids) is its
    # business and not the model's.
    history = body.get("history") or []
    messages = [{"role": turn["role"], "content": turn["content"]} for turn in history]
    messages.append({"role": "user", "content": prompt})
    if system_prompt and system_prompt != _AI_DEFAULT_SYSTEM_PROMPT:
        messages.insert(0, {"role": "system", "content": system_prompt})
    request = {
        # `prompt` is the worker's raw path: it hands the text to the model with
        # no chat template. Sending both lets the worker pick, and it prefers
        # `prompt` — so this is only set when the caller asked for raw.
        **({"prompt": prompt} if body.get("raw") else {}),
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
        # NOT counted as a failure (AI-12b). This call did exactly what AI-5
        # says it should: a model that is not resident cannot answer in the
        # seconds a caller has, so the load STARTS and the job id comes back —
        # the caller is meant to watch it and ask again. Counting that beside a
        # timeout or a missing binary is how "3 failed" comes to mean "one
        # model is downloading", which is the conflation this rule exists to
        # prevent: the number has to mean one thing.
        return JSONResponse(
            {"ok": False, "error": {"type": "model_loading", "message": str(e),
                                    "jobId": e.job_id}},
            status_code=409)
    except supervisor.SupervisorError as e:
        return _ai_failed(model, "ai_unavailable", str(e), status=502)

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
                    return _ai_failed(model, "ai_error",
                                      str(event.get("error") or "generation failed"),
                                      status=502)
                usage = _local_usage(event)
        # The counter is fed the SAME dict the caller is about to read (AI-12),
        # here and at the three other terminal frames — so the graph and the
        # response can never disagree about what this completion generated.
        ai_metrics.record(model, usage)
        return JSONResponse(
            {"ok": True, "result": {"text": "".join(text), "model": model, "usage": usage}})

    def lines():
        # Errors after the first byte are demoted to an ok:false done frame on a
        # 200, exactly as the Claude path does — the status is already sent.
        #
        # The done frame carries **result**, the same `{text, model, usage}` the
        # Claude path sends and the same thing the non-streaming reply above
        # returns. It did not, and the shapes only LOOKED alike because the
        # chunks matched: `fused.ai`'s reader resolves with `finished.result`,
        # so a page streaming from a local model got `undefined` back and threw
        # on the first property it read. The full text is accumulated here
        # rather than left to the caller — the caller may have been streaming
        # into a DOM node and have no string to hand back.
        text = []
        try:
            for event in walk():
                if event.get("type") == "chunk":
                    chunk = event.get("text") or ""
                    text.append(chunk)
                    yield json.dumps({"type": "chunk", "text": chunk}) + "\n"
                elif event.get("type") == "done":
                    ok = bool(event.get("ok", True))
                    usage = _local_usage(event)
                    # A CANCELLED generation lands here too, with the tokens it
                    # produced before the Stop: the worker counts what it
                    # emitted (AI-1a), and those tokens were generated by this
                    # machine whether or not anybody wanted them by the end.
                    if ok:
                        ai_metrics.record(model, usage)
                    else:
                        ai_metrics.record_failure(model, "ai_error")
                    yield json.dumps({
                        "type": "done", "ok": ok,
                        **({"result": {
                            "text": "".join(text), "model": model,
                            "usage": usage}}
                           if ok else {"error": {
                            "type": "ai_error",
                            "message": str(event.get("error") or "generation failed")}}),
                    }) + "\n"
        except supervisor.SupervisorError as e:
            ai_metrics.record_failure(model, "ai_unavailable")
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

    # Prior turns, for a caller holding a conversation rather than asking one
    # question. `prompt` stays what it always was — the thing being asked NOW —
    # and this is what came before it, so no call changes meaning by adding it.
    history = body.get("history")
    if history is not None:
        problem = _history_problem(history)
        if problem:
            return _ai_error("bad_request", problem, status=400)

    # Raw continuation: the prompt goes to the model VERBATIM, with no chat
    # template wrapped around it. A different thing from a conversation, not a
    # setting on one — which is why it refuses history rather than quietly
    # winning over it.
    raw = body.get("raw")
    if raw is not None and not isinstance(raw, bool):
        return _ai_error("bad_request", "'raw' must be a boolean", status=400)
    if raw and history:
        return _ai_error(
            "bad_request",
            "'raw' continues the prompt verbatim with no chat template, so it "
            "has nowhere to put 'history' — send one or the other",
            status=400)

    # The fork. Everything above is shared validation — a prompt is a prompt and
    # a stream flag is a stream flag wherever the tokens come from — and
    # everything below this line is the Claude CLI's own path.
    if _is_local_model(model):
        # Sampling is checked INSIDE this branch, not above it, and the reason
        # is which sentence a bad value earns. These parameters do not exist on
        # the Claude path at all, so range-checking them first meant a
        # `temperature: 5.0` sent to Claude was answered "must be between 0.0
        # and 2.0" — an error that invites the caller to correct a number and
        # try again, on a path where no number would ever work. The refusal
        # below is the true one, and it must not be pre-empted by a message that
        # implies support.
        #
        # Bounded at all for the reason the image endpoint clamps its own
        # numbers: `max_tokens` is how long this machine is busy, and one
        # resident model serves every page, so a typo'd 10_000_000 is not one
        # caller's slow request — it is the model unavailable to everything else
        # until it finishes.
        sampling = _sampling_problem(body)
        if sampling:
            return _ai_error("bad_request", sampling, status=400)
        # In a THREAD. `_local_relay` is blocking I/O to a worker process: it
        # waits for the first token before it can answer, and the non-streaming
        # path waits for the whole completion. On a local model that is seconds
        # to minutes, and on the event loop it is the whole server frozen for
        # that long. (The StreamingResponse it returns is fine — Starlette
        # iterates a sync generator in a threadpool of its own.)
        return await asyncio.to_thread(
            _local_relay, model, prompt, system_prompt, bool(stream), body)

    # Refused rather than dropped. The Claude path is one `claude -p` invocation
    # with no conversation to resume, so honouring history would mean inventing
    # one — and silently ignoring it would answer a follow-up as if it were the
    # first question, which reads as the model having forgotten rather than as
    # the API having declined.
    if history:
        return _ai_error(
            "bad_request",
            "'history' is only supported by a local model (a Hugging Face repo "
            "id, e.g. 'mlx-community/Qwen3-8B-4bit'); this call would go to "
            f"{model!r}, which answers one prompt at a time",
            status=400)

    # Same rule, second flag. `raw` means "no chat template" — a thing only
    # something that OWNS the template can honour, and the Claude CLI does not
    # expose one. Dropping it would answer a raw continuation as a chat turn:
    # plausible text, silently not what was asked for.
    if raw:
        return _ai_error(
            "bad_request",
            "'raw' sends the prompt to the model with no chat template, which "
            "only a local model can do (a Hugging Face repo id, e.g. "
            f"'mlx-community/Qwen3-8B-4bit'); this call would go to {model!r}, "
            "which is always a chat",
            status=400)

    # Third flag, same rule. The Claude CLI exposes no sampling knobs at all —
    # `effort` is what it has — so a temperature accepted here would be a
    # setting the caller could watch have no effect, which is the failure mode
    # `history` and `raw` are refused for.
    named = [name for name in _SAMPLING if body.get(name) is not None]
    if named:
        return _ai_error(
            "bad_request",
            f"{', '.join(repr(n) for n in named)} only applies to a local model "
            "(a Hugging Face repo id, e.g. 'mlx-community/Qwen3-8B-4bit'); "
            f"this call would go to {model!r}, which takes 'effort' instead",
            status=400)

    if not _claude_bin():
        return _ai_failed(
            model, "ai_unavailable",
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
            return _ai_failed(
                model, "timeout",
                f"claude CLI did not answer within {_AI_TIMEOUT_S:.0f}s")
        except OSError as exc:
            return _ai_failed(
                model, "ai_unavailable", f"could not run the claude CLI: {exc}")
        except _AiProcFailure as exc:
            return _ai_failed(model, "ai_error", str(exc))
        payload, err = _ai_result_payload(data, model)
        if err is not None:
            return _ai_failed(model, "ai_error", err)
        # Under the RESOLVED id, not the alias the caller sent: "opus" and
        # "claude-opus-5" are one model and must not be two rows in the
        # breakdown. `_ai_result_payload` already did that resolution.
        ai_metrics.record(payload["model"], payload["usage"],
                          _claude_seconds(data))
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
                ai_metrics.record_failure(model, "timeout")
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "timeout",
                    "message": "claude CLI did not answer within "
                               f"{_AI_TIMEOUT_S:.0f}s"}}) + "\n"
                return
            except OSError as exc:
                ai_metrics.record_failure(model, "ai_unavailable")
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_unavailable",
                    "message": f"could not run the claude CLI: {exc}"}}) + "\n"
                return
            except _AiProcFailure as exc:
                ai_metrics.record_failure(model, "ai_error")
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_error", "message": str(exc)}}) + "\n"
                return
            payload, err = _ai_result_payload(data, model)
            if err is not None:
                ai_metrics.record_failure(model, "ai_error")
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_error", "message": err}}) + "\n"
                return
            ai_metrics.record(payload["model"], payload["usage"],
                              _claude_seconds(data))
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


@router.get("/api/ai/metrics")
def api_ai_metrics(minutes: float = 15):
    """What this process has generated, and when — SPEC AI-12.

    An UNGUARDED read, like every other read in this app: the `X-Fused` guard
    (D3) is on the routes that spend this machine's time, and there is nothing
    here but counters a page could have kept itself. It reads no disk and takes
    no lock anybody waits on, so the Usage tab can poll it.

    `minutes` is the width of the returned series; it is CLAMPED rather than
    refused (1 .. the ring's own retention), because a graph asking for two
    hours from a store that keeps one should get the hour, not a 400.
    """
    return ai_metrics.snapshot(minutes)
