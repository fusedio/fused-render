"""The Schedule hand-off: the chat writes a URL, the /tasks page reads it.

This is the half of `test_claude_schedule_button.py` that outlived the chat
template. The button's own behaviour (the seat beside Send, the confirm popover,
the draft read at Continue time) is now React and is covered by vitest —
`frontend/src/apps/claude/ui/sched-*.test.tsx` and `sched/scheduled.test.ts`.
What vitest cannot see is the CONTRACT ACROSS THE TWO APPS: `schedulerUrl`
builds the query string in `apps/claude/sched/scheduled.ts`, and
`shell/Scheduled.tsx` is a different bundle that has to read back the very same
key names. A rename on either side leaves the button navigating to a page that
silently ignores it — no error, no modal, nothing to debug from.

So this file greps the TypeScript as text, which is the established shape for a
cross-language contract in this suite (see `tests/test_trouble_parity.py`).
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_ROOT, "frontend", "src")
# The hop's URL lives in the chat app's schedule vocabulary rather than beside
# the button that first built it, so a draft ROW can press exactly the same one
# without dragging the button — and its popover — into the rows' module
# (Akshil, 2026-09-16).
_HOP = os.path.join(_FRONTEND, "apps", "claude", "sched", "scheduled.ts")
_SCHED_BUTTON = os.path.join(_FRONTEND, "apps", "claude", "ui", "SchedButton.tsx")
_PAGE = os.path.join(_FRONTEND, "shell", "Scheduled.tsx")
_MODAL = os.path.join(_FRONTEND, "shell", "NewJobModal.tsx")

# The keys that travel. `new` opens the form; `draft` names the record the card
# is about to edit, `target` the folder a session key cannot state, and `from`
# where "Back to chat" lands.
HOP_PARAMS = ("new", "draft", "target", "from")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sched() -> str:
    return _read(_HOP)


@pytest.fixture(scope="module")
def hop(sched) -> str:
    """Just `schedulerUrl`'s body — the one place the URL is composed."""
    body = sched[sched.index("export function schedulerUrl("):]
    return body[:body.index("\n}")]


@pytest.fixture(scope="module")
def sched_button_code() -> str:
    """SchedButton.tsx with comments stripped: the file's own docstring
    RECOUNTS the old shape it replaced — a sessionStorage stash, a `?message=`
    param — to explain why it is gone, and an absence check has to search the
    code, not the history lesson."""
    src = _read(_SCHED_BUTTON)
    without_block = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.M)


@pytest.fixture(scope="module")
def page() -> str:
    return _read(_PAGE)


@pytest.fixture(scope="module")
def modal() -> str:
    return _read(_MODAL)


# ------------------------------------------------------- the chat's half


def test_the_hop_carries_the_record_the_folder_and_the_way_back(hop):
    """Everything the page cannot know and the chat always does. Only the KEY
    and the way back travel now (design "one record", §1): the words, the tray
    and the session used to ride as `?message=`, `?attachments=` and
    `?session_id=`, three copies of a thing the server already holds. `target`
    is not a fourth copy — it is the one fact a session KEY cannot state, and
    without it the card opened on the reader's home folder and wrote that home
    path onto the conversation's own record (Akshil, 2026-09-16)."""
    assert "?new=1&draft=" in hop
    assert "&target=" in hop
    assert "&from=" in hop


def test_every_value_that_travels_is_encoded(hop):
    """A draft key can hold a path and a `from` is a URL: neither may be pasted
    into a query string raw, or an `&` in the value truncates the hop."""
    for param in ("draft", "target", "from"):
        pair = re.search(rf"[?&]{param}=\$\{{([^}}]*)", hop)
        assert pair, f"{param} is not written by schedulerUrl"
        assert "encodeURIComponent(" in pair.group(1), f"{param} travels unencoded"


def test_an_absent_folder_or_route_back_is_still_a_valid_hop(hop):
    """A row on the Tasks page has nowhere to walk back to, and a key that
    carries its own folder needs no `target`. Nothing is validated here — this
    navigates, it does not create."""
    assert "target ?" in hop
    assert "from ?" in hop
    for refusal in ("if (!", "throw ", "return null"):
        assert refusal not in hop, f"{refusal!r} — this builds a URL, it does not judge one"


def test_the_configuration_of_this_chat_does_not_travel(hop):
    """A task runs unattended and the page owns those answers — "ask every time"
    cannot work with nobody watching. Sending the chat's own model, effort or
    approvals mode would make the modal a second composer with a worse box and a
    rule it did not choose."""
    for leaked in ("model", "effort", "permission_mode", "repeats", "due"):
        assert leaked not in hop, f"{leaked} has no business in the handoff"


def test_the_chat_does_not_reimplement_scheduling(sched_button_code):
    """One writer for the schedule store, and it is the page. A fetch in the
    button would be a second, competing create path with a different set of
    defaults."""
    assert "/api/schedule" not in sched_button_code


def test_the_hop_carries_no_sentence_of_its_own(sched_button_code):
    """THE HOP NO LONGER CARRIES A SENTENCE (design "one record", §1): the
    button used to stash a copy in `sessionStorage`, a copy on the URL as
    `&message=`, and let the task form mint a THIRD under `stashDraft` on
    arrival — three copies of one half-written thing, and every bug in this
    feature was two of them disagreeing. Only the record's key crosses now."""
    for gone in ("&message=", "stashDraft", "sessionStorage"):
        assert gone not in sched_button_code, f"{gone} outlived the one-record redesign"


# ------------------------------------------------------- the page's half


def test_the_page_reads_the_params_the_chat_writes(hop, page):
    """The contract, spelled in two bundles. A rename on either side leaves the
    button navigating to a Schedule page that simply ignores it."""
    assert 'q.get("new") !== "1"' in page
    for param in HOP_PARAMS:
        assert f"{param}=" in hop, f"the chat stopped writing {param}"
        if param != "new":
            assert f'q.get("{param}")' in page, f"the page ignores {param}"


def test_the_link_opens_the_form_immediately(page):
    """Landing on a page with a button still to press would make one control
    read as two."""
    effect = page[page.index('q.get("new")'):]
    effect = effect[:effect.index("}, []);")]
    assert "const at = new Date(Date.now() + NEW_LINK_LEAD_MS);" in effect
    # A bare `?new=1` with no key — the app page's own "+ New task" link, and an
    # EMPTY never-sent composer's Schedule, which has no record to hand over —
    # opens on the lead date with nothing stored behind it. It still honours the
    # folder and the route back when the link names them.
    assert "openForm(\n        at,\n        null," in effect
    assert 'from ? { key: "", from } : NO_HOP,' in effect
    assert 'at0 ? { id: "", form: { target: at0 } } : null,' in effect
    # A KEYED HOP READS THE RECORD FIRST (design "one record", §1), through the
    # one function every door onto a chat record takes — the hop, a draft row's
    # press and the bound-form line — so the seeding rule cannot be right in one
    # of them and wrong in another.
    assert 'openChatRecord(key, q.get("from") ?? "", q.get("target") ?? "");' in effect
    assert "const found = all && chatHopSeed(key, all.chat[key] ?? null, at);" in page
    # A FAILED LOOKUP IS "UNKNOWN", NOT "NONE" (`fetchDrafts` answers null for a
    # blip): the card opens anyway, on the key it was given, in both branches.
    assert "openForm(found ? reopenTime(found) : lead, null, hopTo, found);" in page
    assert "openForm(lead, null, hopTo);" in page


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
    for param in (*HOP_PARAMS, "edit"):
        assert f'q.delete("{param}")' in effect, f"{param} outlives its own navigation"
    assert "history.replaceState(" in effect
    assert "pushState" not in effect


def test_the_deep_linked_values_do_not_outlive_their_own_modal(page):
    """Left standing they would prefill the next "+ New task" with a folder, a
    draft and a session the user arrived from some time ago — the same class of
    bug as a When pill that survived its send."""
    # The hop is one value the OPENING seeds — `openForm(at, entry, seed =
    # NO_HOP)` — so a plain "+ New task" carries no hop by construction and the
    # close has nothing to forget. The old shape (six page states cleared one by
    # one in `onClose`) is what let a hop's attachments outlive their modal: one
    # setter was missing from the list.
    assert "seed: ChatHop = NO_HOP" in page
    assert "setHop(seed);" in page
    for stale in ("setNewTarget(", "setNewMessage(", "setNewAttachments(",
                  "setNewSession(", "setNewBack(", "seed: HopSeed"):
        assert stale not in page, f"{stale} is the shape that leaked"


# ------------------------------------------------------- the modal


def test_the_link_beats_the_guess_and_an_edit_beats_the_link(modal):
    """Three sources for one field, in order: a stored target (Edit), the folder
    a link named, and only then defaultTargetOf() — the server's resolved
    workspace, which is what you offer when nobody said. ONE const, read
    everywhere, so the state, the dirty baseline and the folder field cannot
    disagree."""
    assert modal.count("const initialTargetValue = ") == 1
    assert 'editing?.target ?? initialTarget ?? ""' in modal, \
        "Edit beats the link beats the guess"
    assert modal.count("initialTargetValue") == 5, \
        ("the const, the state seed, the dirty baseline, and the folder field's "
         "own default (`folderFieldRows`' `defaultTarget` argument and its dep, "
         "which decide whether typing in the field searches the page's projects "
         "or leaves the remembered folders alone)")
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
