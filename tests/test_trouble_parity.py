"""The shell and the chat template both classify Claude failures (SPEC §42).

They have to: `frontend/src/platform/lib/trouble.ts` runs in the React shell and
the copy inside `templates/claude/template.html` runs in a page served
standalone, which shares no module with it. Duplication is the price of that,
and this file is what stops it being paid twice — silently.

What is pinned is the part a user would notice drifting: the words on the card,
the install command, and the patterns that decide WHICH card. A wording change
in one file and not the other means the same failure is described two ways by
one app; a pattern change in one and not the other means the chat and the
Preferences tab disagree about what went wrong.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = ROOT / "frontend" / "src" / "platform" / "lib" / "trouble.ts"
CARD = ROOT / "frontend" / "src" / "platform" / "ui" / "TroubleCard.tsx"
TEMPLATE = ROOT / "fused_render" / "templates" / "claude" / "template.html"

# The three cases the chat template renders richly. `raw` is deliberately NOT
# among them — it keeps the plain red row there (see addError), because dressing
# every failed turn as a troubleshooting card buries the ones a user can act on.
KINDS = ("notfound", "login", "limit")


def _shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_the_install_command_is_identical_in_both():
    """Shown as a thing to copy into a terminal in both places. Two different
    commands would be worse than one wrong one, because only one gets fixed."""
    command = "curl -fsSL https://claude.ai/install.sh | bash"
    assert command in _shell()
    assert command in _template()


def test_both_deep_link_to_the_same_troubleshooting_tabs():
    assert "#troubleshooting-" in _template()
    assert "troubleshooting-${kind}" in _shell()
    for kind in KINDS:
        # The template builds the URL by concatenation, so the kind has to exist
        # as a classification in both.
        assert f'"{kind}"' in _template()
        assert f'"{kind}"' in _shell()


def test_the_card_titles_match_word_for_word():
    """The same failure must not be described two ways by one app."""
    card = CARD.read_text(encoding="utf-8")
    template = _template()
    for title in ("The app can't find Claude Code",
                  "Claude Code isn't signed in",
                  "Your Claude usage limit was reached"):
        assert title in card, f"{title!r} missing from the shell's card"
        assert title in template, f"{title!r} missing from the chat template"


def test_the_classification_patterns_match():
    """The rules themselves, not just the words.

    Extracted rather than eyeballed: a pattern that exists in one file and not
    the other means the chat and the Preferences tab reach different verdicts
    about the same message, which is the disagreement this whole section exists
    to prevent.
    """
    def patterns(text: str, name: str) -> set:
        # The shell's declaration carries a type annotation containing `][`, so
        # the array is found by its closing line rather than the first `];`.
        block = re.search(rf"\b{name}\b[^=]*=\s*\[\n(.*?)\n\];", text, re.S)
        assert block, f"could not find {name}"
        return set(re.findall(r"/(.+?)/i", block.group(1)))

    shell, template = _shell(), _template()
    assert patterns(shell, "NAMED") == patterns(template, "TROUBLE_NAMED")
    assert patterns(shell, "SHAPES") == patterns(template, "TROUBLE_SHAPES")


def test_the_agent_instructions_match_step_for_step():
    """The "Copy Claude Code instructions" brief. Two copies of a prompt that
    tells an agent what to run is exactly the kind of thing that drifts — one
    gets a better first command and the other quietly keeps the worse one."""
    shell, template = _shell(), _template()
    steps = re.findall(r'"((?:Check whether|Confirm|Sign in|Work out|If it|If the|Do not|Tell me)[^"]+)"', shell)
    assert len(steps) >= 11, f"expected the four step lists, found {len(steps)}"
    for step in steps:
        assert step in template, f"step missing from the chat template: {step[:60]!r}"


def test_both_tell_an_agent_where_to_find_the_installation():
    """The brief is useless without a directory (TR-11).

    An agent handed "something around Fused Render is broken" and no path has
    nowhere to start — and the boot failure, which is the case most likely to
    produce this brief, is precisely the one that cannot state a path, because
    `/api/config` is what failed. Both copies therefore carry the same way to
    FIND it, and a find command that exists in one copy only means the chat and
    the shell send agents looking in different places."""
    shell, template = _shell(), _template()
    for command in (
        # /Applications first: the DMG is how this is actually installed, and a
        # probe that answers with some other python's site-packages sends an
        # agent to edit a copy the app does not run.
        "ls -d /Applications/FusedRender.app ~/Applications/FusedRender.app",
        "/Applications/FusedRender.app/Contents/Resources/lib/python3.*/fused_render",
        "brew list --cask fused-render",
        "FusedRenderPy",
    ):
        assert command in shell, f"missing from the shell: {command[:50]!r}"
        assert command in template, f"missing from the chat template: {command[:50]!r}"
    # The user-data dir is a DIFFERENT place from the install, and the brief
    # says so in both — a reinstall replaces one and never touches the other.
    for text in ("~/.fused-render", 'fused-render-*.log'):
        assert text in shell and text in template
    # And NEITHER may reintroduce the probes that name an unsupported install
    # method — a bare `python3` on PATH is not the bundle's interpreter.
    for banned in ("pip show fused-render", "import fused_render, os"):
        assert banned not in shell, f"unsupported install probe is back: {banned!r}"
        assert banned not in template, f"unsupported install probe is back: {banned!r}"


def test_both_gate_the_shapes_on_the_message_being_about_claude():
    """The fix for the ENOENT misclassification (TR-2a). If one copy loses the
    gate, that copy starts telling users to install Claude Code because a file
    was missing."""
    assert "ABOUT_CLAUDE" in _shell()
    assert "TROUBLE_ABOUT_CLAUDE" in _template()


SELFFIX_LIB = ROOT / "frontend" / "src" / "platform" / "lib" / "selffix.ts"


def test_the_precheck_says_exactly_what_the_spawn_would_have_said():
    """One fact, one sentence, wherever the user meets it (SF-13f).

    A surface that knows in advance that Claude Code is missing answers with
    `CLAUDE_MISSING_ERROR` instead of spending a doomed spawn. That string has to
    stay byte-identical to the one `spawn_helper` returns when the CLI really is
    absent: a user can meet both in one sitting — the pre-check on a failed row,
    and the spawn's own answer from a session started before the config read
    landed — and two accounts of one fact read as two different problems.

    The wording is load-bearing past politeness: "Claude Code isn't installed" is
    the NAMED pattern lib/trouble.ts classifies as `notfound`, which is what puts
    the install command and the troubleshooting link on the card.
    """
    from fused_render.claude_spawn import CLAUDE_MISSING_ERROR

    source = SELFFIX_LIB.read_text(encoding="utf-8")
    match = re.search(r"CLAUDE_MISSING_ERROR\s*=\s*(.*?);", source, re.S)
    assert match, "CLAUDE_MISSING_ERROR is gone from platform/lib/selffix.ts"
    # Adjacent string literals joined by `+`, folded the way the compiler folds
    # them. Only double-quoted pieces, which is how this file is written.
    shell_text = "".join(re.findall(r'"([^"]*)"', match.group(1)))

    assert shell_text == CLAUDE_MISSING_ERROR, (
        "the pre-check and the spawn disagree about what a missing CLI says:\n"
        f"  shell:  {shell_text!r}\n"
        f"  server: {CLAUDE_MISSING_ERROR!r}")
    assert troubleKindIsNotFound(shell_text), (
        "the sentence stopped matching trouble.ts's NAMED notfound pattern")


def troubleKindIsNotFound(text: str) -> bool:
    """The NAMED `notfound` rule from lib/trouble.ts, restated for this one
    assertion — the shell's own test suite owns the classifier, this only needs
    to know that the sentence still trips it."""
    return bool(re.search(r"claude code isn'?t installed|claude cli not found"
                          r"|claude not found", text, re.I))

