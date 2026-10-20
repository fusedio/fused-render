from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
import time

from fused_render import claude_agent, claude_health, pty_session
from fused_render.claude_cmd_log import _ANSI
from fused_render.terminal_profiles import TerminalProfile

_URL = re.compile(rb"https://claude\.ai/code/session_[A-Za-z0-9_-]+")
_TRUST_PROMPT = "I trust this folder"
_TRUST_BYTES = _TRUST_PROMPT.replace(" ", "").encode()
_SELECTED = "❯"
_DOWN, _ENTER = b"\x1b[B", b"\r"
_DOWN_GAP_S = 0.5
_WATCH_TICK_S, _WATCH_SLOW_TICK_S, _WATCH_FAST_FOR_S = 0.25, 2.0, 30.0
_HOST_EXIT_WAIT_S = 3.0
_CONNECT_TIMEOUT_S = 120.0
_IDENTITY_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID")
_ROWS, _COLS = 40, 200
_TAIL_LINES = 12

BUSY = ("Claude is still working in this chat. Wait for the turn to finish "
        "or stop it, then turn on Remote Control.")
HELD = "Remote Control is driving this chat. Stop it to type here."
NO_LINK = "Remote Control did not connect within two minutes, so it was stopped."
STILL_EXITING = ("The chat's previous Claude process is still shutting down. "
                 "Try Remote Control again in a moment.")

_lock = threading.RLock()
_held: dict[str, dict] = {}
_session_locks: dict[str, list] = {}


@contextlib.contextmanager
def _session_lock(session_id: str):
    with _lock:
        entry = _session_locks.setdefault(session_id, [threading.RLock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _lock:
            entry[1] -= 1
            if not entry[1]:
                _session_locks.pop(session_id, None)


def run_sessions(run_id: str) -> list:
    agent = claude_agent.agent_module()
    runs = getattr(agent, "RUNS", None)
    bad_id = getattr(agent, "_bad_id", None)
    own_session = getattr(agent, "_run_own_session", None)
    if not (run_id and runs and bad_id and own_session) or bad_id(run_id):
        return []
    run_dir = os.path.join(runs, run_id)
    try:
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(meta, dict):
        return []
    found = (own_session(run_dir, meta), str(meta.get("resumed_from") or ""))
    return sorted({sid for sid in found if sid})


def admit_call(main, /, **kwargs):
    ids = [str(kwargs.get("session_id") or ""), *run_sessions(str(kwargs.get("run_id") or ""))]
    return admit(ids, main, **kwargs)


def admit(session_ids, fn, /, *args, **kwargs):
    ids = sorted({sid for sid in session_ids if sid})
    with contextlib.ExitStack() as stack:
        for sid in ids:
            stack.enter_context(_session_lock(sid))
        if any(holds(sid) for sid in ids):
            return {"error": HELD}
        return fn(*args, **kwargs)


def _env(agent) -> dict:
    env = agent._spawn_env("", "claude")
    for name in _IDENTITY_ENV:
        env.pop(name, None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    cli_dir = agent._fused_cli_dir()
    if cli_dir:
        env["PATH"] = os.pathsep.join([cli_dir, env.get("PATH", "")])
    return env


def _login_refusal(binary: str) -> str:
    auth = claude_health._auth_status(binary)
    if auth is None:
        return ""
    if not auth.get("loggedIn"):
        return "Sign in to Claude Code first: Remote Control needs a claude.ai account."
    if auth.get("authMethod") != "claude.ai":
        return ("Remote Control needs a claude.ai subscription login, and Claude Code "
                f"here signs in with {auth.get('authMethod') or 'an API key'}.")
    return ""


def _busy(agent, run_dir: str) -> bool:
    turn_open, tasks_pending = agent._turn_state(run_dir)
    if turn_open or tasks_pending:
        return True
    try:
        return any(n.endswith(".json") for n in os.listdir(agent._inbox_dir(run_dir)))
    except OSError:
        return False


def _running(agent, run_dir: str) -> bool:
    return agent._alive(run_dir) or agent._host_alive(run_dir)


def _live_session_runs(agent, session_id: str) -> list:
    try:
        names = os.listdir(agent.RUNS)
    except OSError:
        return []
    return [os.path.join(agent.RUNS, name) for name in names
            if _running(agent, os.path.join(agent.RUNS, name))
            and session_id in run_sessions(name)]


def _end_session_runs(agent, session_id: str) -> str:
    runs = _live_session_runs(agent, session_id)
    if any(_busy(agent, run_dir) for run_dir in runs):
        return BUSY
    for run_dir in runs:
        agent._kill_tree(run_dir)
    deadline = time.monotonic() + _HOST_EXIT_WAIT_S
    while (any(_running(agent, run_dir) for run_dir in runs)
           and time.monotonic() < deadline):
        time.sleep(0.05)
    return STILL_EXITING if any(_running(agent, run_dir) for run_dir in runs) else ""


def _watch(rec: dict) -> None:
    term = rec["term"]
    fast_until = time.monotonic() + _WATCH_FAST_FOR_S
    give_up = time.monotonic() + _CONNECT_TIMEOUT_S
    last_down = 0.0
    while term.alive:
        if time.monotonic() > give_up:
            rec["timed_out"] = True
            term.kill()
            return
        raw = _ANSI.sub(b"", term.scrollback())
        found = _URL.search(raw)
        if found:
            rec["url"] = found.group().decode()
            return
        if not rec["trusted"] and _TRUST_BYTES in raw.replace(b" ", b""):
            screen = term.text(_ROWS)
            if _TRUST_PROMPT in screen:
                chosen = next((line for line in reversed(screen.splitlines())
                               if line.lstrip().startswith(_SELECTED)), "")
                if _TRUST_PROMPT in chosen:
                    term.write(_ENTER)
                    rec["trusted"] = True
                elif time.monotonic() - last_down >= _DOWN_GAP_S:
                    term.write(_DOWN)
                    last_down = time.monotonic()
        time.sleep(_WATCH_TICK_S if time.monotonic() < fast_until else _WATCH_SLOW_TICK_S)


def holds(session_id: str) -> bool:
    rec = _held.get(session_id or "")
    return rec is not None and rec["term"].alive


def status(session_id: str) -> dict:
    with _lock:
        rec = _held.get(session_id or "")
        if rec is None:
            return {"state": "off"}
        term = rec["term"]
        if not term.alive:
            _held.pop(session_id, None)
            tail = term.text(_TAIL_LINES).strip()
            return {"state": "ended", "exit_code": term.exit_code,
                    "tail": f"{NO_LINK}\n{tail}" if rec.get("timed_out") else tail}
        return {"state": "active" if rec["url"] else "starting",
                "url": rec["url"], "terminal_id": term.id}


def start(file: str, session_id: str, name: str = "") -> dict:
    if sys.platform == "win32":
        return {"error": "Remote Control runs on macOS and Linux only."}
    if not file:
        return {"error": "missing target file (no _file param?)"}
    agent = claude_agent.agent_module()
    if not session_id or agent._bad_id(session_id):
        return {"error": "Send a first message, then turn on Remote Control."}
    name = " ".join(name.split())
    if name.startswith("-"):
        return {"error": "A Remote Control name can't start with '-'."}
    file = os.path.abspath(file)
    workdir = agent._workdir(file)
    backend = agent._backend_for_session(session_id, workdir,
                                         hint=agent._session_backend(session_id))
    if backend != "claude":
        return {"error": "Remote Control is a Claude Code feature; this chat runs on another agent."}
    if holds(session_id):
        return status(session_id)
    try:
        binary = agent._claude_bin()
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    refused = _login_refusal(binary)
    if refused:
        return {"error": refused}
    with _session_lock(session_id):
        if holds(session_id):
            return status(session_id)
        try:
            refused = _end_session_runs(agent, session_id)
            if refused:
                return {"error": refused}
            argv = [binary, *agent._plugin_argv(file), "--resume", session_id,
                    "--remote-control", *([name] if name else [])]
            profile = TerminalProfile(shell=binary, argv=argv, env=_env(agent), cwd=workdir)
            term = pty_session.REGISTRY.spawn(profile, rows=_ROWS, cols=_COLS)
        except (pty_session.SessionLimitError, RuntimeError, OSError) as exc:
            return {"error": str(exc)}
        rec = {"term": term, "url": "", "trusted": False}
        with _lock:
            _held[session_id] = rec
    threading.Thread(target=_watch, args=(rec,), daemon=True,
                     name="claude-remote-watch").start()
    return status(session_id)


def stop(session_id: str) -> dict:
    if not session_id:
        return {"state": "off"}
    with _session_lock(session_id):
        rec = _held.get(session_id)
        if rec is not None:
            rec["term"].kill()
        with _lock:
            _held.pop(session_id, None)
    return {"state": "off"}
