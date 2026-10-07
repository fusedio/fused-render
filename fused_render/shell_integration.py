"""Shell integration for the status-bar terminal: lets the server know what the
user's shell is doing (last command, its exit code, the live cwd) without
editing any of the user's dotfiles.

Two halves:

  * `integrate(profile)` rewrites a `TerminalProfile` for zsh and bash so the
    shell, once it has finished its own startup files, installs hooks that emit
    escape sequences into the output stream. Other shells (fish, sh, ...) are
    returned untouched; their fields simply stay null.
  * `ShellEventParser` reads those sequences back out of the pty output.
    The sequences are only OBSERVED: the reader never strips them from the
    stream, so xterm still receives them (and ignores the ones it does not
    know).

Sequences (VS Code's shell-integration vocabulary):

  * `OSC 133;A`        prompt is about to be drawn
  * `OSC 133;C`        a command starts running
  * `OSC 133;D;<n>`    the command finished with exit status n
  * `OSC 7;file://host/path`  the cwd, emitted at every prompt
  * `OSC 633;E;<text>` the command line, emitted from preexec just before 133;C.
                       `\\` and the bytes `;` ESC BEL LF are written as `\\\\`
                       and `\\xNN` so the text cannot terminate the sequence.

zsh: `ZDOTDIR` points at a shim directory whose `.zshenv`/`.zprofile`/`.zshrc`
source the user's real files from `$FUSED_USER_ZDOTDIR` (or `$HOME`), then
the final `.zshrc` installs the hooks and restores `ZDOTDIR`, so `.zlogin`,
the history file and any nested zsh see the user's own value.

bash: `--init-file <shim>`. `--rcfile`/`--init-file` are ignored by a LOGIN
bash, so on macOS (where the profile spawns `-l`) the `-l` is dropped and the
shim replays login semantics itself (`/etc/profile`, then the first of
`~/.bash_profile`, `~/.bash_login`, `~/.profile`) when `FUSED_SHELL_LOGIN` is
set — the same trade VS Code makes. `shopt login_shell` reads off as a result.
bash has no preexec, so the command line comes from a DEBUG trap plus
`history 1`.

The shim files are generated from the strings below into a per-user directory
under the system temp dir (not shipped as package data, so every bundle layout
gets them for free). Windows never reaches `integrate`: `resolve_profile`
returns None there.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import stat
import tempfile
import time
from typing import Optional

from fused_render.terminal_profiles import TerminalProfile

# -- the shims ----------------------------------------------------------------

_ZSHENV = r"""# fused-render shell integration shim (zsh). Sources the user's real file.
if [[ -f ${FUSED_USER_ZDOTDIR:-$HOME}/.zshenv ]]; then
  ZDOTDIR=${FUSED_USER_ZDOTDIR:-$HOME}
  builtin source "${FUSED_USER_ZDOTDIR:-$HOME}/.zshenv"
  # The user's .zshenv may itself have moved ZDOTDIR; honour that.
  FUSED_USER_ZDOTDIR=$ZDOTDIR
  ZDOTDIR=$FUSED_SHIM_DIR
fi
"""

_ZPROFILE = r"""# fused-render shell integration shim (zsh). Sources the user's real file.
if [[ -f ${FUSED_USER_ZDOTDIR:-$HOME}/.zprofile ]]; then
  ZDOTDIR=${FUSED_USER_ZDOTDIR:-$HOME}
  builtin source "${FUSED_USER_ZDOTDIR:-$HOME}/.zprofile"
  FUSED_USER_ZDOTDIR=$ZDOTDIR
  ZDOTDIR=$FUSED_SHIM_DIR
fi
"""

_ZSHRC = r"""# fused-render shell integration shim (zsh).
if [[ -f ${FUSED_USER_ZDOTDIR:-$HOME}/.zshrc ]]; then
  ZDOTDIR=${FUSED_USER_ZDOTDIR:-$HOME}
  builtin source "${FUSED_USER_ZDOTDIR:-$HOME}/.zshrc"
  FUSED_USER_ZDOTDIR=$ZDOTDIR
fi

__fr_precmd_first() {
  __fr_ec=$?
  builtin printf '\e]133;D;%s\a' "$__fr_ec"
  return $__fr_ec
}
__fr_precmd_last() {
  local p=${PWD//\%/%25}
  p=${p// /%20}
  builtin printf '\e]7;file://%s%s\a\e]133;A\a' "${HOST:-localhost}" "$p"
}
__fr_preexec() {
  local c=${1//\\/\\\\}
  c=${c//;/\\x3b}
  c=${c//$'\n'/\\x0a}
  c=${c//$'\e'/\\x1b}
  c=${c//$'\a'/\\x07}
  builtin printf '\e]633;E;%s\a\e]133;C\a' "$c"
}
precmd_functions=(__fr_precmd_first $precmd_functions __fr_precmd_last)
preexec_functions=($preexec_functions __fr_preexec)

# Give the user's own ZDOTDIR back (or none) so .zlogin, HISTFILE and nested
# shells behave as if this shim had never been there.
if [[ -z $FUSED_USER_ZDOTDIR || $FUSED_USER_ZDOTDIR == $HOME ]]; then
  unset ZDOTDIR
else
  ZDOTDIR=$FUSED_USER_ZDOTDIR
fi
unset FUSED_USER_ZDOTDIR FUSED_SHIM_DIR
"""

_BASHRC = r"""# fused-render shell integration shim (bash).
if [ -n "$FUSED_SHELL_LOGIN" ]; then
  [ -r /etc/profile ] && . /etc/profile
  for __fr_f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    if [ -r "$__fr_f" ]; then . "$__fr_f"; break; fi
  done
  unset __fr_f
else
  [ -r "$HOME/.bashrc" ] && . "$HOME/.bashrc"
fi
unset FUSED_SHELL_LOGIN

__fr_armed=0
__fr_pre() {
  __fr_ec=$?
  __fr_armed=0
  builtin printf '\e]133;D;%s\a' "$__fr_ec"
  return $__fr_ec
}
__fr_post() {
  local ec=$? p=${PWD//%/%25}
  p=${p// /%20}
  builtin printf '\e]7;file://%s%s\a\e]133;A\a' "${HOSTNAME:-localhost}" "$p"
  __fr_armed=1
  __fr_hist=$HISTCMD
  return $ec
}
__fr_debug() {
  [ "$__fr_armed" = 1 ] || return
  [ -z "$COMP_LINE" ] || return
  case "$BASH_COMMAND" in __fr_*) return ;; esac
  __fr_armed=0
  local c=
  # `history 1` is only this command when history advanced since the prompt;
  # an unsaved line (ignorespace/ignoredups, history off) would hand back the
  # PREVIOUS command, so fall back to $BASH_COMMAND then.
  if [ "$HISTCMD" != "$__fr_hist" ]; then
    c=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)
    c=${c#"${c%%[![:space:]]*}"}
    c=${c#*[[:space:]]}
  fi
  [ -n "${c//[[:space:]]/}" ] || c=$BASH_COMMAND
  c=${c//\\/\\\\}
  c=${c//;/\\x3b}
  c=${c//$'\n'/\\x0a}
  c=${c//$'\e'/\\x1b}
  c=${c//$'\a'/\\x07}
  builtin printf '\e]633;E;%s\a\e]133;C\a' "$c"
}
if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
  PROMPT_COMMAND=(__fr_pre "${PROMPT_COMMAND[@]}" __fr_post)
else
  PROMPT_COMMAND="__fr_pre"$'\n'"${PROMPT_COMMAND}"$'\n'"__fr_post"
fi
# Only claim the DEBUG trap when the user's rc files did not.
if [ -z "$(trap -p DEBUG)" ]; then
  trap '__fr_debug' DEBUG
fi
"""

_SHIM_FILES = {
    "zsh/.zshenv": _ZSHENV,
    "zsh/.zprofile": _ZPROFILE,
    "zsh/.zshrc": _ZSHRC,
    "bash/bashrc": _BASHRC,
}


_FALLBACK_ROOT: Optional[str] = None


def _shim_state(root: str, uid: int) -> str:
    """"ok" (trusted and complete), "missing", "stale" (ours but not usable:
    incomplete, tampered, wrong type or loose permissions: safe to delete) or
    "foreign" (owned by someone else: never use, never touch)."""
    try:
        st = os.lstat(root)
    except OSError:
        return "missing"
    posix = hasattr(os, "getuid")
    if posix and st.st_uid != uid:
        return "foreign"
    if not stat.S_ISDIR(st.st_mode) or (posix and st.st_mode & 0o022):
        return "stale"
    for rel, text in _SHIM_FILES.items():
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                if fh.read() != text:
                    return "stale"
        except (OSError, ValueError):
            return "stale"
    return "ok"


def _remove(path: str) -> None:
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def _write_shims(dest: str) -> None:
    for rel, text in _SHIM_FILES.items():
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def _fallback_shim_root(uid: int) -> str:
    """A private (mkdtemp, 0o700) per-process root, for when the predictable
    path is occupied by someone else's directory."""
    global _FALLBACK_ROOT
    if _FALLBACK_ROOT and _shim_state(_FALLBACK_ROOT, uid) == "ok":
        return _FALLBACK_ROOT
    root = tempfile.mkdtemp(prefix="fused-render-shell-")
    _write_shims(root)
    _FALLBACK_ROOT = root
    return root


def _shim_root() -> str:
    """The per-user shim directory, written once per content hash. Idempotent
    and race-tolerant (atomic rename), so two servers starting together agree.

    The predictable path lives in a shared temp dir on Linux, so an existing
    root is only trusted if it is a real directory owned by us, not writable
    by group/other, with every shim file byte-equal to what we would write."""
    digest = hashlib.sha1(
        "".join(f"{k}\0{v}\0" for k, v in sorted(_SHIM_FILES.items())).encode()
    ).hexdigest()[:10]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    root = os.path.join(tempfile.gettempdir(), f"fused-render-shell-{uid}-{digest}")
    state = _shim_state(root, uid)
    if state == "ok":
        return root
    if state == "foreign":
        return _fallback_shim_root(uid)
    if state == "stale":
        _remove(root)
    staging = f"{root}.tmp{os.getpid()}"
    _remove(staging)
    try:
        os.mkdir(staging, 0o700)
        _write_shims(staging)
    except OSError:
        _remove(staging)
        return _fallback_shim_root(uid)
    try:
        os.rename(staging, root)
    except OSError:
        # Another process won the race (root now exists and is complete).
        _remove(staging)
    if _shim_state(root, uid) == "ok":
        return root
    return _fallback_shim_root(uid)


def integrate(profile: TerminalProfile) -> TerminalProfile:
    """`profile` rewritten to load the shell-integration hooks, or `profile`
    itself for a shell this does not know."""
    name = os.path.basename(profile.shell)
    if "FUSED_SHIM_DIR" in profile.env or "--init-file" in profile.argv:
        return profile  # already integrated; twice would lose the user's ZDOTDIR
    if name == "zsh":
        shim = os.path.join(_shim_root(), "zsh")
        env = dict(profile.env)
        user = env.get("ZDOTDIR")
        if user:
            env["FUSED_USER_ZDOTDIR"] = user
        else:
            env.pop("FUSED_USER_ZDOTDIR", None)
        env["ZDOTDIR"] = shim
        env["FUSED_SHIM_DIR"] = shim
        return dataclasses.replace(profile, env=env)
    if name == "bash":
        rcfile = os.path.join(_shim_root(), "bash", "bashrc")
        env = dict(profile.env)
        rest = [a for a in profile.argv[1:] if a not in ("-l", "--login")]
        if len(rest) != len(profile.argv) - 1:
            env["FUSED_SHELL_LOGIN"] = "1"
        argv = [profile.argv[0], "--init-file", rcfile, *rest]
        return dataclasses.replace(profile, argv=argv, env=env)
    return profile


# -- the reader ---------------------------------------------------------------

_BEL = b"\x07"
_ST = b"\x1b\\"
#: A sequence that never terminates (binary output, a cut-off write) is
#: dropped once the pending tail passes this, so memory stays bounded.
_MAX_PENDING = 8192
_MAX_COMMAND = 4096


def _unescape_command(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == "x" and i + 3 < len(text) and text[i + 2:i + 4].isalnum():
                try:
                    out.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _decode_cwd(uri: str) -> Optional[str]:
    from urllib.parse import unquote
    if not uri.startswith("file://"):
        return None
    rest = uri[len("file://"):]
    slash = rest.find("/")
    if slash < 0:
        return None
    return unquote(rest[slash:])


class ShellEventParser:
    """Incremental reader for the sequences above. `feed()` takes raw pty
    chunks (split anywhere, even mid-sequence) and updates the public state:

      last_command  text of the most recent command that STARTED (133;C)
      last_exit     its exit status once it finished (133;D;n), else None
      running       True between 133;C and the matching 133;D
      cwd           from OSC 7, None until the first prompt
      last_command_at  wall-clock time the last command started
    """

    def __init__(self) -> None:
        self._tail = b""
        self._pending_command: Optional[str] = None
        self.last_command: Optional[str] = None
        self.last_exit: Optional[int] = None
        self.last_command_at: Optional[float] = None
        self.running = False
        self.cwd: Optional[str] = None

    def feed(self, data: bytes) -> None:
        buf = self._tail + data
        self._tail = b""
        pos = 0
        while True:
            start = buf.find(b"\x1b]", pos)
            if start < 0:
                if buf.endswith(b"\x1b"):
                    self._tail = b"\x1b"
                return
            bel = buf.find(_BEL, start + 2)
            st = buf.find(_ST, start + 2)
            ends = [(i, n) for i, n in ((bel, 1), (st, 2)) if i >= 0]
            if not ends:
                pending = buf[start:]
                if len(pending) <= _MAX_PENDING:
                    self._tail = pending
                return
            end, width = min(ends)
            self._handle(buf[start + 2:end])
            pos = end + width

    def _handle(self, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - decode with replace cannot raise
            return
        if text.startswith("133;"):
            kind, _, arg = text[4:].partition(";")
            if kind == "C":
                self.last_command = self._pending_command
                self._pending_command = None
                self.last_exit = None
                self.last_command_at = time.time()
                self.running = True
            elif kind == "D":
                if self.running:
                    self.running = False
                    try:
                        self.last_exit = int(arg)
                    except ValueError:
                        self.last_exit = None
        elif text.startswith("633;E;"):
            cmd = _unescape_command(text[6:]).strip()
            self._pending_command = cmd[:_MAX_COMMAND] or None
        elif text.startswith("7;"):
            cwd = _decode_cwd(text[2:])
            if cwd:
                self.cwd = cwd
