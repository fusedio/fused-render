"""The Schedule hand-off: the chat writes a URL, the /tasks page reads it.

This is the half of `test_claude_schedule_button.py` that outlived the chat
template. The button's own behaviour (the seat beside Send, the confirm popover,
the draft read at Continue time) is now React and is covered by vitest —
`frontend/src/apps/claude/ui/sched-*.test.tsx` and `sched-draft.test.ts`. What
vitest cannot see is the CONTRACT ACROSS THE TWO APPS: `schedulerUrl` builds the
query string in `apps/claude/ui/sched-draft.ts`, and `shell/Scheduled.tsx` is a
different bundle that has to read back the very same key names. A rename on
either side leaves the button navigating to a page that silently ignores it —
no error, no modal, nothing to debug from.

So this file greps the TypeScript as text, which is the established shape for a
cross-language contract in this suite (see `tests/test_trouble_parity.py`).
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_ROOT, "frontend", "src")
_DRAFT = os.path.join(_FRONTEND, "apps", "claude", "ui", "sched-draft.ts")
_PAGE = os.path.join(_FRONTEND, "shell", "Scheduled.tsx")
_MODAL = os.path.join(_FRONTEND, "shell", "NewJobModal.tsx")

# The five keys that travel. `new` opens the form, the other four are what the
# page cannot know and the chat always does.
HOP_PARAMS = ("new", "target", "message", "session_id", "back")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def draft() -> str:
    return _read(_DRAFT)


@pytest.fixture(scope="module")
def link(draft) -> str:
    """Just `schedulerUrl`'s body — the one place the URL is composed."""
    body = draft[draft.index("export function schedulerUrl("):]
    return body[:body.index("\n}")]


@pytest.fixture(scope="module")
def page() -> str:
    return _read(_PAGE)


@pytest.fixture(scope="module")
def modal() -> str:
    return _read(_MODAL)


# ------------------------------------------------------- the chat's half


def test_the_hop_carries_the_target_the_draft_the_session_and_the_way_back(link):
    """Everything the page cannot know and the chat always does. `back` is how
    the Schedule page offers "Back to chat" — without it the hop is one-way and
    the reader has to find their conversation again by hand."""
    assert "new=1" in link
    assert "&target=${encodeURIComponent(link.file ?? \"\")}" in link
    assert "&message=${encodeURIComponent(link.draft || \"\")}" in link
    assert "&session_id=${encodeURIComponent(link.sessionId || \"\")}" in link
    assert "&back=${encodeURIComponent(link.back)}" in link


def test_every_value_that_travels_is_encoded(link):
    """A draft is free text and a target is a path: neither may be pasted into a
    query string raw, or an `&` in the words truncates the hop."""
    for param in ("target", "message", "session_id", "back"):
        pair = re.search(rf"&{param}=\$\{{([^}}]*)", link)
        assert pair, f"{param} is not written by schedulerUrl"
        assert "encodeURIComponent(" in pair.group(1), f"{param} travels unencoded"


def test_an_empty_draft_is_still_a_valid_hop(link):
    """The page's form is where the description gets written, and arriving there
    with a blank field is exactly what "+ New task" already does. Nothing is
    validated here — this navigates, it does not create."""
    assert 'link.draft || ""' in link
    for refusal in ("if (!", "throw ", "return null"):
        assert refusal not in link, f"{refusal!r} — this builds a URL, it does not judge one"


def test_the_configuration_of_this_chat_does_not_travel(link):
    """A task runs unattended and the page owns those answers — "ask every time"
    cannot work with nobody watching. Sending the chat's own model, effort or
    approvals mode would make the modal a second composer with a worse box and a
    rule it did not choose."""
    for leaked in ("model", "effort", "permission_mode", "repeats", "due"):
        assert leaked not in link, f"{leaked} has no business in the handoff"


def test_the_chat_does_not_reimplement_scheduling(draft):
    """One writer for the schedule store, and it is the page. A fetch here would
    be a second, competing create path with a different set of defaults.

    Comments stripped first: this module's own prose NAMES the endpoint it says
    it does not call, and would otherwise satisfy the search."""
    code = re.sub(r"/\*.*?\*/", "", draft, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "fetch(" not in code
    assert "/api/schedule" not in code
    assert 'export const SCHEDULE_URL = "/tasks";' in code


# ------------------------------------------------------- the page's half


def test_the_page_reads_the_params_the_chat_writes(link, page):
    """The contract, spelled in two bundles. A rename on either side leaves the
    button navigating to a Schedule page that simply ignores it."""
    assert 'q.get("new") !== "1"' in page
    for param in HOP_PARAMS:
        assert f"&{param}=" in link or param == "new", f"the chat stopped writing {param}"
        assert f'q.get("{param}")' in page, f"the page ignores {param}"


def test_the_link_opens_the_form_immediately(page):
    """Landing on a page with a button still to press would make one control
    read as two."""
    effect = page[page.index('q.get("new")'):]
    effect = effect[:effect.index("}, []);")]
    assert "const at = new Date(Date.now() + NEW_LINK_LEAD_MS);" in effect
    assert "openForm(at, null, seed);" in effect
    # The handoff travels as ONE seed handed to that door: the six loose page
    # states undone one by one are what let a hop's attachments leak into every
    # later modal.
    assert "const seed: HopSeed" in effect
    assert ", null, seed)" in effect
    # A hop out of a conversation looks first: a task draft bound to a session
    # has no row of its own, so a second press must reopen THAT form rather than
    # mint a second one bound to the same thread. A failed lookup is "unknown",
    # not "none" — the composer's words must not wait on a GET.
    assert "all && boundDraftSeed(all.task, session, seed.message," in effect
    assert "seed.attachments)" in effect


def test_the_prefilled_time_is_valid_the_moment_it_opens(page):
    """The field is minute-precision and the form refuses a due time at or
    before now, so a value inside the CURRENT minute opens the modal already
    complaining about a time the user never picked."""
    assert "NEW_LINK_LEAD_MS" in page
    lead = re.search(r"const NEW_LINK_LEAD_MS = ([0-9_]+);", page)
    assert lead, "the lead constant moved"
    assert int(lead.group(1).replace("_", "")) >= 60_000


def test_the_params_are_consumed_not_just_read(page):
    """Otherwise a reload — or Back to here from wherever the user went next —
    reopens the modal forever. replaceState, not push: the deep-linked URL is
    not a place worth keeping in the history."""
    effect = page[page.index('q.get("new")'):]
    effect = effect[:effect.index("}, []);")]
    for param in HOP_PARAMS:
        assert f'q.delete("{param}")' in effect, f"{param} outlives its own navigation"
    assert "history.replaceState(" in effect
    assert "pushState" not in effect


def test_the_deep_linked_values_do_not_outlive_their_own_modal(page):
    """Left standing they would prefill the next "+ New task" with a folder, a
    draft and a session the user arrived from some time ago — the same class of
    bug as a When pill that survived its send."""
    assert "seed: HopSeed = NO_HOP" in page
    assert "setHop(seed);" in page
    for stale in ("setNewTarget(", "setNewMessage(", "setNewAttachments(",
                  "setNewSession(", "setNewBack("):
        assert stale not in page, f"{stale} is the shape that leaked"


# ------------------------------------------------------- the modal


def test_the_link_beats_the_guess_and_an_edit_beats_the_link(modal):
    """Three sources for one field, in order: a stored target (Edit), the folder
    a link named, and only then defaultTargetOf() — the server's resolved
    workspace, which is what you offer when nobody said. ONE const, read twice,
    so the state and the dirty baseline cannot disagree."""
    assert modal.count("const initialTargetValue = ") == 1
    assert 'editing?.target ?? initialTarget ?? ""' in modal, \
        "Edit beats the link beats the guess"
    assert modal.count("initialTargetValue") == 3, \
        "the const, the state seed, and the dirty baseline"
    # the async default only fills a still-EMPTY field, which is what keeps it
    # from clobbering the link's target when getConfig resolves
    effect = modal[modal.index("getConfig().then("):]
    effect = effect[:effect.index("}, []);")]
    assert 'prev === "" ? fallback : prev' in effect
    assert 'prev.target === "" ?' in effect


def test_a_prefilled_target_does_not_read_as_dirty(modal):
    """The chassis' close-twice guard must fire on "the user typed something".
    Counting a prefill as dirty is what made ✕ look broken (QA 2026-08-14)."""
    baseline = modal[modal.index("const [initial, setInitial] = useState(() => ({"):]
    baseline = baseline[:baseline.index("}));")]
    assert "initialTarget" in baseline
