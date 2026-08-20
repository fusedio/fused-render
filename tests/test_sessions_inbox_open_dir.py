"""The Inbox's "Open in explorer" — a session handed to the Explorer, chat and
all (`core_apps/sessions/inbox.html`).

The button used to open the folder and nothing else, which threw away the only
reason the reader clicked it: they were reading a conversation, wanted to see the
files it was about, and arrived at a bare listing that had forgotten the chat. It
now links to

    /explorer/view/<the session's cwd>?_side=claude&session_id=<id>

and the two params do two different jobs — `_side=claude` opens the folder's chat
pane (listing/pane-side.ts), `session_id` tells the chat which conversation to
resume (the pane's template reads it off the shell URL through the runtime's
ancestor climb, runtime.js D46/D72 — nothing forwards it).

That makes this a THREE-FILE contract with two param names spelled in each, so
what is pinned here is the contract rather than any one side of it (D146: a
duplicated rule needs a test, not a comment).

The third file is the reason the link ARRIVES SOMEWHERE, and it is worth writing
down what changed under it. A session is keyed on ONE cwd — its transcript lives
under ~/.claude/projects/<munge(_workdir)> — and the chat pane's target follows
the selected row (`paneSideTarget`). So while the listing still auto-selected a
row on arrival, this link resolved the resume against a SUBFOLDER of the cwd the
session was recorded in and found nothing: the right session id in the pane's
corner, an empty transcript under it. That was held off by a `resumingPaneSession`
guard inside the one-shot auto-select effect.

D278 then deleted the folder auto-select outright (FS-16): opening a folder now
selects nothing at all. With no row selected `paneSideTarget` falls back to the
folder, which is the ground the session has — so the behaviour the guard bought
is now simply how the listing works, for every arrival and not just this one. The
guard went with the effect it guarded, and what is pinned below is the stronger
fact that replaced it: there is no folder auto-select left to steal the pane.

The link itself is checked by running the page's REAL handler under node (the
`_js_block` approach of test_sessions_inbox_layout.py / test_calls.py — a copy of
the URL builder in the test would keep passing after the shipping code
regressed). The cross-file halves are structural, the way test_claude_kind.py
pins facts about code it cannot execute.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_INBOX = os.path.join(_ROOT, "core_apps", "sessions", "inbox.html")
_PANE_SIDE = os.path.join(_ROOT, "frontend", "src", "apps", "explorer",
                          "listing", "pane-side.ts")
_LISTING = os.path.join(_ROOT, "frontend", "src", "apps", "explorer", "Listing.tsx")
_TEMPLATE = os.path.join(_ROOT, "fused_render", "templates", "claude", "template.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def source():
    return _read(_INBOX)


def _open_dir_block(src):
    """The shipping handler, verbatim: openDirBtn .. the end of its onclick."""
    start = src.find("const openDirBtn = $(")
    assert start != -1, "inbox.html no longer wires peek-open-dir — did the button go away?"
    end = src.find("\n  };", start)
    assert end != -1, "the open-dir handler is no longer a single arrow body"
    return src[start:end + len("\n  };")]


def _opened(tmp_path, src, cwd, session_id="s-1"):
    """Click the real handler with this cwd/id; return [url, target] or None.

    `$` and `window.open` are stubbed exactly as far as the block reaches: one
    element with a `style` (the show/hide line) and an assignable `onclick`.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the page's JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        f"const cwd = {json.dumps(cwd)};\n"
        f"const id = {json.dumps(session_id)};\n"
        "const btn = { style: {}, onclick: null };\n"
        "const $ = () => btn;\n"
        "let opened = null;\n"
        "globalThis.window = { open: (url, target) => { opened = [url, target]; } };\n"
        f"{_open_dir_block(src)}\n"
        "console.log(JSON.stringify({ display: btn.style.display,"
        " opened: (btn.onclick && btn.onclick(), opened) }));\n",
        encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


# ------------------------------------------------------------- the link


def test_the_link_carries_the_pane_and_the_conversation(tmp_path, source):
    """Both params, or the click is only half of what the button promises: the
    folder without `_side=claude` is a listing with no chat beside it, and the
    chat without `session_id` is a NEW chat about the right folder — which is
    exactly the state the reader was trying to get out of."""
    url = _opened(tmp_path, source, "/Users/me/work/repo", "abc-123")["opened"][0]
    assert url.startswith("/explorer/view/Users/me/work/repo?")
    assert "_side=claude" in url
    assert "session_id=abc-123" in url


def test_it_leaves_the_inbox_rather_than_nesting_the_explorer(tmp_path, source):
    """The inbox is an iframe inside the shell, so a default-target open would
    render the whole Explorer inside the peek panel."""
    assert _opened(tmp_path, source, "/Users/me/work/repo")["opened"][1] == "_top"


def test_the_cwd_goes_through_the_shells_own_path_codec(tmp_path, source):
    """Segment-wise encode of the absolute path, leading slash dropped — the
    same codec platform/lib/router.ts uses. A whole-string encode would turn the
    separators into %2F and the shell would route to one long file name."""
    url = _opened(tmp_path, source, "/Users/me/my work/a+b & c")["opened"][0]
    assert url.split("?")[0] == "/explorer/view/Users/me/my%20work/a%2Bb%20%26%20c"


def test_a_windows_cwd_is_normalised_before_it_is_encoded(tmp_path, source):
    url = _opened(tmp_path, source, r"C:\Users\me\repo")["opened"][0]
    assert url.split("?")[0] == "/explorer/view/C%3A/Users/me/repo"


def test_a_session_with_no_cwd_has_nothing_to_open(tmp_path, source):
    """Some transcripts record no cwd. The button is hidden rather than left
    pointing at /explorer/view/ — the Explorer's root, which is not where the
    session was."""
    assert _opened(tmp_path, source, "")["display"] == "none"
    assert _opened(tmp_path, source, "/Users/me/work/repo")["display"] == ""


def test_the_id_is_encoded_on_the_way_out(tmp_path, source):
    """It is a URL param, and it reaches a filesystem path on the other side
    (agent.py joins it onto a project dir). Encoding is the first of the two
    guards; `_bad_id` is the second."""
    url = _opened(tmp_path, source, "/Users/me/repo", "a b&c=d")["opened"][0]
    assert "session_id=a%20b%26c%3Dd" in url


# ------------------------------------------- the two files that read the link


def test_the_pane_reads_exactly_the_param_the_inbox_writes(source):
    """A rename on either side leaves the button opening a folder with the pane
    shut — no error, no log line, nothing to debug from.

    `_side` is the half the SHELL reads, and "claude" has to be a value it will
    hold: since D285 the param carries a COMPANION and nothing else, so a mode
    dropped from that tuple would make this link parse as no-choice."""
    link = _open_dir_block(source)
    reader = _read(_PANE_SIDE)
    for param in ('_side=claude', 'session_id'):
        assert param in link, f"the inbox link no longer writes {param}"
    assert 'PANE_SIDE_COMPANIONS = ["claude", "git", "mcp"]' in reader, (
        "the inbox writes _side=claude; parsePaneSide keeps only companions")
    assert 'isPaneSideChoice(raw)' in reader


def test_the_id_is_read_by_the_chat_itself_not_the_shell():
    """The other half never reaches the shell's own code, which is why nothing
    in pane-side.ts mentions it: the pane's chat iframe reads `session_id` off
    the shell URL through the runtime's ancestor climb (D46/D72) and resumes
    from it on boot. The template is the reader, so the template is the pin."""
    template = _read(_TEMPLATE)
    assert 'fused.params.get("session_id")' in template
    # and it is a param this chat OWNS rather than one it merely observes
    assert '"session_id"' in template


def test_nothing_selects_a_row_out_from_under_the_arriving_chat():
    """The load-bearing half, and since D278 it is an ABSENCE rather than a guard.

    The chat pane's target follows the selected row (paneSideTarget), so anything
    that selects a row on arrival aims the resume at a SUBFOLDER of the session's
    cwd and it comes up empty. The folder auto-select is what used to do that; it
    is deleted, so a freshly opened folder holds no selection and the pane falls
    back to the folder — the one target the id can be resolved against.

    Pinned as the absence of the machinery, because that is what makes the link
    work: a re-added folder auto-select would silently break this button again.
    `searchAutoSelectPath` is deliberately still allowed — a query is a request to
    look at something, and this link is not a query."""
    listing = _read(_LISTING)
    assert "autoSelectedRef" not in listing, (
        "a folder auto-select is back: it will steal the pane from the arriving chat")
    assert not re.search(r"(?<![A-Za-z])autoSelectPath", listing), (
        "the folder auto-select decision is back (searchAutoSelectPath is fine)")
    assert "selectionClaimed" not in listing
    # and with nothing selected, the chat is aimed at the folder
    reader = _read(_PANE_SIDE)
    assert 'return isFolderBoundSide(side) ? folder : (rowPath ?? folder);' in reader, (
        "paneSideTarget no longer falls back to the folder without a row")
