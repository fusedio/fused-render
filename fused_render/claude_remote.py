from __future__ import annotations

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
_SELECTED = "❯"
_DOWN, _ENTER = b"\x1b[B", b"\r"
_DOWN_GAP_S = 0.5
_WATCH_TICK_S, _WATCH_SLOW_TICK_S, _WATCH_FAST_FOR_S = 0.25, 2.0, 30.0
_HOST_EXIT_WAIT_S = 3.0
_IDENTITY_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID")
_ROWS, _COLS = 40, 200
_TAIL_LINES = 12

BUSY = ("Claude is still working in this chat. Wait for the turn to finish "
        "or stop it, then turn on Remote Control.")
HELD = "Remote Control is driving this chat. Stop it to type here."

_lock = threading.RLock()
_held: dict[str, dict] = {}


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


def _end_idle_host(agent, file: str, session_id: str) -> str:
    run_id = agent._live_host(file, session_id).get("run_id") or ""
    if not run_id:
        return ""
    run_dir = os.path.join(agent.RUNS, run_id)
    if _busy(agent, run_dir):
        return BUSY
    agent._kill_tree(run_dir)
    deadline = time.monotonic() + _HOST_EXIT_WAIT_S
    while agent._host_alive(run_dir) and time.monotonic() < deadline:
        time.sleep(0.05)
    return ""


def _watch(rec: dict) -> None:
    term = rec["term"]
    fast_until = time.monotonic() + _WATCH_FAST_FOR_S
    last_down = 0.0
    while term.alive:
        found = _URL.search(_ANSI.sub(b"", term.scrollback()))
        if found:
            rec["url"] = found.group().decode()
            return
        if not rec["trusted"]:
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
    return rec is not None and (rec["term"] is None or rec["term"].alive)


def status(session_id: str) -> dict:
    with _lock:
        rec = _held.get(session_id or "")
        if rec is None:
            return {"state": "off"}
        term = rec["term"]
        if term is None:
            return {"state": "starting", "url": "", "terminal_id": ""}
        if not term.alive:
            _held.pop(session_id, None)
            return {"state": "ended", "exit_code": term.exit_code,
                    "tail": term.text(_TAIL_LINES).strip()}
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
    with _lock:
        if holds(session_id):
            return status(session_id)
        rec = {"term": None, "url": "", "trusted": False}
        _held[session_id] = rec
        try:
            refused = _end_idle_host(agent, file, session_id)
            if not refused:
                argv = [binary, *agent._plugin_argv(file), "--resume", session_id,
                        "--remote-control", *([name] if name else [])]
                profile = TerminalProfile(shell=binary, argv=argv, env=_env(agent), cwd=workdir)
                rec["term"] = pty_session.REGISTRY.spawn(profile, rows=_ROWS, cols=_COLS)
        except (pty_session.SessionLimitError, RuntimeError, OSError) as exc:
            refused = str(exc)
        finally:
            if rec["term"] is None:
                _held.pop(session_id, None)
        if refused:
            return {"error": refused}
    threading.Thread(target=_watch, args=(rec,), daemon=True,
                     name="claude-remote-watch").start()
    return status(session_id)


def stop(session_id: str) -> dict:
    with _lock:
        rec = _held.get(session_id or "")
        if rec is not None and rec["term"] is not None:
            rec["term"].kill()
        _held.pop(session_id or "", None)
    return {"state": "off"}
