"""Source-contract tests for the annotate template's anchor strategies (§17).

The template picks a strategy per framed view — code-editor views anchor on a
LINE NUMBER, everything else on element paths and quoted text — and picking the
wrong one is silent: the gesture handlers `preventDefault()` first and bail on an
unresolvable anchor, so a mis-detected view swallows every click and drops every
selection with no error anywhere. That is exactly what happened to the markdown
notes view, a CodeMirror editor whose lineNumbers gutter is present but
**hidden**, so these pin the detection rule and the container rule that keep it
working.

It also covers the **Send to Claude round trip** (§17): which comments ride a
handoff (`sent`), how the URL budget evicts them, and the `claudeReturn` ticket
that brings the shell back to annotate when the run finishes. The return leg's
code lives in `templates/claude/template.html`, but the contract is annotate's —
annotate is the only thing that sets the ticket, and a silent regression there
means a reviewer is stranded in a chat that never reloads. Those pieces run the
template's REAL functions under node (the `_js_block` approach of
test_map_template_escaping.py / test_calls.py — a copy would keep passing after
the shipping code regressed).

The sidecar writer next door has its own behavioural tests
(tests/test_annotate_comments.py).
"""
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "annotate",
    "template.html")
CLAUDE_TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "claude",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def claude_source():
    with open(CLAUDE_TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def _block(src, start, end):
    """The shipping source from `start` up to and including `end`, verbatim."""
    i = src.index(start)
    j = src.index(end, i) + len(end)
    return src[i:j]


def _node(script, tmp_path):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's own JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_line_strategy_needs_a_gutter_that_is_actually_laid_out(source):
    # `.cm-editor` alone was the probe, and the markdown notes view matched it:
    # cmVisibleLines() was then always empty, so clicks were swallowed by the
    # `!hit` bail after preventDefault, selections dropped, and stored comments
    # never painted. Testing for `.cm-lineNumbers` is NOT enough either — that
    # view ships basicSetup, so the gutter element EXISTS; it is `display:none`
    # (prose, MD-18a), which is exactly why cmVisibleLines pairs only gutter
    # elements with real height. The probe has to make the same measurement, or
    # it disagrees with the code that consumes it.
    body = source[source.index("const isCmDoc = (doc) => {"):]
    body = body[:body.index("\n    };")]
    assert '.cm-editor' in body
    assert '.cm-lineNumbers' in body
    assert "getBoundingClientRect().height > 0" in body
    # And the gutter query the strategy itself reads is the same one, filtered
    # the same way.
    assert '.cm-lineNumbers .cm-gutterElement' in source
    assert "gr.height > 0" in source


def test_a_quote_in_a_gutterless_editor_anchors_on_the_stable_content_element(
        source):
    # A gutterless editor falls through to element/quote anchors, where the
    # natural container is a `.cm-line` div — whose nth-of-type index shifts as
    # lines mount and unmount on scroll, so the path drifts onto another line.
    # `.cm-content` is structurally stable; the quote and its occurrence index do
    # the locating inside it.
    body = source[source.index('doc.addEventListener("mouseup"'):]
    body = body[:body.index('doc.addEventListener("click"')]
    assert 'cont.closest(".cm-content")' in body
    assert "if (content) cont = content;" in body


# --- the Send to Claude payload (`sent`) ------------------------------------


def test_a_handoff_only_carries_comments_that_are_neither_resolved_nor_sent(
        source, tmp_path):
    # The bug: the button sent load() in full, so every Claude turn re-received
    # the entire review — resolved threads and threads the previous turn had
    # already answered — and the agent re-did work it had just done.
    fn = _block(source, "const sendable = (arr) =>", ";")
    got = _node(
        fn + "\nconsole.log(JSON.stringify(sendable(" + json.dumps([
            {"id": "open"},
            {"id": "done", "resolved": True},
            {"id": "already", "sent": 1},
            {"id": "both", "resolved": True, "sent": 1},
            {"id": "reopened"},
        ]) + ").map((c) => c.id)));\n", tmp_path)
    assert got == ["open", "reopened"]


def test_the_send_handler_stamps_and_saves_before_it_navigates(source):
    # Ordering is the whole fix: save() is what writes the `comments` URL param
    # (and the sidecar), and the navigation tears this frame down — a stamp
    # applied after `window.top.location.href` would never land, so the next
    # visit would re-send everything again.
    body = _block(source, 'document.getElementById("toclaude").addEventListener',
                  "window.top.location.href = top.pathname")
    assert "const payload = sendable(all);" in body
    assert "for (const c of payload) c.sent = 1;" in body
    assert body.index("const persisted = save(all);") < body.index(
        "window.top.location.href")
    # And the payload is what crosses over — not the whole store.
    assert 'params.set("claudeComments", JSON.stringify(payload));' in body
    # The navigation URL is built from a RAW read of window.top's search, but
    # fused.params.set coalesces its history write (D99) — so the stamps may not
    # be in that string yet. Writing it back verbatim would undo them and
    # re-send the same comments next time; the persisted list is re-asserted.
    assert 'params.set("comments", JSON.stringify(persisted));' in body
    assert "return arr;" in _block(source, "function save(arr, deletedIds) {",
                                   "return arr;")


def test_nothing_new_to_send_reports_in_the_bar_instead_of_navigating(source):
    # An empty payload must not open a chat with no attachments (which reads as
    # "the review was sent"). The affordance is the template's existing inline
    # note, not an alert.
    body = _block(source, 'document.getElementById("toclaude").addEventListener',
                  "window.top.location.href = top.pathname")
    early = body[:body.index("const flush = window.__fusedFlushEdits;")]
    assert "if (!payload.length) {" in early
    assert 'document.getElementById("budgetnote").textContent' in early
    assert "return;" in early
    assert "alert(" not in body


def test_reopen_clears_sent_so_a_comment_can_be_sent_again(source):
    # Reopen is the ONLY way back into the payload; without this the flag is a
    # one-way door and a comment can never be re-raised with Claude.
    body = _block(source, "card.querySelector('[data-a=\"resolve\"]').onclick",
                  "save(comments);")
    assert "c.resolved = !c.resolved;" in body
    assert "if (!c.resolved) delete c.sent;" in body


def test_a_sent_card_says_so(source):
    # Otherwise the exclusion is invisible and the button looks broken.
    assert 'const sentTag = c.sent ? chip("sent ↗") : "";' in source
    assert '(c.sent ? " sent" : "")' in source
    assert ".card.sent {" in source


# --- URL budget eviction order ---------------------------------------------


def test_eviction_drops_resolved_before_sent_and_never_an_open_comment(
        source, tmp_path):
    fn = _block(source, "function evictTarget(arr) {", "\n    }")
    pool = json.dumps([
        {"id": "open-oldest", "createdAt": 1},
        {"id": "sent-old", "createdAt": 2, "sent": 1},
        {"id": "resolved-new", "createdAt": 9, "resolved": True},
        {"id": "sent-new", "createdAt": 10, "sent": 1},
        {"id": "resolved-old", "createdAt": 3, "resolved": True},
    ])
    # Drain the pool the way save() does and record the eviction order.
    got = _node(
        fn.strip() + "\nlet arr = " + pool + ";\nconst order = [];\n"
        "for (;;) { const t = evictTarget(arr); if (!t) break;"
        " order.push(t.id); arr = arr.filter((c) => c.id !== t.id); }\n"
        "console.log(JSON.stringify({ order, left: arr.map((c) => c.id) }));\n",
        tmp_path)
    # Resolved tier first (oldest within it), then the sent tier — and the open,
    # unsent comment survives even though it is the oldest thing in the list.
    assert got["order"] == ["resolved-old", "resolved-new", "sent-old", "sent-new"]
    assert got["left"] == ["open-oldest"]


def test_the_budget_loop_uses_that_order_and_still_stops(source):
    body = _block(source, "function save(arr, deletedIds) {", "URL size limit")
    assert "const oldest = evictTarget(arr);" in body
    assert "if (!oldest) break;" in body  # an all-open list must not spin forever
    assert "resolved/sent" in body  # the bar note names both tiers now


# --- the return trip (annotate → claude → annotate) -------------------------
#
# The chat calls fused.autoReload(false) and owns the viewport, so nothing else
# would ever bring the reviewer back to the edited file. The code below lives in
# templates/claude/template.html; the contract is annotate's.

# A shell URL with everything that must survive a round trip: the review itself,
# the annotate `view` param, and the balanced `_layout=(…)` span whose `&` is
# LITERAL — hand that to URLSearchParams unexcised and the layout is shredded.
_LAYOUT = "_layout=(h:(t:code&t:claude),v:preview)"
_SHELL_STUB = """
class Event { constructor(t) { this.type = t; } }
const seen = [];
let search = SEARCH;
const window = { top: {
  location: { get search() { return search; }, pathname: "/view/tmp/note.md",
              href: "http://127.0.0.1:8765/view/tmp/note.md" },
  history: { state: { k: 1 },
             replaceState: (s, t, u) => { seen.push(["replaceState", u]); },
             pushState: (s, t, u) => { seen.push(["pushState", u]); } },
  dispatchEvent: (e) => { seen.push(["event", e.type]); },
} };
"""


def _shell_harness(fns, search, call):
    return (_SHELL_STUB.replace("SEARCH", json.dumps("?" + search))
            + "\n".join(fns) + "\n" + call
            + "\nconsole.log(JSON.stringify(seen));\n")


def test_the_return_ticket_is_stripped_from_the_shell_url_like_the_comments(
        claude_source, tmp_path):
    # claudeReturn is a one-shot boot input, already in memory as `returnMode`.
    # Left on the URL, a Back entry (or a refresh) re-attaches a review that was
    # already sent and re-arms the return — so it rides the SAME replaceState.
    fns = [_block(claude_source, "const HANDOFF_PARAMS = [", "];"),
           _block(claude_source, "function clearTopClaudeComments() {", "\n}")]
    seen = _node(_shell_harness(
        fns,
        "_mode=claude&comments=%5B%7B%22id%22%3A%22c1%22%7D%5D&view=markdown"
        "&claudeComments=%5B%7B%22id%22%3A%22c1%22%7D%5D&claudeReturn=annotate"
        "&" + _LAYOUT,
        "clearTopClaudeComments();"), tmp_path)
    assert [k for k, _ in seen] == ["replaceState", "event"]
    url = seen[0][1]
    assert "claudeComments" not in url
    assert "claudeReturn" not in url
    # The review, the framed view and the layout span come through untouched.
    assert "comments=%5B%7B%22id%22%3A%22c1%22%7D%5D" in url
    assert "view=markdown" in url
    assert url.endswith("&" + _LAYOUT)
    assert seen[1][1] == "fused:urlchange"


def test_a_chat_opened_normally_never_rewrites_the_shell_url(
        claude_source, tmp_path):
    fns = [_block(claude_source, "const HANDOFF_PARAMS = [", "];"),
           _block(claude_source, "function clearTopClaudeComments() {", "\n}")]
    seen = _node(_shell_harness(fns, "_mode=claude&session_id=abc",
                                "clearTopClaudeComments();"), tmp_path)
    assert seen == []


def test_the_return_navigation_flips_the_mode_and_keeps_the_review(
        claude_source, tmp_path):
    fn = _block(claude_source, "function returnToMode(mode) {", "\n}")
    seen = _node(_shell_harness(
        [fn],
        "_mode=claude&comments=%5B%7B%22id%22%3A%22c1%22%2C%22sent%22%3A1%7D%5D"
        "&view=markdown&session_id=abc&run=r-42&" + _LAYOUT,
        'returnToMode("annotate");'), tmp_path)
    # navigateShell's idiom (history/template.html): pushState + fused:navigate,
    # so the React shell re-routes in place instead of reloading the world.
    assert [k for k, _ in seen] == ["pushState", "event"]
    assert seen[1][1] == "fused:navigate"
    url = seen[0][1]
    assert url.startswith("/view/tmp/note.md?")
    assert "_mode=annotate" in url and "_mode=claude" not in url
    # The review survives verbatim — same comments, same sent stamps — and so do
    # the framed view and the resumable session.
    assert "comments=%5B%7B%22id%22%3A%22c1%22%2C%22sent%22%3A1%7D%5D" in url
    assert "view=markdown" in url
    assert "session_id=abc" in url
    # The finished run's id does not ride back: the next hop into chat would
    # re-attach (resumeRun) to a dead run.
    assert "run=" not in url
    assert url.endswith("&" + _LAYOUT)


def test_only_the_comment_carrying_run_returns_and_only_when_it_succeeded(
        claude_source):
    # An in-memory one-shot. A plain follow-up turn must stay in the chat, a
    # failed/aborted run must stay put (nothing to go back and look at, and
    # leaving would hide the error), and a run re-attached on a fresh boot has
    # no ticket at all.
    compose = _block(claude_source, "function composeAndSend(typed) {", "\n}")
    assert "if (returnMode) pendingReturn = true;" in compose
    # Armed inside the `attached.length` branch — never for a bare turn.
    assert compose.index("if (attached.length)") < compose.index("pendingReturn = true")

    poll = _block(claude_source, "async function pollLoop(run_id) {", "\n}")
    assert "if (pendingReturn && !data.error) returnToMode(returnMode);" in poll
    # Cleared however the run ended — including the catch path, which is why the
    # reset lives in `finally` rather than next to the navigation.
    tail = poll[poll.index("} finally {"):]
    assert "pendingReturn = false;" in tail
    assert poll.count("pendingReturn = false;") == 1

    boot = _block(claude_source, "const returnMode =", ";")
    assert 'fused.params.get("claudeReturn")' in boot


def test_annotate_is_what_sets_the_ticket(source):
    body = _block(source, 'document.getElementById("toclaude").addEventListener',
                  "window.top.location.href = top.pathname")
    assert 'params.set("claudeReturn", "annotate");' in body
    # `view` is re-asserted so the round trip can't land on the default sibling
    # view if the shell had not synced annotate's own param yet.
    assert 'const view = fused.params.get("view");' in body
    assert 'if (view) params.set("view", view);' in body
    # `_mode` cannot go through fused.params (reserved name → throws), so the
    # standard-break has to be spelled out where the write happens — this repo
    # documents them in the template comment, next to the code that breaks it.
    preamble = _block(source, "// Send to Claude: hand the NEW comments",
                      'document.getElementById("toclaude").addEventListener')
    assert "DELIBERATE STANDARD-BREAK" in preamble
    assert "reserved" in preamble.lower()
