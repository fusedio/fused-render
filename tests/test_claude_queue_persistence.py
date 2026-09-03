"""The CLI holds the queue now, not the page (Task 6).

A message typed while a turn is streaming used to be parked in a page-side
array (`queuedMsgs`), mirrored into `sessionStorage` so a mode switch,
navigation, or reload did not drop it on the floor while the turn it was
queued behind kept going. That whole apparatus — the mirror, its ownership
rules, the dashed placeholder bubbles, the drain — existed only because the
OLD architecture killed the `claude` process at the end of every turn: a
follow-up had nowhere real to go until the next spawn, so the page had to
hold it itself.

Tasks 1-4 changed that: one `claude` process now stays up for a session's
whole life, listening on its own held-open stdin. A follow-up typed mid-turn
goes straight there — `action: "send"` writes it into the SAME host's inbox
(`agent.py::_send`), which the CLI absorbs into the turn already streaming or
queues on its own. There is nothing left on the page worth mirroring: the
text is either already in the CLI's hands or, if the run never accepted it,
still sitting in the box the reader is looking at.

This file is the source-assertion style `test_claude_composer_block.py` and
`test_claude_stop_run.py` already use: it does not execute the page (most of
what it used to check no longer exists to run), it checks the template's own
text for the shape of the new contract and the complete absence of the old
one.
"""
import os

TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")


def _html():
    return open(TEMPLATE, encoding="utf-8").read()


def _fn(html, header):
    """One top-level function's source: its header to the first column-0 `}`."""
    a = html.index(header)
    return html[a:html.index("\n}\n", a)]


# --------------------------------------------------------- the old apparatus

def test_the_sessionstorage_mirror_is_gone():
    assert "fused-render:claude-queue" not in _html(), \
        "no follow-up should still be written to sessionStorage"
    assert "QUEUE_STORE" not in _html()


def test_the_retired_queue_functions_are_gone():
    for name in ("queueRecord", "queueOwnedBy", "queueRestore", "persistQueue",
                 "restoreQueue", "queueMessage", "drainQueue", "unqueueAll"):
        assert name not in _html(), \
            name + " belonged to the page-side queue and should not remain"


def test_the_queued_array_is_gone():
    assert "queuedMsgs" not in _html()


def test_the_queue_container_and_its_css_are_gone():
    html = _html()
    assert 'id="queue"' not in html
    assert "#queue " not in html and "#queue{" not in html
    for selector in (".queued", "qbubble", "qmeta", ' class="qx"', "qx:hover"):
        assert selector not in html, selector + " was the retired queue's CSS/DOM"


# ------------------------------------------------------------- the new path

def test_send_follow_up_hands_text_straight_to_the_live_host():
    """The direct replacement for `queueMessage`: a real user bubble immediately
    (not a dashed placeholder), then `action: "send"` against the run already
    live — no page-side array in between."""
    html = _html()
    assert "async function sendFollowUp(" in html
    body = _fn(html, "async function sendFollowUp(")
    assert "addUser(text)" in body
    assert 'action: "send"' in body
    assert "run_id" in body


def test_submit_chat_sends_directly_instead_of_queueing():
    body = _fn(_html(), "function submitChat()")
    assert "if (activeRun)" in body
    branch = body[body.index("if (activeRun)"):]
    assert "sendFollowUp(message)" in branch
    assert "queueMessage" not in branch


def test_stop_run_hands_back_the_clis_own_still_queued_list():
    """No local array to reconcile any more — whatever the interrupt's
    `still_queued` names IS the answer, straight from the process that was
    actually holding it."""
    body = _fn(_html(), "async function stopRun()")
    assert "still_queued" in body
    assert "queuedMsgs" not in body
    assert "unqueueAll" not in body


def test_send_message_tries_the_live_host_before_spawning_a_new_one():
    """A session already live for this file/session_id gets this message
    through its own inbox (`action: "send"`) instead of `action: "start"`
    launching a second, parallel `claude` against the same session_id."""
    html = _html()
    body = _fn(html, "async function sendMessage(")
    assert 'action: "live_host"' in body
    assert 'action: "send"' in body
    assert 'action: "start"' in body
    # The probe must be tried before the fallback spawn, not after.
    assert body.index('action: "live_host"') < body.index('action: "start"')


def test_no_drain_queue_call_sites_remain():
    assert "drainQueue()" not in _html()


def test_the_flush_hook_is_a_no_op_now():
    """There is nothing page-side left for the shell's remount-safety hook to
    save — a follow-up already left for the CLI the moment it was sent — so it
    always lets the frame go."""
    html = _html()
    assert "window.__fusedFlushEdits" in html, "the chat exposes no teardown hook"
    hook = html[html.index("window.__fusedFlushEdits"):]
    hook = hook[:hook.index("};")]
    assert "persistQueue" not in hook
    assert "ok: true" in hook
