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
_PASTE_START, _PASTE_END = b"\x1b[200~", b"\x1b[201~"
_SUBMIT_DELAY_S = 0.3
_WATCH_TICK_S = 0.4
_CONNECT_TIMEOUT_S = 120.0
_HOST_EXIT_WAIT_S = 3.0
_IDENTITY_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID")
_ROWS, _COLS, _TAIL_LINES = 40, 200, 12

BUSY = ("Claude is still replying in this chat. Wait for the reply to finish "
        "(or press Stop), then run /remote-control again.")
BUSY_TASKS = ("This chat has background tasks running, and Remote Control would end them. "
              "Wait for them to finish or stop them, then run /remote-control again.")
NOT_CONNECTED = "Remote Control is not connected yet."
HELD = "Remote Control is driving this chat. Send from the chat or claude.ai, or stop Remote Control first."
NO_LINK = "Remote Control did not connect within two minutes, so it was stopped."
STILL_EXITING = ("The chat's previous Claude process is still shutting down. "
                 "Try Remote Control again in a moment.")

_lock = threading.Lock()
_remotes: dict[str, "_Remote"] = {}
_STRIPES = [threading.RLock() for _ in range(64)]


class _Remote:
    def __init__(self, term: pty_session.PtySession):
        self.term, self.url, self.timed_out = term, "", False
        threading.Thread(target=self._watch, daemon=True, name="claude-remote-watch").start()

    def _watch(self) -> None:
        give_up = time.monotonic() + _CONNECT_TIMEOUT_S
        trusted = False
        while self.term.alive:
            if time.monotonic() > give_up:
                self.timed_out = True
                self.term.kill()
                return
            raw = _ANSI.sub(b"", self.term.scrollback())
            found = _URL.search(raw)
            if found:
                self.url = found.group().decode()
                return
            if not trusted and _TRUST_BYTES in raw.replace(b" ", b""):
                trusted = self._answer_trust()
            time.sleep(_WATCH_TICK_S)

    def _answer_trust(self) -> bool:
        screen = self.term.text(_ROWS)
        if _TRUST_PROMPT not in screen:
            return False
        chosen = next((line for line in reversed(screen.splitlines())
                       if line.lstrip().startswith(_SELECTED)), "")
        yes = _TRUST_PROMPT in chosen
        self.term.write(_ENTER if yes else _DOWN)
        return yes

    def status(self) -> dict:
        if self.term.alive:
            return {"state": "active" if self.url else "starting", "url": self.url,
                    "terminal_id": self.term.id}
        tail = self.term.text(_TAIL_LINES).strip()
        return {"state": "ended", "exit_code": self.term.exit_code,
                "tail": f"{NO_LINK}\n{tail}" if self.timed_out else tail}


@contextlib.contextmanager
def _locked(session_ids):
    ids = {sid for sid in session_ids if sid}
    with contextlib.ExitStack() as stack:
        for stripe in sorted({hash(sid) % len(_STRIPES) for sid in ids}):
            stack.enter_context(_STRIPES[stripe])
        yield ids


def holds(session_id: str) -> bool:
    remote = _remotes.get(session_id or "")
    return remote is not None and remote.term.alive


def admit(session_ids, fn, /, *args, **kwargs):
    with _locked(session_ids) as ids:
        if any(holds(sid) for sid in ids):
            return {"error": HELD}
        return fn(*args, **kwargs)


def admit_call(main, /, **kwargs):
    ids = [str(kwargs.get("session_id") or ""), *run_sessions(str(kwargs.get("run_id") or ""))]
    return admit(ids, main, **kwargs)


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
    return [sid for sid in (own_session(run_dir, meta), str(meta.get("resumed_from") or "")) if sid]


def _refusal(agent, file: str, session_id: str, name: str) -> str:
    if sys.platform == "win32":
        return "Remote Control runs on macOS and Linux only."
    if not file:
        return "missing target file (no _file param?)"
    if not session_id or agent._bad_id(session_id):
        return "Send a first message, then turn on Remote Control."
    if name.startswith("-"):
        return "A Remote Control name can't start with '-'."
    hint = agent._session_backend(session_id)
    if agent._backend_for_session(session_id, agent._workdir(file), hint=hint) != "claude":
        return "Remote Control is a Claude Code feature; this chat runs on another agent."
    try:
        auth = claude_health._auth_status(agent._claude_bin())
    except FileNotFoundError as exc:
        return str(exc)
    if auth is not None and not auth.get("loggedIn"):
        return "Sign in to Claude Code first: Remote Control needs a claude.ai account."
    if auth is not None and auth.get("authMethod") != "claude.ai":
        return ("Remote Control needs a claude.ai subscription login, and Claude Code "
                f"here signs in with {auth.get('authMethod') or 'an API key'}.")
    return ""


def _running(agent, run_dir: str) -> bool:
    return agent._alive(run_dir) or agent._host_alive(run_dir)


def _busy(agent, run_dir: str) -> str:
    turn_open, tasks_pending = agent._turn_state(run_dir)
    try:
        queued = any(n.endswith(".json") for n in os.listdir(agent._inbox_dir(run_dir)))
    except OSError:
        queued = False
    return BUSY if turn_open or queued else BUSY_TASKS if tasks_pending else ""


def _end_session_runs(agent, session_id: str) -> str:
    try:
        names = os.listdir(agent.RUNS)
    except OSError:
        names = []
    runs = [os.path.join(agent.RUNS, name) for name in names
            if _running(agent, os.path.join(agent.RUNS, name)) and session_id in run_sessions(name)]
    busy = next((why for why in (_busy(agent, r) for r in runs) if why), "")
    if busy:
        return busy
    for run_dir in runs:
        agent._kill_tree(run_dir)
    deadline = time.monotonic() + _HOST_EXIT_WAIT_S
    while any(_running(agent, r) for r in runs) and time.monotonic() < deadline:
        time.sleep(0.05)
    return STILL_EXITING if any(_running(agent, r) for r in runs) else ""


def _profile(agent, file: str, session_id: str, name: str) -> TerminalProfile:
    binary = agent._claude_bin()
    env = agent._spawn_env("", "claude")
    for key in _IDENTITY_ENV:
        env.pop(key, None)
    path = [agent._fused_cli_dir(), env.get("PATH", "")]
    env.update(TERM="xterm-256color", COLORTERM="truecolor",
               PATH=os.pathsep.join(p for p in path if p))
    argv = [binary, *agent._plugin_argv(file), "--resume", session_id,
            "--remote-control", *([name] if name else [])]
    return TerminalProfile(shell=binary, argv=argv, env=env, cwd=agent._workdir(file))


def status(session_id: str) -> dict:
    with _lock:
        remote = _remotes.get(session_id or "")
        if remote is None:
            return {"state": "off"}
        out = remote.status()
        if out["state"] == "ended":
            _remotes.pop(session_id, None)
        return out


def start(file: str, session_id: str, name: str = "") -> dict:
    agent = claude_agent.agent_module()
    file, name = os.path.abspath(file) if file else "", " ".join(name.split())
    refused = _refusal(agent, file, session_id, name)
    if refused:
        return {"error": refused}
    with _locked([session_id]):
        if not holds(session_id):
            refused = _end_session_runs(agent, session_id)
            if refused:
                return {"error": refused}
            try:
                term = pty_session.REGISTRY.spawn(_profile(agent, file, session_id, name),
                                                  rows=_ROWS, cols=_COLS)
            except (pty_session.SessionLimitError, RuntimeError, OSError) as exc:
                return {"error": str(exc)}
            with _lock:
                _remotes[session_id] = _Remote(term)
    return status(session_id)


def say(session_id: str, text: str) -> dict:
    remote = _remotes.get(session_id or "")
    if remote is None or not remote.term.alive or not remote.url:
        return {"error": NOT_CONNECTED}
    remote.term.write(_PASTE_START + text.encode() + _PASTE_END)
    threading.Timer(_SUBMIT_DELAY_S, remote.term.write, args=(_ENTER,)).start()
    return {"sent": True}


def stop(session_id: str) -> dict:
    with _locked([session_id]):
        with _lock:
            remote = _remotes.pop(session_id or "", None)
        if remote is not None:
            remote.term.kill()
    return {"state": "off"}
