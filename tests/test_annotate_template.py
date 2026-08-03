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
import re
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


def _card_opacity(src, classes, reverse_order=False):
    """Resolve the real CSS cascade for a card carrying `classes`.

    Class-chain rules only (`.card.resolved` …), which is all the card styling
    is. Winner = highest specificity (class count), then declaration order —
    `reverse_order` flips that tie-break to prove a result does NOT depend on it.
    """
    style = src[src.index("<style>"):src.index("</style>")]
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
    best = None
    for order, m in enumerate(re.finditer(r"([^{}]+)\{([^{}]*)\}", style)):
        sel = m.group(1).strip()
        hit = re.search(r"(?:^|;)\s*opacity\s*:\s*([0-9.]+)", m.group(2))
        if not hit or not re.fullmatch(r"(?:\.[A-Za-z0-9_-]+)+", sel):
            continue
        need = set(sel.split(".")[1:])
        if not need <= set(classes):
            continue
        key = (len(need), -order if reverse_order else order)
        if best is None or key > best[0]:
            best = (key, float(hit.group(1)))
    return None if best is None else best[1]


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
    assert body.index("const persisted = save(all,") < body.index(
        "window.top.location.href")
    # And the payload is what crosses over — not the whole store.
    assert 'params.set("claudeComments", wire);' in body
    # The navigation URL is built from a RAW read of window.top's search, but
    # fused.params.set coalesces its history write (D99) — so the stamps may not
    # be in that string yet. Writing it back verbatim would undo them and
    # re-send the same comments next time; the persisted list is re-asserted.
    assert 'params.set("comments", JSON.stringify(persisted));' in body
    assert "return arr;" in _block(source, "function save(arr, deletedIds, keep) {",
                                   "return arr;")


def test_resolved_outranks_sent_on_a_card_that_is_both(source):
    # The two flags are independent, so a card can carry both. `.card.resolved`
    # and `.card.sent` are equal specificity, so the tie went to whichever was
    # written second — `.sent` was, so a RESOLVED card rendered at the weaker 0.8
    # and read as prominent as an open one. Resolved is the terminal state and
    # takes the stronger dim; the point of this test is that the precedence holds
    # on SPECIFICITY, so reordering the stylesheet cannot silently flip it.
    both = {"card", "resolved", "sent"}
    assert _card_opacity(source, {"card", "resolved"}) == 0.6
    assert _card_opacity(source, {"card", "sent"}) == 0.8
    assert _card_opacity(source, both) == 0.6
    # Same answer with the declaration-order tie-break inverted.
    assert _card_opacity(source, both, reverse_order=True) == 0.6
    # An open card is undimmed by any of these rules.
    assert _card_opacity(source, {"card"}) is None


def test_a_resolved_card_does_not_also_claim_to_be_sent(source):
    # One state, one signal: resolved already explains why the comment is skipped
    # (and owns the dim), so the "sent ↗" chip would be a second, weaker reason
    # stacked on the terminal one. Reopen clears `sent`, so the chip can never
    # reappear stale on a reopened card either.
    assert 'const sentTag = c.sent && !c.resolved ? chip("sent ↗") : "";' in source


def test_the_agent_never_sees_the_internal_sent_flag(source):
    # `sent` is bookkeeping for the URL store. formatComments (claude template)
    # hands the agent the raw JSON with every field intact and enumerates the
    # fields by name, so a stamp serialized into the payload would show up as a
    # mystery key for the model to reason about.
    body = _block(source, 'document.getElementById("toclaude").addEventListener',
                  "window.top.location.href = top.pathname")
    assert body.index("const wire = JSON.stringify(payload);") < body.index(
        "for (const c of payload) c.sent = 1;")


def test_the_staleness_sweep_covers_every_param_the_top_url_carries(source):
    # Re-asserting `comments` and `view` by hand missed offset/sheet: a reveal
    # inside the 400 ms coalescing window would return the user to page 1 of a
    # table/pdf. The sweep is the general fix — and it must only touch keys the
    # top URL already has, so a pane-local param is never pushed onto the shell.
    body = _block(source, 'document.getElementById("toclaude").addEventListener',
                  "window.top.location.href = top.pathname")
    assert "const live = fused.params.getAll();" in body
    assert "for (const k of [...params.keys()]) {" in body
    assert 'if (typeof live[k] === "string") params.set(k, live[k]);' in body
    # Before the handoff params are written, so the sweep cannot revert them.
    assert body.index("const live = fused.params.getAll();") < body.index(
        'params.set("_mode", "claude");')


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
    assert 'chip("sent ↗")' in source
    assert '(c.sent ? " sent" : "")' in source
    assert ".card.sent {" in source


# --- URL budget eviction order ---------------------------------------------


def test_eviction_drops_resolved_before_sent_and_never_an_open_comment(
        source, tmp_path):
    fn = _block(source, "function evictTarget(arr, keep) {", "\n    }")
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


def test_a_comment_being_sent_is_never_the_eviction_victim(source, tmp_path):
    # The save that STAMPS `sent` is also the save that can evict on it. With
    # nothing resolved and the list sitting just under BUDGET, the stamp pushes
    # it over, the sent tier is then the ENTIRE payload, and the oldest comment
    # in the review gets deleted from the only store there is — silently, since
    # the "removed" note lands in a bar the navigation destroys milliseconds
    # later. Ids being sent right now are off-limits.
    fn = _block(source, "function evictTarget(arr, keep) {", "\n    }")
    arr = json.dumps([
        {"id": "a", "createdAt": 1, "sent": 1},
        {"id": "b", "createdAt": 2, "sent": 1},
        {"id": "c", "createdAt": 3},
    ])
    got = _node(
        fn.strip() + "\nconst arr = " + arr + ";\n"
        # The whole sent tier is the payload being handed over right now.
        "const sending = evictTarget(arr, new Set(['a', 'b']));\n"
        # Same list, but 'a' rode an EARLIER handoff — then it is fair game.
        "const earlier = evictTarget(arr, new Set(['b']));\n"
        "console.log(JSON.stringify({ sending: sending && sending.id,"
        " earlier: earlier && earlier.id, none: evictTarget(arr, new Set()) }));\n",
        tmp_path)
    assert got["sending"] is None  # nothing evictable → over-budget write kept
    assert got["earlier"] == "a"
    assert got["none"] == {"id": "a", "createdAt": 1, "sent": 1}


def test_the_send_protects_its_own_payload_from_eviction(source):
    body = _block(source, 'document.getElementById("toclaude").addEventListener',
                  "window.top.location.href = top.pathname")
    assert ("const persisted = save(all, undefined, "
            "new Set(payload.map((c) => c.id)));") in body
    # An over-budget write that keeps every comment is the accepted outcome —
    # trading a live comment for a flag is not — and the send still proceeds.
    assert body.index("const persisted = save(all,") < body.index(
        'params.set("_mode", "claude")')


def test_the_budget_loop_uses_that_order_and_still_stops(source):
    body = _block(source, "function save(arr, deletedIds, keep) {", "URL size limit")
    assert "const oldest = evictTarget(arr, keep);" in body
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
// The pane's own window: `pagehide` is the runtime's documented flush hook.
window.dispatchEvent = (e) => { seen.push(["self", e.type]); };
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
           _block(claude_source, "function spliceLayoutSpan(full) {", "\n}"),
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
           _block(claude_source, "function spliceLayoutSpan(full) {", "\n}"),
           _block(claude_source, "function clearTopClaudeComments() {", "\n}")]
    seen = _node(_shell_harness(fns, "_mode=claude&session_id=abc",
                                "clearTopClaudeComments();"), tmp_path)
    assert seen == []


def test_the_return_navigation_flips_the_mode_and_keeps_the_review(
        claude_source, tmp_path):
    fn = _block(claude_source, "function returnToMode(mode) {", "\n}")
    splice = _block(claude_source, "function spliceLayoutSpan(full) {", "\n}")
    seen = _node(_shell_harness(
        [splice, fn],
        "_mode=claude&comments=%5B%7B%22id%22%3A%22c1%22%2C%22sent%22%3A1%7D%5D"
        "&view=markdown&session_id=abc&run=r-42&" + _LAYOUT,
        'returnToMode("annotate");'), tmp_path)
    # The runtime's coalesced history write is flushed first, then navigateShell's
    # idiom (history/template.html): pushState + fused:navigate, so the React
    # shell re-routes in place instead of reloading the world.
    assert [k for k, _ in seen] == ["self", "pushState", "event"]
    assert seen[0][1] == "pagehide"
    assert seen[2][1] == "fused:navigate"
    url = seen[1][1]
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
    # The ticket is threaded, not latched. A plain follow-up turn must stay in
    # the chat, a failed/aborted run must stay put (nothing to go back and look
    # at, and leaving would hide the error), and a run re-attached on a fresh
    # boot has no ticket at all.
    compose = _block(claude_source, "function composeAndSend(typed) {", "\n}")
    assert "handoff = { mode: returnMode, comments: attached };" in compose
    # Built inside the `attached.length` branch — a bare turn passes null.
    assert compose.index("if (attached.length)") < compose.index("handoff = {")
    assert "sendMessage(message, handoff);" in compose

    poll = _block(claude_source, "async function pollLoop(run_id, handoff) {", "\n}")
    assert 'const returnTo = (handoff && handoff.mode) || "";' in poll
    assert "if (returnTo && !data.error) returnToMode(returnTo);" in poll

    boot = _block(claude_source, "const returnMode =", ";")
    assert 'fused.params.get("claudeReturn")' in boot


def test_a_run_that_never_started_leaves_no_ticket_and_hands_the_chips_back(
        claude_source):
    # The regression this replaces: the ticket was a module-scoped latch armed
    # BEFORE sendMessage, and `if (error) throw` on a failed `agent.py start`
    # never reaches pollLoop — the only place that cleared it. The user's next,
    # unrelated question then navigated the shell away mid-thread, for a run that
    # carried no comments, while annotate had already stamped them `sent`.
    assert "pendingReturn" not in claude_source, (
        "the return ticket must stay an argument — a module-scoped latch "
        "outlives the send that armed it")
    send = _block(claude_source, "async function sendMessage(message, handoff) {",
                  "\n}")
    # Bound to a run id: armed only once `start` handed one back.
    assert "let started = false;" in send
    assert send.index("started = true;") > send.index('if (error) throw')
    assert send.index("started = true;") < send.index("await pollLoop(run_id, handoff)")
    # And the comments are not silently consumed by a run that never ran.
    assert "if (!started && handoff) {" in send
    assert "attached = handoff.comments;" in send
    assert "renderAttachments();" in send
    assert "were not sent" in send  # the user is told, rather than left guessing

    # A resumed run gets no second argument, so it can never return.
    resume = _block(claude_source, "async function resumeRun(run_id) {", "\n}")
    assert "pollLoop(run_id)" in resume
    assert "handoff" not in resume


def test_the_return_flushes_the_runtimes_pending_history_write_first(
        claude_source):
    # returnToMode reads location.search RAW and then pushState()s. A history
    # write the runtime is still coalescing (D99) would both be missing from the
    # string it copies forward (a pending session_id) and, worse, land AFTER our
    # push — replaceState-ing the pre-return search over the entry we just made.
    fn = _block(claude_source, "function returnToMode(mode) {", "\n}")
    flush = fn.index('dispatchEvent(new Event("pagehide"))')
    assert flush < fn.index("top.location.search")
    assert flush < fn.index("top.history.pushState")


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
