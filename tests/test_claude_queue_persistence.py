"""Messages queued against a live run survive the frame dying.

A message typed while a turn is streaming is parked in the chat's queue and
fired when the turn ends. The RUN survives a teardown — its id is in the URL
(`?run=`) and the next boot re-attaches to the still-running subprocess — but
the queue did not: it lived only in the `queuedMsgs` array and its placeholder
bubbles, so switching template modes (which REMOUNTS the preview iframe),
navigating, or reloading dropped the user's follow-ups on the floor while the
turn they were queued behind kept going. Silent, and unrecoverable: the text was
never sent and never handed back to the composer.

The queue is now mirrored into `sessionStorage` (per tab, exactly the lifetime
of the frame's own comings and goings) on every mutation, and restored on boot
before the re-attach, so the restored entries drain through the ordinary
`drainQueue` path.

Two decisions are pinned here because they are what makes the feature safe
rather than merely present:

* **a record only ever comes back to the conversation it was written for.** It
  carries both ids the URL can name (`session_id`, `run`) and is adopted only
  when the booting page matches one of them — otherwise it is dropped, so a
  leftover record can never inject a previous chat's words into a new one.
* **the flush hook refuses a mode switch only on a real failure to save.** The
  shell awaits `window.__fusedFlushEdits()` before remounting and aborts on
  `{ok: false}` (the same contract the code template's editor uses); "there is a
  queue" is not a failure, "the queue could not be written" is.

The decisions are pure functions in the page (`queueRecord`, `queueRestore`,
`persistQueue`), extracted and executed under node — what matters is what comes
back, not the shape of the source.
"""
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")

STORE_START = "const QUEUE_STORE"
STORE_END = "\nfunction restoreQueue("


def _html():
    return open(TEMPLATE, encoding="utf-8").read()


def _block(html, start, end):
    a = html.index(start)
    return html[a:html.index(end, a)]


def _body(html, header):
    """One top-level function's source: its header to the first column-0 `}`."""
    a = html.index(header)
    return html[a:html.index("\n}\n", a)]


def _node(expr):
    """Run the page's real persistence helpers over a stubbed environment.

    The guard sits on the shell-out itself (same siting as
    test_claude_stop_run.py's `_run_ending`) so no later test can drift out of
    its reach.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own queue-persistence decisions")
    src = _block(_html(), STORE_START, STORE_END)
    out = subprocess.run(["node", "-e", src + "\n" + expr], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _record(session, run, texts):
    return _node("console.log(JSON.stringify(queueRecord(%s, %s, %s)));"
                 % (json.dumps(session), json.dumps(run), json.dumps(texts)))


def _restore(raw, session, run):
    return _node("console.log(JSON.stringify(queueRestore(%s, %s, %s)));"
                 % (json.dumps(raw), json.dumps(session), json.dumps(run)))


# --------------------------------------------------------------- the record

def test_a_queue_is_stored_with_both_ids_that_can_name_its_conversation():
    rec = _record("sess-1", "run-1", ["first", "second"])
    assert rec["texts"] == ["first", "second"], "order is the drain order"
    assert rec["session"] == "sess-1" and rec["run"] == "run-1"


def test_an_empty_queue_stores_nothing():
    """Drained or unqueued to the last entry: the record must go, not linger as
    something a later boot could adopt."""
    assert _record("sess-1", "run-1", []) is None


def test_a_queue_with_nothing_to_key_it_by_is_not_stored():
    """A record no boot could ever match is worse than none: it would sit in
    storage looking restorable. (Unreachable in practice — parking a message
    needs a live run, and a live run means `?run=`.)"""
    assert _record("", "", ["orphan"]) is None


# -------------------------------------------------------------- the restore

def test_the_queue_comes_back_to_the_session_it_was_written_for():
    raw = json.dumps({"session": "sess-1", "run": "run-1", "texts": ["a", "b"]})
    assert _restore(raw, "sess-1", "run-1") == ["a", "b"]


def test_the_queue_comes_back_when_only_the_run_id_still_matches():
    """The two ids drift on purpose: a message can be queued before the first
    poll has reported the session id, so its record names only the run — and the
    boot that re-attaches to that run knows the session by then. Matching on
    EITHER id is what keeps that turn's follow-ups."""
    raw = json.dumps({"session": "", "run": "run-1", "texts": ["a"]})
    assert _restore(raw, "sess-1", "run-1") == ["a"]


def test_another_conversations_queue_is_never_adopted():
    raw = json.dumps({"session": "sess-1", "run": "run-1", "texts": ["a"]})
    assert _restore(raw, "sess-2", "run-2") == []
    # ...and a brand-new chat, which names no conversation at all, matches
    # nothing: this is the case that would otherwise put a finished session's
    # words into the first turn of an unrelated one.
    assert _restore(raw, "", "") == []


def test_a_damaged_record_is_ignored_rather_than_thrown():
    assert _restore("{not json", "sess-1", "run-1") == []
    assert _restore(None, "sess-1", "run-1") == []
    assert _restore(json.dumps({"session": "sess-1"}), "sess-1", "") == []
    # non-strings and blanks inside an otherwise valid record don't become bubbles
    raw = json.dumps({"session": "sess-1", "run": "", "texts": ["a", "", 7, None]})
    assert _restore(raw, "sess-1", "") == ["a"]


# ------------------------------------------------------- persist / the hook

def _persist(texts, session="sess-1", run="run-1", throws=False):
    """Drive the page's `persistQueue` over a stubbed sessionStorage."""
    stub = """
    const store = {};
    globalThis.sessionStorage = {
      setItem: (k, v) => { if (%s) throw new Error("quota"); store[k] = v; },
      removeItem: (k) => { delete store[k]; },
    };
    globalThis.fused = { params: { get: (k) => ({session_id: %s, run: %s})[k] } };
    globalThis.queuedMsgs = %s.map((t) => ({ text: t }));
    """ % (json.dumps(bool(throws)), json.dumps(session), json.dumps(run),
           json.dumps(texts))
    return _node(stub + """
    const ok = persistQueue();
    console.log(JSON.stringify({ ok, stored: store[QUEUE_STORE] || null }));
    """)


def test_persisting_writes_the_live_queue_under_the_url_s_ids():
    out = _persist(["a", "b"])
    assert out["ok"] is True
    assert json.loads(out["stored"]) == {"session": "sess-1", "run": "run-1",
                                         "texts": ["a", "b"]}


def test_persisting_an_emptied_queue_clears_the_record():
    out = _persist([])
    assert out["ok"] is True and out["stored"] is None


def test_persisting_reports_failure_when_the_write_is_refused():
    """Storage can say no (quota, a locked-down profile). Saying "saved" then
    would let the shell tear the frame down over text that is nowhere."""
    out = _persist(["a"], throws=True)
    assert out["ok"] is False


def test_a_failed_write_of_an_empty_queue_is_still_a_success():
    """Nothing to lose: refusing the mode switch here would strand the user in a
    pane for no reason."""
    assert _persist([], throws=True)["ok"] is True


def test_the_flush_hook_answers_with_whether_the_queue_was_saved():
    """The shell's contract (Preview.tsx): await `__fusedFlushEdits`, abort the
    mode switch on {ok: false}. Same shape as the code template's editor hook."""
    html = _html()
    assert "window.__fusedFlushEdits" in html, "the chat exposes no teardown hook"
    hook = html[html.index("window.__fusedFlushEdits"):]
    hook = hook[:hook.index("};")]
    assert "persistQueue()" in hook, "the hook does not actually save the queue"
    assert "ok:" in hook or "ok :" in hook


# ------------------------------------------------------------- the wiring

@pytest.mark.parametrize("header,why", [
    ("function queueMessage(", "a newly parked message"),
    ("function drainQueue(", "the entry that just went out"),
    ("function unqueueAll(", "entries handed back to the composer"),
])
def test_every_mutation_of_the_queue_is_mirrored_to_storage(header, why):
    """The store must never drift from `queuedMsgs`: a mirror written on the
    enqueue only would replay messages that have already been sent (%s)."""
    assert "persistQueue()" in _body(_html(), header), \
        "the store is not updated for " + why


def test_removing_one_bubble_updates_the_store():
    """The × handler lives inside queueMessage's closure, not in a function of
    its own — pinned separately because the body assertion above would pass on
    the enqueue's own call alone."""
    body = _body(_html(), "function queueMessage(")
    click = body[body.index("x.onclick"):]
    assert "persistQueue()" in click, "unqueueing one message leaves it in the store"


def test_the_queue_is_restored_before_the_run_is_re_attached():
    """Ordering is the whole feature: `resumeRun`'s tail calls `drainQueue`, so
    entries restored after it would sit there until some later turn ended."""
    boot = _html()[_html().index("// ── boot:"):]
    assert "restoreQueue(" in boot, "the boot never restores the queue"
    assert boot.index("restoreQueue(") < boot.index("resumeRun(run_id)")
    assert boot.index("restoreQueue(") < boot.index("loadHistory(session_id)")


def test_a_boot_with_no_conversation_sweeps_the_store():
    """The landing page names no conversation, so it adopts nothing — and takes
    the chance to drop the record rather than leaving it for the next chat to
    fail to match."""
    boot = _html()[_html().index("// ── boot:"):]
    landing = boot[boot.index("} else {"):]
    assert "restoreQueue(" in landing
