"""The shell's Claude failure classifier agrees with the server (SPEC §42).

There used to be two classifiers to keep in step: `frontend/src/platform/lib/
trouble.ts` in the React shell and a copy inside the iframe chat page
(templates/claude/template.html), which shared no module with it. That page is
retired; the native chat renders its trouble cards through the shell's own
module, so the page-vs-shell parity half of this file went with it.

What is still duplicated is the install command: the shell shows it, and
`claude_health` RUNS it when the user presses Install.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = ROOT / "frontend" / "src" / "platform" / "lib" / "trouble.ts"


def _shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def test_the_shell_gates_the_shapes_on_the_message_being_about_claude():
    """The fix for the ENOENT misclassification (TR-2a). Losing the gate starts
    telling users to install Claude Code because a file was missing."""
    assert "ABOUT_CLAUDE" in _shell()


def test_the_install_command_matches_the_server_s_own_constant():
    """`claude_health.INSTALL_COMMAND_POSIX` is what the app itself RUNS when
    the user presses Install, and what it discloses beside that button. A drift
    here means the user is shown one command and has a different one run on
    their behalf."""
    from fused_render import claude_health

    command = "curl -fsSL https://claude.ai/install.sh | bash"
    assert claude_health.INSTALL_COMMAND_POSIX == command
    assert command in _shell()


def test_the_windows_install_command_is_pinned_too():
    """It is shown to Windows users and piped into PowerShell on their behalf,
    which is exactly the reason TR-10 pins the POSIX one. Absent this, the
    Windows half of D517's platform fix could be silently reworded — and the one
    platform that had the wrong command for longest is the one nobody developing
    this runs."""
    from fused_render import claude_health

    assert claude_health.INSTALL_COMMAND_WINDOWS == "irm https://claude.ai/install.ps1 | iex"
