"""The composer's half of persistent sessions (template.html): send-immediately
over `steer` instead of the browser-side queue, and the `submitChat` race this
work also had to close (see agent.py's `_steer`/`_persistent_ok` and D496).

Structural assertions over the template source, the same approach
test_claude_composer_block.py and test_claude_schedule_pill.py take for this
file: what can be pinned cheaply is that the wiring exists and points the
right way, not a full DOM execution of a 12000-line document.
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_ROOT, "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def code(source) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


def _fn(code: str, opening: str) -> str:
    body = code[code.index(opening):]
    return body[:body.index("\n}")]


def test_submit_chat_steers_a_persistent_run_instead_of_parking_it(code):
    submit = _fn(code, "function submitChat()")
    # The historical shape is still there (the fallback for a one-shot run)...
    assert "queueMessage(message);" in submit
    # ...but a persistent run's follow-up goes straight to the CLI.
    assert "if (activeRunPersistent) steerMessage(message);" in submit
    assert "else queueMessage(message);" in submit


def test_submit_chat_no_longer_silently_drops_a_message_sent_mid_start(code):
    """The race: `sending` goes true before `activeRun` is assigned (see
    sendMessage), so a second submit landing in that window used to fall
    through both guards to a bare `if (sending) return;` with nothing queued
    and no feedback at all. It must now park the same way the activeRun
    branch above it does."""
    submit = _fn(code, "function submitChat()")
    assert "if (sending) return;" not in submit, \
        "the silent-drop return is still there"
    assert submit.count("queueMessage(message);") >= 2, \
        "the sending-window message is not parked"
    # Never steered here: there is no run_id yet to steer into.
    sending_block = submit[submit.index("if (sending) {"):]
    sending_block = sending_block[:sending_block.index("\n  }")]
    assert "steerMessage" not in sending_block


def test_steer_message_calls_the_steer_action(code):
    fn = _fn(code, "async function steerMessage(text)")
    assert 'action: "steer"' in fn
    assert "run_id" in fn and "message: text" in fn
    # A failed/raced steer must not lose the message.
    assert "queueMessage(text);" in fn


def test_active_run_persistent_is_read_from_poll_and_reset_with_active_run(code):
    assert "let activeRunPersistent = false;" in code
    poll_loop = _fn(code, "async function pollLoop(run_id, gen = logGen)")
    assert "activeRunPersistent = !!data.persistent;" in poll_loop
    # Reset alongside activeRun everywhere activeRun is cleared, so a later
    # one-shot run never inherits an earlier persistent run's "yes".
    for reset_site in code.split("activeRun = null;")[1:]:
        # the next few lines after each reset
        window = reset_site[:200]
        assert "activeRunPersistent = false;" in window, \
            "an activeRun = null site left activeRunPersistent stale"
