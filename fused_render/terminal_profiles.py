"""Resolve the user's shell into a spawn profile for the status-bar terminal.

One resolved profile, not a settings-file *list* of them — VS Code's
`terminal.integrated.profiles` earns its complexity by serving many shells;
this app targets macOS + Linux (see Task in PLAN-status-bar-terminal.md) and
`$SHELL` is right by default. Revisit if someone asks for fish-vs-zsh
switching.

Ports a rule already proven in `fused_render/claude_health.py` rather than
re-deriving it:

  * `$SHELL` or `/bin/bash` as the fallback (claude_health.py:281), checked
    with the same `executable()` used there so a non-executable value never
    gets spawned.

Deliberately does NOT scrub PYTHONHOME/PYTHONPATH here, unlike
claude_health.py:283. There, the immediate Popen target IS the shell, so
scrubbing in the env handed to Popen is correct. Here, `pty_session.py`'s
immediate Popen target is `sys.executable` running `_pty_exec_helper.py` — in
a packaged (py2app) build that IS the bundled interpreter, which per
`engine.py`'s documented failure mode needs PYTHONHOME to find its own
runtime; stripped, it reports the build machine's Homebrew framework as its
prefix and fails to start. The scrub belongs one process later, in the
helper, right before it execs the actual shell — see
`_pty_exec_helper.py`.

`-l` (login shell) is added on darwin only. macOS GUI apps inherit launchd's
environment, not the user's shell profile — PATH has none of nvm/volta/asdf's
shims, none of the aliases a user's .zshrc sets up. A login shell re-reads
that profile. This is exactly what VS Code's integrated terminal does on
macOS, and `claude_health._shell_rc` (claude_health.py:547) already documents
the same asymmetry for bash/zsh. Linux desktop sessions already export a
profile-shaped environment to GUI apps, so no `-l` there — matching VS Code's
own default.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from typing import Optional

from fused_render import _platform
from fused_render.claude_health import executable


@dataclasses.dataclass(frozen=True)
class TerminalProfile:
    """A fully resolved shell spawn: executable path, argv, env overlay, cwd.

    `env` is the COMPLETE environment to spawn with (not a delta) — the
    caller passes it straight to Popen's `env=`.
    """
    shell: str
    argv: list[str]
    env: dict[str, str]
    cwd: str


def _default_shells() -> tuple:
    """Candidate shells in preference order: PowerShell 7, Windows
    PowerShell, then cmd.exe on Windows; `$SHELL`, bash, sh elsewhere."""
    if _platform.IS_WINDOWS:
        return (shutil.which("pwsh"), shutil.which("powershell"),
                os.environ.get("ComSpec"))
    return (os.environ.get("SHELL"), "/bin/bash", "/bin/sh")


def _is_powershell(shell: str) -> bool:
    # Split on both separators: os.path.basename on POSIX ignores backslashes.
    name = shell.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in ("pwsh", "pwsh.exe", "powershell", "powershell.exe")


def resolve_profile(cwd: Optional[str] = None, shells: Optional[tuple] = None) -> Optional[TerminalProfile]:
    """The resolved profile, or None when no usable shell is found."""
    # `$SHELL` can be SET but stale (a shell that was uninstalled, a path
    # that never existed on this machine) — that is not "the user asked for
    # no terminal," so this falls through to /bin/bash and then /bin/sh
    # instead of refusing on a machine that plainly has a POSIX shell.
    for shell in (shells if shells is not None else _default_shells()):
        if shell and executable(shell):
            break
    else:
        return None

    env = dict(os.environ)
    # FUSED_RENDER_ORIGIN and friends (and PYTHONHOME/PYTHONPATH — see the
    # module docstring for why those two are NOT scrubbed here) are left to
    # the default (inherit); the child gets whatever the server process
    # already has.
    # FUSED_ENV is the exception, as for Claude sessions (claude_agent
    # `env.pop("FUSED_ENV")`): the `fused` wrapper only picks the canvas
    # clone's env when FUSED_ENV is unset, so a value the SERVER happened to
    # inherit would send a bare `fused` in a clone to the wrong Fused — and
    # in a custom clone, unset is the target itself. A value the user's own
    # shell rc exports, or `FUSED_ENV=x fused ...`, still wins.
    env.pop("FUSED_ENV", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"

    argv = [shell]
    if _platform.IS_WINDOWS:
        # No `-l` on Windows. PowerShell gets `-NoLogo` (the banner is noise
        # in a drawer tab) and, unlike POSIX shells, `-NoProfile` is NOT
        # passed: the user's $PROFILE should load. cmd.exe takes no flags.
        if _is_powershell(shell):
            argv.append("-NoLogo")
    elif sys.platform == "darwin":
        argv.append("-l")

    resolved_cwd = cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~")

    return TerminalProfile(shell=shell, argv=argv, env=env, cwd=resolved_cwd)
