import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from fastapi import APIRouter, Body, Header
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from fused_render import claude_health, jobs
from fused_render.ai.runners import formats
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
# How often the remote-Claude activity row re-states itself while a call is in
# flight — a DISPLAY heartbeat and nothing more, the same thing
# `supervisor._QUEUE_TICK_S` is and for the same reason.
#
# The row is opened once and has nothing to report until the answer lands, but
# `jobs.STALE_AFTER_S` (30s) turns any running row with no update in that long
# into *"No longer reporting — the process running it stopped reporting"*:
# dimmed, its ✕ withdrawn, a Dismiss offered instead. A Claude turn routinely
# runs longer than 30s — `_AI_TIMEOUT_S` above allows ten minutes of it, and a
# second call parked on `_AI_SESSION.lock` has not started at all — so without
# this every slow call announced, truthfully as far as the registry could tell,
# that nobody was reporting it, and then succeeded anyway. This is the rule
# `runners/worker_base._heartbeat` states as a CONTRACT: progress whose natural
# granularity is coarser than the stale window has to say "still here".
#
# Well under 30s so a busy loop or a slow machine still leaves margin, and no
# faster than it needs to be: nothing reads these ticks but the clock.
_REMOTE_TICK_S = 10.0
# The row's detail line. Says only that this is remote — the model rides its
# own field (`jobs.py` `Job.model`, a dimmed suffix JobRow draws after the
# title), so the one thing this line is for is the fact a local row's detail
# never states. Named rather than inlined because three sites now write it:
# the opening report, and both sides of the queued swap in `run_once`.
_REMOTE_ROW_DETAIL = "Claude — remote"
# …and what it says while the call is parked behind `_AI_SESSION.lock` — work
# that has not started, which "Claude — remote" alone would show as work in
# progress. `supervisor._QUEUED_DETAIL`'s twin.
_REMOTE_QUEUED_DETAIL = "Queued — another Claude call is in flight"
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
#
# **The seam gained a second shape (D411) and this is the one place it has to
# be spelled out, because it is genuinely not a slash-shaped id.**
# `llamacpp-text`'s curated ids (`formats.GGUF_RECIPES`) are the GGUF's own
# FILENAME, never a repo id — `"Qwen3.5-4B-Q4_K_M.gguf"` has no `/` at all —
# because a GGUF repo commonly ships two dozen quantizations of one model and
# the filename is what tells them apart (see `llama_text.py`'s own docstring).
# `"/" in model` alone therefore sent every curated llamacpp id down the
# CLAUDE path, as an unrecognised alias, which nothing caught because nothing
# had called `fused.ai()` with one — a bug from the moment `llamacpp-text`
# shipped (D411), not something Piece 1/2 introduced, found while auditing
# this file for what the branch invalidated. No Claude alias has ever ended
# in `.gguf` either, so the fix is the same kind of fact as the slash: an
# uncurated repo id Piece 1 resolves (always `org/name`) already has the
# slash and needs nothing new.
_AI_MODEL_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _is_local_model(model: str) -> bool:
    return "/" in model or model.lower().endswith(formats.GGUF_EXTENSION)


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
# held four such lists across this module, claude_config/lib.py, and the
# retired learn and sessions bundled content. Any directory
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


#: How many pictures one request may attach. Bounded for the same reason
#: `max_tokens` is (`_SAMPLING`, above): ONE model is resident per capability
#: and serves every page on this machine, so an unbounded list is not one
#: caller's slow request — it is every other caller's turn behind a request
#: that decided to carry fifty images' worth of placeholder tokens. Eight is
#: past any ordinary "compare these pictures" ask and short of a caller that
#: meant to send a folder.
_MAX_IMAGES = 8


def _images_problem(images) -> str | None:
    """Why this `images` list is unusable, or None. Local models only — the
    caller-facing refusal for a Claude-tier request lives in `_ai_relay`,
    beside `history`'s and `raw`'s own refusals, and this only checks the
    SHAPE: a list of non-empty strings, under the cap."""
    if not isinstance(images, list):
        return "'images' must be a list of absolute file paths"
    if len(images) > _MAX_IMAGES:
        return f"'images' may not carry more than {_MAX_IMAGES} paths"
    for index, path in enumerate(images):
        if not isinstance(path, str) or not path:
            return f"'images[{index}]' must be a non-empty string"
    return None


def _images_unsupported_by_runner(model: str) -> str | None:
    """Why the runner that would actually SERVE `model` cannot be handed an
    image, or None.

    `_images_problem` only checks the shape of the list; this checks whether
    the request means anything at all once it reaches a worker. Only
    `mlx-text`'s own worker reads `images` (`mlx_text/worker.py`'s image
    branch, which builds the prompt through `mlx_vlm.prompt_utils.
    apply_chat_template`) — `llamacpp_text`'s shared `generate` (`runners/
    llama_text.py`) reads `messages`/`prompt`/the sampling knobs and nothing
    else, so a picture handed to it is silently dropped on the floor. That
    would read as a confident answer about nothing on Linux, on Windows, or
    on a Mac where llama.cpp has been promoted over MLX — not a 400, not a
    warning, just an answer about a photograph the model never saw.

    `_accepts_image` (`ai_runtime.py`, AI-11j) already knows this — it is the
    SAME computation the catalog's attach affordance is drawn from, both the
    engine gate and, for `mlx-text`, whether the specific checkpoint even has
    a vision tower — so it is asked here rather than re-derived: a caller
    that ignores the flag, or a client built before it existed, must not get
    an answer the flag never promised.
    """
    from fused_render.ai import registry
    from fused_render.server.routers import ai_runtime

    runner = registry.for_capability(registry.TEXT_GENERATION)
    runner_code = runner.code if runner else None
    if ai_runtime._accepts_image(registry.TEXT_GENERATION, runner_code, model):
        return None
    if runner_code is None:
        return "no text-generation runner is available on this machine to read an image"
    return (f"{model!r} cannot be handed an image on this machine — the resolved "
            f"runner ({runner_code!r}) either cannot read a picture at all, or "
            "this checkpoint has no vision tower")


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


#: How often the prefill watchdog restates the row while it waits for the
#: first token (`_local_relay`'s `_start_watchdog`). Comfortably under
#: `jobs.STALE_AFTER_S` (30s) — a reporter's own poll cadence has to clear
#: that bar by a wide margin or an ordinary slow prefill trips "stalled" on
#: a row that is, in truth, still doing real work. A module constant rather
#: than a literal so a test can shrink it instead of sleeping through 30s of
#: real prefill.
_TEXT_WATCHDOG_TICK_S = 10.0


def _text_title(prompt: str, model: str) -> str:
    """The row's title: the PROMPT's first line, not the model — the same
    argument `_transcribe_title` makes for using the file name instead of
    the model: the manager may show several of these rows at once, and the
    prompt is what tells them apart, not which model is answering. Capped
    shorter than a render's title (60, not 80): a chat prompt is
    conversational text that wraps ugly past a phrase or two, where an
    image prompt is already terse.
    """
    stripped = (prompt or "").strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    return first_line[:60] or model


def _text_prefill_detail(input_tokens) -> str:
    """What the row's opening tick — and the watchdog behind it — say while
    the worker is still reading the prompt: the phase between a resident
    model accepting the request and its first generated token. Named the
    way `_QUEUED_DETAIL` (supervisor.py) names any other wait a person can
    see, and for the same reason a queued transcription's row does not
    invent a percentage: prefill is one forward pass over the whole prompt,
    not a series of steps with a fraction to report, so this says only that
    it is under way. `input_tokens` is `None` whenever the worker cannot
    count it (`mlx_text/worker.py`'s image path, by design — see that
    module's own comment on `_prompt_tokens`), and the sentence has to read
    correctly either way rather than print "None tokens".
    """
    if isinstance(input_tokens, int):
        return f"Processing the prompt — {input_tokens} tokens…"
    return "Processing the prompt…"


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
        # Absolute paths on THIS turn only (mlx_text/worker.py's own boundary,
        # AI-11j) — a LIST, unlike `/api/ai/image`'s single `image`, because a
        # VLM's chat template is told `num_images` and asking about two
        # pictures at once is the ordinary case for this capability, where an
        # edit always has exactly one base image. Omitted entirely rather than
        # sent empty: `images: []` on every call would be a needless departure
        # from the worker's "absent = today's text path" contract for every
        # model that never uses it.
        **({"images": body.get("images")} if body.get("images") else {}),
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
        # prevent: the number has to mean one thing. NO row opens here either
        # (`text_row_fields`'s own docstring) — the load's own row already
        # covers this wait, and a second row for it is exactly the doubling
        # `_wait_ready`'s merge exists to remove elsewhere.
        return JSONResponse(
            {"ok": False, "error": {"type": "model_loading", "message": str(e),
                                    "jobId": e.job_id}},
            status_code=409)
    except supervisor.SupervisorError as e:
        return _ai_failed(model, "ai_unavailable", str(e), status=502)

    # Past here the model was already resident and answered enough to begin
    # generating — `first` came back with no exception — so THIS call earns
    # its own Activity row (image/video/transcription already get one; text
    # was the one kind that reported nowhere). Minted here, not inside
    # `generate_text`: this relay is where the done-frame/error-demotion
    # logic already lives for both the streaming and non-streaming shapes,
    # and it is the caller that mints ids for the other kinds too
    # (`image_job_id`, `transcribe_job_id`).
    job = supervisor.text_job_id(uuid.uuid4().hex)
    row = supervisor.text_row_fields(_text_title(prompt, model), model)

    def tick(**over) -> None:
        """One report, always restating the row's full identity — a row can
        be REBUILT from scratch on any tick (`jobs._sweep` evicts the least
        recently updated running row once `MAX_JOBS` bites), so a tick that
        omitted `title`/`cancellable`/`unit` would recreate a row missing
        them rather than update the one already showing. See
        `transcribe_row_fields`'s docstring for the argument in full."""
        supervisor._report(job, **row, **over)

    # The opening detail names whichever phase `first` actually landed in.
    # `mlx_text/worker.py` sends a `prefill` frame before its first token —
    # naming the size of the prompt it is chewing on when it can count it —
    # but `llama_text.py`'s runner has no such frame and starts straight on
    # `chunk`; either way `first` is what walk() below yields as the loop's
    # own first iteration, so nothing here is dropped, only DESCRIBED before
    # the loop starts.
    opening_detail = "Processing the prompt…"
    if first is not None and first.get("type") == "prefill":
        opening_detail = _text_prefill_detail(first.get("input_tokens"))
    tick(state="running", done=None, total=None, detail=opening_detail)

    # **The prefill watchdog.** `stream_generate` yields nothing at all until
    # the whole prompt has been read — one forward pass, seconds of real
    # work on a long context — and with no tick in that window the row would
    # cross `jobs.STALE_AFTER_S` (30s) and read as "no longer reporting"
    # about work that is genuinely still running. This is honest reporting,
    # not a fake pulse: the stream is open and the worker has not errored,
    # so restating "processing the prompt" on an interval is a true
    # statement about live work, the same discipline `_wait_ready`'s merged
    # tick and `_MeasurementRow`'s benchmark watcher already follow for
    # their own long silent phases. Stopped the moment real progress
    # arrives (the first `chunk`) or the call ends any other way — a daemon
    # thread that outlived the request would keep ticking a row nothing is
    # generating for any more.
    stop_watchdog = threading.Event()

    def _watchdog() -> None:
        while not stop_watchdog.wait(_TEXT_WATCHDOG_TICK_S):
            tick(state="running", done=None, total=None, detail=opening_detail)
            # The ✕ is polled HERE as well as between chunks below, because
            # prefill is precisely the window where it would otherwise sit
            # inert: the row draws a cross from its very first tick
            # (`cancellable=True`), and on a long context a minute can pass
            # before the loop reaches its first chunk-boundary check. A cross
            # that does nothing for a minute is a worse row than one with no
            # cross at all — stopping a prompt that is still being READ has
            # to stop it, not queue the cancel behind the first token of an
            # answer nobody wants any more.
            if supervisor._cancel_requested(job):
                supervisor.cancel_generation()

    watchdog = threading.Thread(target=_watchdog, daemon=True, name="ai-text-watchdog")
    watchdog.start()
    watchdog_stopped = False

    def _stop_watchdog() -> None:
        nonlocal watchdog_stopped
        if not watchdog_stopped:
            stop_watchdog.set()
            watchdog.join(timeout=2.0)
            watchdog_stopped = True

    count = 0
    reported_terminal = False

    def walk():
        """The events, with the one already pulled off put back in front —
        and the row's own lifecycle folded in, so both the streaming and
        non-streaming loops below drive it identically rather than each
        reimplementing the same ticks.

        Cancellation is cooperative, the same channel the benchmark tab's
        own Stop button uses (`ai/benchmark.py`'s `_MeasurementRow._poll_
        once`): a pressed ✕ is forwarded to `supervisor.cancel_generation`,
        which POSTs `/cancel` to the resident worker — the worker's own
        generation loop is what actually stops, replying with a `done`
        frame carrying `cancelled: true` and the tokens it had made by then.
        Checked between chunks, not on every event, because that is the
        cadence real progress arrives on; a cold prefill has nothing to
        check between.
        """
        nonlocal count, reported_terminal

        def _all_events():
            if first is not None:
                yield first
            yield from events

        try:
            for event in _all_events():
                etype = event.get("type")
                if etype == "chunk":
                    _stop_watchdog()
                    count += 1
                    tick(state="running", done=count, total=None, detail="Generating…")
                elif etype == "done":
                    _stop_watchdog()
                    reported_terminal = True
                    if event.get("cancelled"):
                        tick(state="cancelled", done=count, total=None,
                             detail=f"Cancelled after {count} "
                                    f"token{'' if count == 1 else 's'}")
                    elif not event.get("ok", True):
                        tick(state="error", done=count, total=None,
                             message=str(event.get("error") or "generation failed"))
                    else:
                        tick(state="done", done=count, total=count,
                             detail=f"Generated {count} token{'' if count == 1 else 's'}")
                yield event
                if etype == "chunk" and supervisor._cancel_requested(job):
                    supervisor.cancel_generation()
        finally:
            # Every exit that is not a `done` frame — the client aborting a
            # stream mid-generation (`GeneratorExit`, thrown here once this
            # generator is garbage-collected out from under an abandoned
            # response), a `SupervisorError` reading the socket, anything
            # unforeseen — still has to leave the row in a TERMINAL state:
            # a row stuck at "running" forever is the one failure this
            # feature exists to avoid, worse than a row that says "error"
            # for a call nobody was watching any more.
            _stop_watchdog()
            if not reported_terminal:
                reported_terminal = True
                tick(state="error", done=count, total=None,
                     message="generation stopped before it finished")

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

    # Base images for a vision-language local model, on the CURRENT turn only
    # (mlx_text/worker.py's own boundary — `history` stays text-only, matching
    # `_history_problem`'s `content: str` requirement, which this leaves
    # alone). Shape-checked here, refused for Claude below, same as history
    # and raw.
    images = body.get("images")
    if images is not None:
        problem = _images_problem(images)
        if problem:
            return _ai_error("bad_request", problem, status=400)
    # `raw` and `images` refuse each other, the same shape as `raw`/`history`
    # above and for the same underlying reason: `raw` means "no chat template
    # at all", and the image placeholder tokens a picture needs are inserted
    # BY that template (`mlx_vlm.prompt_utils.apply_chat_template`, called
    # only on the image path — see `mlx_text/worker.py::generate`). There is
    # nowhere in a template-free request to put them, so honouring `raw` here
    # would mean silently ignoring `images` — the worker's image branch reads
    # `messages` unconditionally and never looks at `prompt` at all, so a
    # caller setting both today would watch `raw` have no effect with no
    # error, which is exactly the silent-drop `history` is refused instead of
    # dropped for. Refusing the pair, rather than teaching the image path to
    # honour a raw string, is the correct call: mlx-vlm's own template helper
    # is what carries the placeholder tokens, and it takes structured messages
    # to do it, not a bare continuation.
    if raw and images:
        return _ai_error(
            "bad_request",
            "'raw' sends the prompt with no chat template, and the image "
            "placeholder tokens 'images' needs are inserted BY that template "
            "— send one or the other",
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
        # `images` is shape-checked above (any local model), but SHAPE is not
        # SUPPORT: only mlx-text's own worker reads `images` at all
        # (`mlx_text/worker.py`'s image branch) — `llamacpp_text`'s shared
        # `generate` reads `messages`/`prompt`/the sampling knobs and nothing
        # else, so a picture handed to it is dropped on the floor rather than
        # refused, and a caller reads back a confident answer about nothing.
        # The catalog already knows this answer per entry (`acceptsImage`,
        # AI-11j) so a caller who never reads that flag — or a stale client
        # that predates it — must not be trusted to have honoured it; the
        # request path enforces the SAME computation rather than a second,
        # looser one.
        if images:
            unsupported = _images_unsupported_by_runner(model)
            if unsupported:
                return _ai_error("bad_request", unsupported, status=400)
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

    # Third flag, same rule. The Claude CLI has no notion of an attachment —
    # `claude -p` takes a prompt string — so silently dropping the picture
    # would answer as if it had never been sent, which reads as the model
    # ignoring what was attached rather than the API declining to attach it.
    if images:
        return _ai_error(
            "bad_request",
            "'images' is only supported by a local model (a Hugging Face repo "
            "id, e.g. 'mlx-community/Qwen3-8B-4bit'); this call would go to "
            f"{model!r}, which cannot be handed a picture",
            status=400)

    # Fourth flag, same rule. The Claude CLI exposes no sampling knobs at all —
    # `effort` is what it has — so a temperature accepted here would be a
    # setting the caller could watch have no effect, which is the failure mode
    # `history`, `raw` and `images` are refused for.
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

    # Bottom-right activity notification (fused_render/jobs.py's registry —
    # see supervisor._report for the local-model twin of this write). Before
    # this, only a LOCAL model ever produced a row there: a page calling
    # fused.ai() against remote Claude did nothing visible in that corner, so
    # a user watching it could not tell they were talking to Claude instead
    # of a model on their own machine. There is no worker process to report
    # from on this path, so the relay opens and closes the row itself — one
    # per call (a fresh id, not a fixed one, because a second call queued
    # behind the same-instance lock in _AiSession is a second call, not a
    # progress tick on the first) — with wording that says "remote" up
    # front, which is the whole point of adding it.
    #
    # `cancellable=False`: JobRow renders a ✕ on any running, cancellable row
    # (DownloadManager.tsx), and pressing it only sets jobs.py's
    # `cancel_requested` flag for a reporter to notice on its next tick.
    # Nothing in this relay polls that flag, so advertising a ✕ here would
    # ship a button that visibly does nothing when pressed.
    #
    # NOT opened here, before the stream/non-stream fork: `ndjson()` below is
    # an async generator that Starlette only starts iterating once it
    # actually sends the response body, so a client that disconnects before
    # that first `__anext__` never runs the generator at all — including its
    # `finally`. A row opened out here, unconditionally, would then never
    # close: exactly the leak the streaming `finally` exists to prevent, just
    # relocated one step earlier. Each branch below opens its own row as its
    # own first action instead, so there is no window where a row exists
    # with nothing yet committed to closing it.
    _remote_job = jobs.SERVER_ID_PREFIX + "ai-claude:" + uuid.uuid4().hex
    _remote_job_closed = False

    def _report_remote(**fields) -> None:
        """One tick on the remote-Claude row, best-effort (never breaks the
        call — same discipline as supervisor._report).

        Catches EVERYTHING, like every other reporter here
        (`supervisor._report`, `schedule._report`, `claude_install._report`):
        reporting is decoration, and a registry that refuses a write must not
        cost the call its answer. The narrow `(JobError, ValueError)` this used
        to catch was survivable while every caller was a one-shot on an exit
        path; `_start_remote_beat` made it a LOOP, and there anything else
        escaping kills the heartbeat for the rest of the call — whose symptom
        is precisely the stalled row the heartbeat exists to prevent, with
        nothing else to show for it.
        """
        nonlocal _remote_job_closed
        if fields.get("state") in jobs.TERMINAL_STATES:
            if _remote_job_closed:
                return
            _remote_job_closed = True
        try:
            jobs.upsert({"id": _remote_job, **fields}, server=True)
        except Exception:  # noqa: BLE001 — reporting is never authoritative
            pass

    def _open_remote_job() -> None:
        # Same title convention as the local rows (supervisor._start_render):
        # the PROMPT, trimmed and capped at 80 chars, falling back to the
        # model when there is no usable prompt (unreachable today — `prompt`
        # is already validated non-empty above this branch — but written the
        # same way so the two shapes cannot drift). The model rides its own
        # field (jobs.py `Job.model`) — a dimmed suffix JobRow draws after the
        # title, same as a local row's — so the detail line is free to say
        # only "Claude — remote", which is the one thing this row exists to
        # say that a local row's detail never does.
        title = str(prompt or model).strip() or model
        _report_remote(title=title[:80], model=model, state="running", kind="task",
                       cancellable=False, detail=_REMOTE_ROW_DETAIL)

    def _finish_remote_job() -> None:
        """Success only: drop the row immediately rather than leaving it at
        a "done" state for the 3s sweep to clear later. `jobs.dismiss`, not a
        terminal `_report_remote(state="done")` left sitting — the whole
        point is that the corner shows nothing at all once the call
        succeeded. Safe to poison this id into `jobs._dismissed`: it is a
        fresh uuid4 per call (see `_remote_job` above), never reused, unlike
        the local rows' deterministic `job_id_for(model)`."""
        _report_remote(state="done")  # dismiss() refuses a still-running row
        jobs.dismiss(_remote_job)

    def _start_remote_beat():
        """Start the row's display heartbeat; returns the task that runs it.

        `_REMOTE_TICK_S` says why a row with nothing new to report still has
        to report. This carries NO FIELDS, which is the whole design of it:
        `jobs.upsert` applies only the keys present, so an id and nothing else
        is precisely "still here" and is incapable of saying anything more —
        it cannot move a bar, cannot overwrite the detail `run_once` sets
        while this call is parked in the queue, and is not an *opening* report
        (jobs.py reopens a dismissed or forgotten id only for a report that
        states `running` outright), so it can never blink a closed row back
        onto the screen. `schedule._report` documents the same fieldless call
        for the same registry.

        A tick after the outcome is already reported is impossible rather than
        merely unlikely: `_remote_job_closed` is set by the terminal report
        itself, and is re-read here after every sleep."""
        async def beat() -> None:
            while True:
                await asyncio.sleep(_REMOTE_TICK_S)
                if _remote_job_closed:
                    return
                _report_remote()

        return asyncio.ensure_future(beat())

    async def _stop_remote_beat(beat) -> None:
        """Stop the heartbeat and await it, so the task cannot outlive the
        request that owns the row. Not load-bearing for correctness — the
        `_remote_job_closed` re-read above is what stops a late tick — but a
        cancelled generator whose beat kept sleeping would be a task leaked
        per abandoned stream."""
        beat.cancel()
        try:
            await beat
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

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

        # Contended: this call has not begun, and a row reading exactly like
        # one that is generating is the row lying by omission — the same fix
        # supervisor's `_QUEUED_DETAIL` is for the transcription queue, at the
        # one other place in the app that parks work behind a lock. Probed
        # rather than acquired-non-blockingly because `asyncio.Lock` has no
        # such acquire; a stale read only mis-labels a row for one tick, which
        # is the whole stake of a detail line.
        queued = _AI_SESSION.lock.locked()
        if queued:
            _report_remote(detail=_REMOTE_QUEUED_DETAIL)
        async with _AI_SESSION.lock:
            if queued:
                _report_remote(detail=_REMOTE_ROW_DETAIL)
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
        _open_remote_job()
        beat = _start_remote_beat()
        try:
            try:
                data = await run_once()
            except asyncio.TimeoutError:
                _report_remote(state="error",
                               message=f"timed out after {_AI_TIMEOUT_S:.0f}s")
                return _ai_failed(
                    model, "timeout",
                    f"claude CLI did not answer within {_AI_TIMEOUT_S:.0f}s")
            except OSError as exc:
                _report_remote(state="error", message=str(exc))
                return _ai_failed(
                    model, "ai_unavailable",
                    f"could not run the claude CLI: {exc}")
            except _AiProcFailure as exc:
                _report_remote(state="error", message=str(exc))
                return _ai_failed(model, "ai_error", str(exc))
            payload, err = _ai_result_payload(data, model)
            if err is not None:
                _report_remote(state="error", message=err)
                return _ai_failed(model, "ai_error", err)
            # Under the RESOLVED id, not the alias the caller sent: "opus" and
            # "claude-opus-5" are one model and must not be two rows in the
            # breakdown. `_ai_result_payload` already did that resolution.
            ai_metrics.record(payload["model"], payload["usage"],
                              _claude_seconds(data))
            _finish_remote_job()
            return JSONResponse({"ok": True, "result": payload})
        except asyncio.CancelledError:
            # A genuine cancellation — the client went away mid-call. The
            # only exit that is honestly "cancelled".
            _report_remote(state="cancelled")
            raise
        except Exception:
            # Anything else escaping the block above (a bug in
            # `_ai_result_payload`, or any other unhandled exception) is a
            # real server error, not something the user did — reporting it
            # as "cancelled" would tell the corner a lie about what
            # happened. Let it keep propagating (still a 500) after marking
            # the row accordingly.
            _report_remote(state="error", message="internal error")
            raise
        finally:
            # The heartbeat first, so nothing is still ticking a row this
            # block is about to close.
            await _stop_remote_beat(beat)
            # Belt-and-suspenders: anything that reaches here without one of
            # the terminal reports above closes the row as cancelled rather
            # than leaving it running forever. A no-op once a real terminal
            # state (done/error/cancelled) was already reported, thanks to
            # `_report_remote`'s own dedup.
            _report_remote(state="cancelled")

    # Streaming: NDJSON over a chunked 200. Anything that goes wrong after
    # the first chunk left the wire cannot change the status code, so errors
    # become the terminal done frame instead.
    async def ndjson():
        # First action, not before the StreamingResponse is constructed: this
        # generator only starts running once Starlette actually sends the
        # body (its first `__anext__`), so a client that disconnects before
        # that point never runs this line — and, symmetrically, never runs
        # the `finally` below either. Opening the row out here rather than
        # before `return StreamingResponse(...)` keeps those two facts in
        # sync: no code path can create a row without something guaranteed
        # to run also being on the hook to close it.
        _open_remote_job()
        beat = _start_remote_beat()
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
                _report_remote(
                    state="error",
                    message=f"timed out after {_AI_TIMEOUT_S:.0f}s")
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "timeout",
                    "message": "claude CLI did not answer within "
                               f"{_AI_TIMEOUT_S:.0f}s"}}) + "\n"
                return
            except OSError as exc:
                ai_metrics.record_failure(model, "ai_unavailable")
                _report_remote(state="error", message=str(exc))
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_unavailable",
                    "message": f"could not run the claude CLI: {exc}"}}) + "\n"
                return
            except _AiProcFailure as exc:
                ai_metrics.record_failure(model, "ai_error")
                _report_remote(state="error", message=str(exc))
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_error", "message": str(exc)}}) + "\n"
                return
            payload, err = _ai_result_payload(data, model)
            if err is not None:
                ai_metrics.record_failure(model, "ai_error")
                _report_remote(state="error", message=err)
                yield json.dumps({"type": "done", "ok": False, "error": {
                    "type": "ai_error", "message": err}}) + "\n"
                return
            ai_metrics.record(payload["model"], payload["usage"],
                              _claude_seconds(data))
            _finish_remote_job()
            yield json.dumps(
                {"type": "done", "ok": True, "result": payload}) + "\n"
        except asyncio.CancelledError:
            # A genuine cancellation — the client went away mid-stream. The
            # only exit that is honestly "cancelled".
            _report_remote(state="cancelled")
            raise
        except Exception:
            # Anything else escaping the block above is a real server bug,
            # not something the user did — reporting it as "cancelled" would
            # tell the corner a lie about what happened.
            _report_remote(state="error", message="internal error")
            raise
        finally:
            await _stop_remote_beat(beat)
            if not task.done():
                # Client went away mid-stream: cancel AND await, so
                # run_once's cancel branch (discard the now-mid-turn
                # instance) actually runs before the generator is dropped.
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            # Belt-and-suspenders, same reasoning as the non-streaming
            # branch's own finally: any exit that reached here without one of
            # the terminal reports above (mid-stream disconnect, an
            # unexpected exception) still closes the row rather than leaving
            # it running forever. `_report_remote`'s own dedup makes this a
            # no-op once a real terminal state was already reported.
            _report_remote(state="cancelled")

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
#
# A fresh `_AiSession` is built here rather than reusing the module-level
# one: `_AiSession.lock` is an `asyncio.Lock`, which binds itself to
# whichever event loop first contends it and never rebinds. `prewarm_ai()`
# is an `@on_startup` hook, so every app build touches its session's lock;
# reusing one instance across two independent app builds (each with its own
# event loop, e.g. two `TestClient(create_app(...))` blocks in one process)
# risks a `RuntimeError: ... is bound to a different event loop` the moment
# that lock is ever contended in one of those builds. A fresh session per
# app keeps a build's lock scoped to only its own lifespan's loop.
def prewarm_ai():
    global _AI_SESSION
    _AI_SESSION = _AiSession()
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
