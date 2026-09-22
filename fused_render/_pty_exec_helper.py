"""Bare-python target for the pty session's Popen, spawned by
fused_render/pty_session.py. No `fused_render` import here — same "helper
script run in a clean interpreter" shape as `fused_render/_env_install_worker.py`.

The parent (server process) does the one thing that must NOT fork —
`os.openpty()`, which is a plain syscall, not a fork — and then Popen's this
script with the pty SLAVE fd wired to stdin/stdout/stderr. Everything this
script does runs in the freshly posix_spawn'd child, after the fork-dangerous
window has already closed, so setsid()/execv() here are safe in a way they
would not be back in the server process.

Run as:  python _pty_exec_helper.py <cwd> <shell> [shell-argv...]

`<shell>` and the trailing argv together are exactly `TerminalProfile.argv`
(fused_render/terminal_profiles.py) — argv[0] there is conventionally the
shell path itself, so `sys.argv[2:]` is passed to `os.execv` unmodified as
the child's own argv.

Order of operations matters:
  1. setsid() — become session leader, so the shell has no controlling
     terminal inherited from the server (which has none to give it anyway,
     but this also detaches the new process group cleanly).
  2. TIOCSCTTY on fd 0 — the slave is not yet the controlling terminal for
     the new session until this ioctl claims it. Without it, job control
     (Ctrl-C, `fg`/`bg`) inside the shell does not work.
  3. chdir(cwd) — moved here, out of the parent's Popen(cwd=...), precisely
     because `cwd=` on that Popen would force CPython onto the fork path.
  4. Scrub PYTHONHOME/PYTHONPATH — THIS interpreter (the one running this
     helper script) may need them (a packaged build's bundled python needs
     PYTHONHOME to find its own runtime — see terminal_profiles.py's module
     docstring for why `resolve_profile` deliberately keeps them). The
     shell about to replace this process is not Python and does not; a
     non-Python child that inherits them dies with "No module named
     'encodings'" for a bundled interpreter's PYTHONHOME/PYTHONPATH. This is
     the one process boundary where scrubbing is correct — after everything
     that still needs this interpreter's own env, right before the exec
     that throws it away.
  5. execv — replaces this interpreter's image with the resolved shell;
     nothing after this line ever runs. execv (not execve) inherits
     `os.environ` as mutated in step 4, since it takes no explicit env.
"""
import fcntl
import os
import sys
import termios


def main() -> None:
    cwd = sys.argv[1]
    shell = sys.argv[2]
    argv = sys.argv[2:]

    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir(cwd)
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.execv(shell, argv)


if __name__ == "__main__":
    main()
