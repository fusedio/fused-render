"""One tool-less, session-free text completion from an agent CLI — GitHub
Copilot, OpenAI Codex or Google Gemini — the way the session recaps have
always run them, lifted here so `/api/ai` can run the same thing (D1433).

Two callers, one argv per backend:

* `/api/ai` (`server/ai.py::_agent_relay`): `provider: "copilot" | "codex" |
  "gemini"` on `fused.ai.text`, and the embedded workbench's inline AI edit
  relayed by the canvas host. Runs `run_async` so a client that goes away
  mid-stream kills the CLI instead of paying for an answer nobody reads.
* The session recap (`routers/claude_sessions.py::_recap_copilot` and its
  twins): `run_sync`, blocking, with the recap's own short timeout.

What every spec keeps, whichever caller (the recap runners' hard-won facts;
each flag was checked against the real binary when it was first written —
docs/COPILOT_BACKEND.md, docs/CODEX_BACKEND.md, docs/GEMINI_BACKEND.md):

* **The prompt travels over stdin, never argv.** A system text leads the one
  user message with a blank line between — none of the three has a system
  prompt flag.
* **No tools, nothing from the server's cwd.** Copilot: an allow-list naming
  a tool that does not exist (an EMPTY `--available-tools=` is ignored and
  keeps every tool), no custom instructions, no built-in MCP servers, no
  ask-user. Codex: `-s read-only` in an EMPTY mkdtemp as `-C`, with
  `project_root_markers=[]` so it never climbs to a planted `/tmp/.git` +
  `AGENTS.md`. Gemini: a deny-all policy file (the read-only tools run
  without asking under `--approval-mode default`).
* **No session left behind.** Copilot has no `--no-session-persistence`, so
  the id is minted here and `~/.copilot/session-state/<id>` is removed in
  `finally`. Codex `--ephemeral` writes no rollout. Gemini records a chat
  file named by the `session_id` its JSON reply carries; that file is
  removed when the reply named it.
* **The spawn rule** (D277/D292): no Popen `cwd=` and `close_fds=False`
  (`claude_health.SUBPROCESS_KWARGS`) so CPython uses posix_spawn — a fork
  with libproj resident in the server dies SIGSEGV before exec. The CLIs are
  pointed at their folder with their own `-C` flag instead.
* **`agent._spawn_env(backend=…)`**: Claude's `CLAUDE_CODE_*` switches
  stripped, each CLI's own credential variables left as found, Gemini's
  `NO_BROWSER=1`.

Streaming: none of the three streams in this mode (`copilot -s` prints the
reply when it is done, `codex exec -o` writes a file, `gemini -o json` is one
object), so a caller that wants chunks gets the whole text once (D5 of the
gateway design). `codex exec --json` / `copilot --output-format json` are
NOT used until they have been checked against the real binaries.

Reasoning effort: Copilot takes `--reasoning-effort <level>` (verified 1.0.102:
none, minimal, low, medium, high, xhigh, max) except for the models its
`models.list` marks `reasoning: false` (the registry's `Model.reasoning`,
`agent._COPILOT_NO_EFFORT_MODELS`); Codex takes the `model_reasoning_effort`
config key on `-c` (ASSUMED from the Codex config reference — the binary is
not on the build machine; the session host sends `effort` on `turn/start`,
which is the same setting); Gemini has no effort control at all (D7 of its
contract). `prepare()` reports a dropped effort so the relay can say so in
`warnings[]` rather than fail or stay silent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from fused_render import claude_health

logger = logging.getLogger(__name__)

#: The backends this module runs. Claude is NOT one: `/api/ai` keeps its
#: persistent `_AiSession` (D168/D169) and the recap keeps its own
#: `claude -p` argv, both of which stream.
BACKENDS = ("copilot", "codex", "gemini")

#: `claude_health.SUBPROCESS_KWARGS` minus the text-mode keys: asyncio's
#: `create_subprocess_exec` refuses `text` / `encoding` / `errors` with a
#: ValueError (its pipes are bytes; `_decode` does the UTF-8 "replace" the
#: sync runner gets from the kwargs). `close_fds=False` — the posix_spawn
#: rule — is the part that must ride on both.
ASYNC_SUBPROCESS_KWARGS = {
    k: v for k, v in claude_health.SUBPROCESS_KWARGS.items()
    if k not in ("text", "encoding", "errors")}

#: How long a run killed at its timeout gets to be collected before the
#: caller gives up on it (a grandchild still holding stdout would otherwise
#: hang the request past the timeout this exists to keep; a zombie at worst).
REAP_TIMEOUT_S = 5.0

#: The one tool a Copilot run may use — a name no tool has. Verified live
#: against copilot 1.0.98 (2026-10-16): `--available-tools=` with an EMPTY
#: value is ignored and the run keeps every tool (it ran `cat` on a probe
#: file), while an allowlist naming only a tool that does not exist leaves the
#: model with none (no tool definitions in the session's events, and a prompt
#: asking it to read a file got "I don't have a tool available").
COPILOT_NO_TOOLS = "fused_recap_no_tools"

COPILOT_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: What `gemini -p` says when the whole prompt rides stdin: the CLI appends
#: the flag's text AFTER the stdin input, so the pointer closes the message.
GEMINI_POINTER = "Answer as the instructions above say."
#: Gemini's policy (`--policy <file>`): NO TOOL AT ALL. `--approval-mode
#: default` only decides what asks; the read-only tools (read_file, glob,
#: grep, web fetch) run without asking under it, so a prompt that carries an
#: instruction to read something could pull the server's cwd into the
#: model's context. One rule, every tool, the highest priority the engine
#: takes (<= 999, verified 0.65.0 — 1000 is refused as "tier overflow"); a
#: denied call answers the model with "denied by policy" and the run goes
#: on. Written once under fused's own home (the file must exist on disk for
#: the flag). The file keeps its original name so an upgrade finds and
#: refreshes the one the recap already wrote.
GEMINI_POLICY = (
    "# fused-render: agent one-shot runs take no tools (agent_oneshot.py).\n"
    "[[rule]]\n"
    'toolName = "*"\n'
    'decision = "deny"\n'
    "priority = 999\n")
GEMINI_POLICY_NAME = "gemini-recap-policy.toml"

#: The levels the Copilot CLI's `--reasoning-effort` takes (1.0.102 `--help`).
COPILOT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
#: The levels Codex's `model_reasoning_effort` takes (config reference).
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


class OneShotError(Exception):
    """A run that produced no text. `type_` is `/api/ai`'s error vocabulary:
    `ai_unavailable` (no binary, signed out), `ai_error` (the CLI failed or
    answered nothing usable), `timeout`."""

    def __init__(self, type_: str, message: str):
        super().__init__(message)
        self.type_ = type_


@dataclass
class OneShot:
    """One prepared run: argv + env + stdin, and how to read its answer."""
    backend: str
    model: str
    argv: list[str]
    env: dict
    stdin: str
    #: `subprocess.PIPE` or `subprocess.DEVNULL`, per stream.
    stdout: int
    stderr: int
    #: `(stdout_text, stderr_text, returncode) -> text`; raises OneShotError.
    parse: Callable[[str, str, int], str]
    #: Runs in `finally`, whatever happened (removes the session the CLI kept).
    cleanup: Callable[[], None] = lambda: None
    #: Why a requested effort was NOT passed, or "" when it was (or none asked).
    effort_dropped: str = ""
    #: Marker substrings the backend's registry row names for "signed out"
    #: and "limit hit" (`agent_backends.Backend.*_error_markers`), consulted
    #: by `parse` to type a failure.
    account_markers: tuple = field(default_factory=tuple)
    limit_markers: tuple = field(default_factory=tuple)
    #: Facts `parse` learned beside the text (Gemini's `stats`), for the
    #: relay's result frame.
    extra: dict = field(default_factory=dict)


def gemini_policy_path(home_dir: Optional[str] = None) -> str:
    """`<fused home>/gemini-recap-policy.toml`, written when missing or stale."""
    if home_dir is None:
        from fused_render.shell import storage
        home_dir = storage.home_dir()
    path = os.path.join(home_dir, GEMINI_POLICY_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == GEMINI_POLICY:
                return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A unique temp name per writer: two runs refreshing the file at once
    # must not race each other's rename (a shared `.tmp` would make the
    # second `os.replace` raise FileNotFoundError).
    fd, tmp = tempfile.mkstemp(prefix=GEMINI_POLICY_NAME + ".",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(GEMINI_POLICY)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _agent():
    """The agent module (bins, `_spawn_env`), or an `ai_unavailable` error
    when it will not import — the same answer every other reader gives."""
    from fused_render import project_queue
    mod = project_queue.agent_module()
    if mod is None:
        raise OneShotError("ai_unavailable", "the agent module is not available")
    return mod


def _bin(agent, backend: str) -> str:
    try:
        return {"copilot": agent._copilot_bin, "codex": agent._codex_bin,
                "gemini": agent._gemini_bin}[backend]()
    except OneShotError:
        raise
    except Exception as exc:  # noqa: BLE001 - the resolver's own "not found" text
        raise OneShotError("ai_unavailable", str(exc)) from exc


def _failure_type(text: str, spec: "OneShot") -> tuple[str, str]:
    """Type a non-zero exit from what the CLI said: signed out / no access
    reads as `ai_unavailable`, a limit as `ai_error` with the CLI's line,
    anything else `ai_error` with the last non-empty line."""
    low = (text or "").lower()
    for marker in spec.account_markers:
        if marker in low:
            return "ai_unavailable", f"{spec.backend} is signed out or has no access: {_last_line(text)}"
    for marker in spec.limit_markers:
        if marker in low:
            return "ai_error", f"{spec.backend} usage limit: {_last_line(text)}"
    line = _last_line(text)
    return "ai_error", (f"{spec.backend} CLI failed: {line}" if line
                        else f"{spec.backend} CLI failed with no output")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _markers(backend: str) -> tuple[tuple, tuple]:
    from fused_render.agent_backends import registry
    row = registry.get(backend)
    return tuple(row.account_error_markers), tuple(row.limit_error_markers)


def _copilot_effort_ok(model: str) -> bool:
    """Whether the Copilot CLI takes `--reasoning-effort` for `model`: the
    registry's row (discovered list first — the plan's own `models.list`
    says per model), and an id the catalog has never heard of is assumed
    to take it, as `agent._copilot_argv` assumes."""
    from fused_render.agent_backends import registry
    for m in registry.get("copilot").effective_models():
        if m.id == model:
            return bool(m.reasoning)
    return True


def prepare(backend: str, model: str, system: str, prompt: str,
            effort: Optional[str] = None, agent=None) -> OneShot:
    """Build the run. `model` empty means the backend's registry default.
    Raises OneShotError(ai_unavailable) when the binary cannot be resolved."""
    if backend not in BACKENDS:
        raise ValueError(f"not a one-shot backend: {backend!r}")
    agent = agent or _agent()
    if not model:
        from fused_render.agent_backends import registry
        model = registry.get(backend).default_model
    text = (system.strip() + "\n\n" + prompt) if system and system.strip() else prompt
    account, limit = _markers(backend)
    common = dict(backend=backend, model=model, stdin=text,
                  account_markers=account, limit_markers=limit)
    if backend == "copilot":
        return _prepare_copilot(agent, model, effort, common)
    if backend == "codex":
        return _prepare_codex(agent, model, effort, common)
    return _prepare_gemini(agent, model, effort, common)


def _prepare_copilot(agent, model: str, effort: Optional[str], common: dict) -> OneShot:
    """`-p` mode entered by a prompt on stdin, `-s` (the reply only, no
    stats), no tools, no repo instructions, no built-in MCP servers, nothing
    it could stop to ask. The session id is MINTED here (`--session-id`) and
    that one directory under `session-state` is removed afterwards, whatever
    the outcome — verified live to leave nothing else there. (Copilot's own
    `session-store.db` index still gets a row; that file is Copilot's.) The
    CLI is pointed at the temp dir with `-C`: a folder it already trusts, so
    no trust question can stall the run."""
    from fused_render import copilot_health

    sid = str(uuid.uuid4())
    state_dir = os.path.join(copilot_health.config_dir(), "session-state")
    argv = [_bin(agent, "copilot"), "-s", "--no-auto-update", "--model", model,
            f"--available-tools={COPILOT_NO_TOOLS}",
            "--no-custom-instructions", "--disable-builtin-mcps",
            "--no-ask-user", "--session-id", sid,
            "-C", tempfile.gettempdir()]
    dropped = ""
    if effort:
        if effort not in COPILOT_EFFORTS:
            dropped = f"the Copilot CLI takes no effort level {effort!r}"
        elif not _copilot_effort_ok(model):
            dropped = f"Copilot's {model!r} takes no reasoning effort"
        else:
            argv += ["--reasoning-effort", effort]

    def parse(stdout: str, stderr: str, rc: int) -> str:
        if rc != 0:
            raise OneShotError(*_failure_type(stderr or stdout, spec))
        if not stdout.strip():
            raise OneShotError("ai_error", "the Copilot CLI answered nothing")
        return stdout

    def cleanup() -> None:
        if COPILOT_SESSION_ID_RE.fullmatch(sid):
            shutil.rmtree(os.path.join(state_dir, sid), ignore_errors=True)

    spec = OneShot(argv=argv, env=agent._spawn_env(backend="copilot"),
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   parse=parse, cleanup=cleanup, effort_dropped=dropped, **common)
    return spec


def _prepare_codex(agent, model: str, effort: Optional[str], common: dict) -> OneShot:
    """`codex exec`, Codex's one-shot, never the app-server the sessions use.
    Flags, each checked against `codex exec --help` (0.163.0) when the recap
    was built: `--ephemeral` (no rollout under `<CODEX_HOME>/sessions/`, so
    nothing for `codex resume` to list), `-s read-only` (Codex has no "no
    tools" switch; the sandbox is the bound), `-C <empty mkdtemp>` +
    `--skip-git-repo-check` (not a git repo, and Codex refuses to run outside
    one without the flag), `-c project_root_markers=[]` (no climb to a
    planted `/tmp/.git`; Codex's `agents_md.rs`: "An empty marker list
    disables parent traversal"), `-o <file>` (the reply alone; stdout also
    carries the banner and progress), `-` (prompt from stdin)."""
    work_dir = tempfile.mkdtemp(prefix="fused-codex-oneshot-")
    out_path = os.path.join(work_dir, "reply.txt")
    argv = [_bin(agent, "codex"), "exec", "--skip-git-repo-check", "--ephemeral",
            "-s", "read-only", "-C", work_dir,
            "-c", "project_root_markers=[]",
            "-m", model]
    dropped = ""
    if effort:
        if effort in CODEX_EFFORTS:
            # TOML on the right-hand side, as `agent._codex_argv` spells its
            # own `-c` overrides.
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        else:
            dropped = f"Codex takes no effort level {effort!r}"
    argv += ["-o", out_path, "-"]

    def parse(stdout: str, stderr: str, rc: int) -> str:
        if rc != 0:
            raise OneShotError(*_failure_type(stderr or stdout, spec))
        try:
            with open(out_path, encoding="utf-8", errors="replace") as fh:
                reply = fh.read()
        except OSError as exc:
            raise OneShotError("ai_error", f"the Codex CLI wrote no reply: {exc}") from exc
        if not reply.strip():
            raise OneShotError("ai_error", "the Codex CLI answered nothing")
        return reply

    def cleanup() -> None:
        shutil.rmtree(work_dir, ignore_errors=True)

    spec = OneShot(argv=argv, env=agent._spawn_env(backend="codex"),
                   # stderr is read: `exec` prints its failure there.
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   parse=parse, cleanup=cleanup, effort_dropped=dropped, **common)
    return spec


def _prepare_gemini(agent, model: str, effort: Optional[str], common: dict) -> OneShot:
    """`gemini -p`, Gemini's headless one-shot, never the ACP server the
    sessions use. `-p <pointer>` enters headless mode (the flag's text is
    appended to the stdin input); `-o json` gives one object
    `{session_id, response, stats}` on stdout — a FAILED run's JSON (with
    `session_id` and `error`) goes to STDERR (verified 0.65.0, exit 41
    signed out); `--policy <deny-all>`, `--approval-mode default`,
    `--skip-trust` (a headless run in the server's untrusted cwd would
    otherwise die at startup with folder trust on). The chat file the run
    records is removed by the `session_id` the reply named."""
    argv = [_bin(agent, "gemini"), "-p", GEMINI_POINTER, "-o", "json",
            "--approval-mode", "default", "--skip-trust",
            "--policy", gemini_policy_path(),
            "-m", model]
    dropped = "the Gemini CLI has no reasoning effort control" if effort else ""
    found: dict = {}  # becomes `spec.extra`

    def parse(stdout: str, stderr: str, rc: int) -> str:
        body = None
        for text in (stdout, stderr):
            try:
                candidate = json.loads(text or "")
            except ValueError:
                continue
            if isinstance(candidate, dict):
                body = candidate
                break
        if isinstance(body, dict):
            found["sid"] = str(body.get("session_id") or "")
        if rc != 0 or (isinstance(body, dict) and body.get("error")):
            said = ""
            if isinstance(body, dict) and body.get("error"):
                err = body.get("error")
                said = (err.get("message") if isinstance(err, dict) else str(err)) or ""
            raise OneShotError(*_failure_type(said or stderr or stdout, spec))
        if not isinstance(body, dict):
            raise OneShotError("ai_error", "the Gemini CLI answered no JSON")
        reply = body.get("response")
        if not isinstance(reply, str) or not reply.strip():
            raise OneShotError("ai_error", "the Gemini CLI answered nothing")
        if isinstance(body.get("stats"), dict):
            found["stats"] = body["stats"]
        return reply

    def cleanup() -> None:
        sid = found.get("sid")
        if not sid:
            return
        # Lazily: `routers.tasks` is the module that knows Gemini's chat
        # file layout (`_gemini_chat_files`), and it imports widely.
        try:
            from fused_render.server.routers import tasks as tasks_mod
            paths = tasks_mod._gemini_chat_files(sid)
        except Exception:  # noqa: BLE001 - cleanup never fails the answer
            return
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    spec = OneShot(argv=argv, env=agent._spawn_env(backend="gemini"),
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   parse=parse, cleanup=cleanup, effort_dropped=dropped,
                   extra=found, **common)
    return spec


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def _reap_sync(proc) -> None:
    proc.kill()
    try:
        proc.communicate(timeout=REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning("a killed %s run (pid %s) did not exit in %ss",
                       "one-shot", proc.pid, REAP_TIMEOUT_S)


def run_sync(spec: OneShot, timeout: float) -> str:
    """Blocking run (the recap's). Raises OneShotError; `cleanup` always runs."""
    try:
        try:
            proc = subprocess.Popen(
                spec.argv, env=spec.env, stdin=subprocess.PIPE,
                stdout=spec.stdout, stderr=spec.stderr,
                **claude_health.SUBPROCESS_KWARGS)
        except OSError as exc:
            raise OneShotError("ai_unavailable",
                               f"could not run the {spec.backend} CLI: {exc}") from exc
        try:
            out, err = proc.communicate(input=spec.stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Killed rather than left to finish: the answer is already
            # unwanted, and an abandoned CLI would keep burning tokens.
            _reap_sync(proc)
            raise OneShotError(
                "timeout", f"the {spec.backend} CLI did not answer within {timeout:.0f}s")
        return spec.parse(_decode(out), _decode(err), proc.returncode)
    finally:
        spec.cleanup()


async def run_async(spec: OneShot, timeout: float,
                    on_spawn: Optional[Callable] = None) -> str:
    """The relay's run: an asyncio subprocess, so a cancelled caller (a
    streaming client that disconnected — `_ai_relay`'s `finally`) kills the
    CLI. `on_spawn(proc)` hands the process to the caller for its own
    cancel. Raises OneShotError; `cleanup` always runs."""
    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv, env=spec.env, stdin=asyncio.subprocess.PIPE,
                stdout=spec.stdout, stderr=spec.stderr,
                **ASYNC_SUBPROCESS_KWARGS)
        except OSError as exc:
            raise OneShotError("ai_unavailable",
                               f"could not run the {spec.backend} CLI: {exc}") from exc
        if on_spawn is not None:
            on_spawn(proc)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=spec.stdin.encode("utf-8")), timeout)
        except asyncio.TimeoutError:
            await _reap_async(proc)
            raise OneShotError(
                "timeout", f"the {spec.backend} CLI did not answer within {timeout:.0f}s")
        except asyncio.CancelledError:
            await _reap_async(proc)
            raise
        return spec.parse(_decode(out), _decode(err), proc.returncode)
    finally:
        spec.cleanup()


async def _reap_async(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), REAP_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - a zombie at worst
        logger.warning("a killed one-shot run (pid %s) did not exit in %ss",
                       proc.pid, REAP_TIMEOUT_S)
