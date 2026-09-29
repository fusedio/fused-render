"""Claude failure copy: how many copies of it exist, and what pins them (SPEC §42).

This file used to guard a DUPLICATION. The chat template was served standalone
and shared no module with the React shell, so the classifier, the copy blocks,
the deep links and the install command all existed twice — once in
`platform/lib/trouble.ts`, once inline in `templates/claude/template.html` — and
these tests were what stopped the second copy drifting.

The template is gone. The native chat (`apps/claude`) is a React app in the same
bundle as the shell, so it IMPORTS that module instead of restating it:
`apps/claude/protocol/trouble.ts` maps the platform verdict onto the chat's own
kinds, and `apps/claude/ui/TroubleView.tsx` is a thin wrapper over
`platform/ui/TroubleCard.tsx`. The parity assertions that compared two TS copies
of one string therefore have nothing left to compare.

What survives is the parity that still crosses a language boundary or a module
boundary:

  * PYTHON → TS. `claude_health.INSTALL_COMMAND_POSIX` is what the app RUNS when
    the user presses Install; `CLAUDE_INSTALL_COMMAND` is what it SHOWS. A drift
    is worse than a wrong command, because the user would be shown one line and
    have a different one run on their behalf.
  * THE SINGLE COPY ITSELF. The tests that say the chat has NOT grown a second
    classifier, a second install command or a second set of deep links — which
    is the property that made everything above deletable, and the one a future
    "just inline it here" would quietly undo.
  * THE ONE OVERRIDE LEFT. `TroubleView` restates the `cli-missing` title over
    the card's own, so that one string does still exist twice.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "src"
SHELL = FRONTEND / "platform" / "lib" / "trouble.ts"
CARD = FRONTEND / "platform" / "ui" / "TroubleCard.tsx"
CHAT_TROUBLE = FRONTEND / "apps" / "claude" / "protocol" / "trouble.ts"
CHAT_VIEW = FRONTEND / "apps" / "claude" / "ui" / "TroubleView.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_install_command_matches_the_server_s_own_constant():
    """What the app SHOWS and what the app RUNS, pinned to each other.

    `claude_health.INSTALL_COMMAND_POSIX` is piped into a shell when the user
    presses Install and is disclosed beside that button;
    `CLAUDE_INSTALL_COMMAND` is the line the trouble card puts in a copy box.
    Two different commands would be worse than one wrong one, because only one
    gets fixed."""
    from fused_render import claude_health

    command = "curl -fsSL https://claude.ai/install.sh | bash"
    assert claude_health.INSTALL_COMMAND_POSIX == command
    shell = _read(SHELL)
    assert f'export const CLAUDE_INSTALL_COMMAND = "{command}";' in shell
    # …and the agent brief tells the agent to run that same line, verbatim.
    assert f"install it: `{command}`." in shell


def test_the_windows_install_command_is_pinned_too():
    """It is shown to Windows users and piped into PowerShell on their behalf,
    which is exactly the reason the POSIX one is pinned. Absent this, the
    Windows half could be silently reworded — and the one platform that had the
    wrong command for longest is the one nobody developing this runs."""
    from fused_render import claude_health

    assert claude_health.INSTALL_COMMAND_WINDOWS == "irm https://claude.ai/install.ps1 | iex"


def test_the_chat_keeps_no_install_command_of_its_own():
    """The reason the three-way parity above is now a two-way one.

    The chat re-exports the platform constant rather than restating it, and
    draws no install box of its own — `TroubleCard` draws exactly one, complete
    with the "run it in a terminal, then quit and reopen" hint. A second literal
    here is the drift this file exists to prevent, arriving by the one route
    still open to it."""
    chat = _read(CHAT_TROUBLE)
    assert "CLAUDE_INSTALL_COMMAND" in chat
    assert 'from "@platform/lib/trouble"' in chat
    for source in (chat, _read(CHAT_VIEW)):
        assert "claude.ai/install" not in source, \
            "the chat has grown its own copy of the install command"


def test_the_chat_does_not_reimplement_the_classifier():
    """`platform/lib/trouble.ts` holds the two-tier matcher — unconditional
    NAMED phrases, plus SHAPE patterns that only count when the message is ABOUT
    Claude. A second matcher is how the chat and the Preferences tab start
    reaching different verdicts about the same message."""
    shell, chat = _read(SHELL), _read(CHAT_TROUBLE)
    # The gate that fixed the ENOENT misclassification lives with the patterns:
    # lose it and a missing file starts telling users to install Claude Code.
    assert "ABOUT_CLAUDE" in shell
    assert "troubleKind as platformTroubleKind" in chat
    assert "platformTroubleKind(text)" in chat
    for owned in ("NAMED", "SHAPES", "ABOUT_CLAUDE"):
        assert not re.search(rf"^const {owned}\b", chat, re.M), \
            f"{owned} is the platform module's to own, not the chat's"


def test_the_chat_deep_links_through_the_platform_helper():
    """One spelling of `#troubleshooting-<kind>`, so the chat and the
    Preferences tab cannot send a reader to different tabs for one failure."""
    shell, chat = _read(SHELL), _read(CHAT_TROUBLE)
    assert "troubleshooting-${kind}" in shell
    assert "troubleHelpUrl" in chat
    assert "troubleHelpUrl(platformKindOf(t.kind))" in chat
    # The chat builds no URL of its own — comments here NAME the spelling they
    # describe, so they are stripped before the search.
    body = re.sub(r"/\*.*?\*/", "", chat, flags=re.S)
    body = re.sub(r"//.*$", "", body, flags=re.M)
    assert "#troubleshooting-" not in body


def test_every_chat_kind_renders_as_a_card_the_platform_knows():
    """`platformKindOf` is the whole translation layer, and the card takes only
    the four platform kinds. A chat kind added without a seat here would reach
    `TroubleCard` as a name it has no copy for."""
    shell, chat = _read(SHELL), _read(CHAT_TROUBLE)
    declared = re.search(r"export type TroubleKind =([^;]+);", shell)
    assert declared, "the platform kinds moved"
    kinds = set(re.findall(r'"([a-z-]+)"', declared.group(1)))
    assert kinds == {"notfound", "login", "limit", "raw"}
    body = chat[chat.index("export function platformKindOf("):]
    body = body[:body.index("\n}")]
    returned = set(re.findall(r'return "([a-z-]+)"', body))
    assert returned <= kinds, f"{returned - kinds} is not a card the platform draws"
    assert 'return "raw"' in body, "an unmapped chat kind must still land somewhere"


def test_the_cli_missing_title_is_the_same_in_the_card_and_the_chat():
    """The one string that DOES still exist twice.

    `TroubleView` passes its own title for `cli-missing`, overriding the card's
    — everything else falls through to the card's copy. The same failure must
    not be described two ways by one app, so this is pinned the way all three
    titles used to be."""
    title = "The app can't find Claude Code"
    assert title in _read(CARD), "the shell's card lost the title"
    assert title in _read(CHAT_VIEW), "the chat's override drifted from it"


def test_the_chat_restates_no_other_card_title():
    """…and the reason the other two need no test: `login` and `limit` have no
    override, so there is exactly one copy of their words. An override added
    here quietly recreates the duplication this file was written for."""
    card, view = _read(CARD), _read(CHAT_VIEW)
    for title in ("Claude Code isn't signed in", "Your Claude usage limit was reached"):
        assert title in card, f"{title!r} missing from the shell's card"
        assert title not in view, \
            f"{title!r} is now stated twice — pin it, or drop the override"
