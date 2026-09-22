"""Resolve the user's shell into a spawn profile for the status-bar terminal.

One resolved profile, not a settings-file *list* of them — VS Code's
`terminal.integrated.profiles` earns its complexity by serving every shell on
Windows plus WSL; this app has exactly one platform family in scope this
round (macOS + Linux, see Task in PLAN-status-bar-terminal.md) and `$SHELL`
is right by default. Revisit if someone asks for fish-vs-zsh switching.

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
import sys
from typing import Optional

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


def resolve_profile(cwd: Optional[str] = None) -> Optional[TerminalProfile]:
    """The resolved profile for the current platform, or None on Windows.

    Windows is deliberately out of scope this round (see Decisions in
    PLAN-status-bar-terminal.md): Python's `pty` module is unix-only, and
    ConPTY support means `pywinpty`, a C extension this app does not bundle.
    """
    if os.name == "nt":
        return None

    shell = os.environ.get("SHELL") or "/bin/bash"
    if not executable(shell):
        return None

    env = dict(os.environ)
    # FUSED_RENDER_ORIGIN and friends (and PYTHONHOME/PYTHONPATH — see the
    # module docstring for why those two are NOT scrubbed here) are left to
    # the default (inherit); the child gets whatever the server process
    # already has.
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"

    argv = [shell]
    if sys.platform == "darwin":
        argv.append("-l")

    resolved_cwd = cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~")

    return TerminalProfile(shell=shell, argv=argv, env=env, cwd=resolved_cwd)
