"""Reader for the per-chat command log the shell-prefix wrapper writes.

`claude_shell_prefix.sh` (D1327) mirrors every Bash-tool command a chat runs
into `<root>/<chat id>/<cmd id>.{cmd,meta,out,exit}`. This module is the other
half: it turns that directory into the byte stream the drawer's read-only
"Claude" tab shows, lists the chats that have one, and stops the commands still
running. Stdlib only; no import of agent.py (it is a standalone template), so
`root()` repeats `agent._runs_root`'s formula and a test pins the two equal.

Session ids on the terminal routes are `claude:<chat id>`.
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time

PREFIX = "claude:"
# A chat's tab disappears from the list this long after its last command wrote
# anything, and the list is capped, so old chats do not pile up as tabs.
ACTIVE_WINDOW_SECONDS = 2 * 3600
MAX_CHATS = 8

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
_BARE_LF = re.compile(rb"(?<!\r)\n")
_EVAL = re.compile(r"&& eval (.*?) < /dev/null && pwd -P >\|", re.S)


def root() -> str:
    geteuid = getattr(os, "geteuid", None)
    suffix = "-%d" % geteuid() if geteuid is not None else ""
    return os.path.join(tempfile.gettempdir(), "fused_render_claude" + suffix,
                        "claude-cmds")


def session_id(chat: str) -> str:
    return PREFIX + chat


def chat_of(sid: str) -> str | None:
    """The chat id behind a `claude:<chat>` session id, or None."""
    if not sid.startswith(PREFIX):
        return None
    chat = sid[len(PREFIX):]
    return None if bad_chat(chat) else chat


def bad_chat(chat: str) -> bool:
    return not chat or chat.startswith(".") or any(c in chat for c in "/\\:")


def chat_dir(chat: str) -> str:
    return os.path.join(root(), chat)


def readable_command(raw: str) -> str:
    """The user-facing command inside the Bash-tool wrapper string: the
    (shell-quoted) argument of `eval`. Falls back to the raw string."""
    m = _EVAL.search(raw)
    if m:
        try:
            parts = shlex.split(m.group(1))
        except ValueError:
            return raw
        if parts:
            return " ".join(parts)
    return raw


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _read_from(path: str, offset: int) -> bytes:
    """The bytes of `path` from `offset` on (only those are read)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return b""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Cmd:
    def __init__(self, chat: str, ident: str):
        self.ident = ident
        self.base = os.path.join(chat_dir(chat), ident)
        meta = {}
        for line in _read(self.base + ".meta").decode("utf-8", "replace").splitlines():
            k, _, v = line.partition("=")
            meta[k] = v
        self.pid = int(meta["pid"]) if meta.get("pid", "").isdigit() else 0
        self.pgid = int(meta["pgid"]) if meta.get("pgid", "").isdigit() else 0
        self.start = int(meta["start"]) if meta.get("start", "").isdigit() else 0
        self.raw = _read(self.base + ".cmd").decode("utf-8", "replace")

    @property
    def exit_code(self) -> int | None:
        text = _read(self.base + ".exit").decode().strip()
        try:
            return int(text)
        except ValueError:
            return None

    @property
    def running(self) -> bool:
        """No footer yet and the wrapper process is still there. pid 0 (no
        .meta read; only an older file layout can show that) counts as running
        until an .exit appears."""
        return self.exit_code is None and (self.pid == 0 or _pid_alive(self.pid))

    def finished(self) -> bool:
        return not self.running


def command_ids(chat: str) -> list[str]:
    try:
        names = os.listdir(chat_dir(chat))
    except OSError:
        return []
    return sorted({n[:-4] for n in names if n.endswith(".cmd")})


def commands(chat: str) -> list[Cmd]:
    return [Cmd(chat, i) for i in command_ids(chat)]


def last_activity(chat: str) -> float:
    d = chat_dir(chat)
    newest = 0.0
    try:
        for n in os.listdir(d):
            try:
                newest = max(newest, os.stat(os.path.join(d, n)).st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return newest


def list_chats() -> list[dict]:
    """Chats with at least one logged command and recent activity, newest
    first, as terminal-list entries (kind "claude")."""
    out = []
    try:
        names = os.listdir(root())
    except OSError:
        return []
    now = time.time()
    for chat in names:
        if bad_chat(chat) or not command_ids(chat):
            continue
        act = last_activity(chat)
        running = any(c.running for c in commands(chat))
        if not running and now - act > ACTIVE_WINDOW_SECONDS:
            continue
        out.append({"chat": chat, "running": running, "lastActivity": act})
    out.sort(key=lambda e: e["lastActivity"], reverse=True)
    return out[:MAX_CHATS]


def _to_crlf(data: bytes) -> bytes:
    return _BARE_LF.sub(b"\r\n", data)


class Stream:
    """Incremental renderer: each `poll()` returns the bytes appended since the
    last one. A fresh Stream's first poll is the whole scrollback."""

    def __init__(self, chat: str):
        self.chat = chat
        self._state: dict[str, dict] = {}

    def poll(self) -> bytes:
        out = []
        for ident in command_ids(self.chat):
            st = self._state.setdefault(ident, {"hdr": False, "off": 0, "foot": False,
                                                "nl": True})
            if st["foot"]:
                continue
            cmd = Cmd(self.chat, ident)
            if not st["hdr"]:
                text = readable_command(cmd.raw).replace("\r\n", "\n").replace("\n", "\r\n")
                out.append(("\x1b[1;36m$ %s\x1b[0m\r\n" % text).encode())
                st["hdr"] = True
            # Check for completion BEFORE reading, so the final bytes are in
            # the read that precedes the footer.
            code = cmd.exit_code
            ended = code is not None or (cmd.pid != 0 and not _pid_alive(cmd.pid))
            data = _read_from(cmd.base + ".out", st["off"])
            if data:
                st["off"] += len(data)
                st["nl"] = data.endswith(b"\n")
                out.append(_to_crlf(data))
            if ended:
                if not st["nl"]:
                    out.append(b"\r\n")
                if code != 0:
                    label = "exit %d" % code if code is not None else "ended"
                    out.append(("\x1b[31m[%s]\x1b[0m\r\n" % label).encode())
                st["foot"] = True
        return b"".join(out)

    def running(self) -> bool:
        return any(c.running for c in commands(self.chat))


def render_text(chat: str, lines: int) -> str:
    raw = Stream(chat).poll()
    text = _ANSI.sub(b"", raw).decode("utf-8", "replace").replace("\r\n", "\n")
    rows = text.rstrip("\n").split("\n")
    return "\n".join(rows[-lines:])


def _ps_exe() -> str:
    """Absolute ps path. Runs inside the server process, where fork() with
    libproj resident SIGSEGVs: a bare name or close_fds=True or cwd= forces the
    fork+exec path, so use the posix_spawn-safe shape (see pty_session._process_name)."""
    return "/bin/ps" if os.path.exists("/bin/ps") else "/usr/bin/ps"


def _descendants(pid: int) -> list[int]:
    try:
        res = subprocess.run([_ps_exe(), "-A", "-o", "pid=,ppid="], capture_output=True,
                             text=True, timeout=5, close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return []
    kids: dict[int, list[int]] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            found.append(k)
            todo.append(k)
    return found


def _is_wrapper(pid: int) -> bool:
    """Guard against a recycled pid: the live process must be our wrapper."""
    try:
        res = subprocess.run([_ps_exe(), "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude_shell_prefix" in res.stdout


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def stop(chat: str, grace: float = 3.0) -> list[int]:
    """Stop the chat's currently running commands: TERM the wrapper's process
    TREE (not its pgid: that group may be shared with the claude CLI itself),
    then KILL whatever ignored it after `grace` seconds. Returns the pids of the
    commands (wrapper pids) that were running."""
    stopped, victims = [], []
    for cmd in commands(chat):
        if not cmd.running or not _is_wrapper(cmd.pid):
            continue
        tree = _descendants(cmd.pid)
        stopped.append(cmd.pid)
        victims.extend(tree)
        # Children first; the wrapper then notices and writes its own footer.
        for pid in (tree or [cmd.pid]):
            _signal(pid, signal.SIGTERM)

    if victims:
        def reap():
            time.sleep(grace)
            for pid in victims:
                if _pid_alive(pid):
                    _signal(pid, signal.SIGKILL)
        threading.Thread(target=reap, daemon=True).start()
    return stopped
