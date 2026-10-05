"""The composer's calendar BUTTON — the hop from the chat to the Schedule page
(frontend/src/apps/claude/ui/SchedButton.tsx -> frontend/src/shell/Scheduled.tsx).

There used to be a "Send now" pill beside it that deferred ONE message from the
composer itself, with no title, no description and no repeat rule (its own suite
was test_claude_schedule_pill.py). It is gone (Akshil, 2026-08-16): a row of
send-later presets sat permanently in front of every user to serve the rarest
thing they do with a draft, and everything past a bare deferral was the Schedule
page's form anyway. So the composer keeps one button whose whole job is the
HANDOFF — and the handoff carries the three things the page cannot know and this
template always does: the folder, the words already typed, and the conversation
they were written in.

Structural assertions over the two sources, the same approach test_claude_kind.py
takes: this is inline vanilla JS in a 12000-line document on one side and a React
page on the other, so what can be pinned is that the wiring exists and that the
contract between the two halves agrees. The contract IS duplicated — five param
names spelled in two files (D146: a duplicated rule needs a test, not a comment) —
which is most of what is below.
"""
import os
import re

import pytest

_PAGE = os.path.join("frontend", "src", "shell", "Scheduled.tsx")
_MODAL = os.path.join("frontend", "src", "shell", "NewJobModal.tsx")
_SCHED_BUTTON = os.path.join("frontend", "src", "apps", "claude", "ui", "SchedButton.tsx")
# The hop's URL itself moved off the button and into the chat app's schedule
# vocabulary, so a draft ROW can press exactly the same one without dragging the
# button — and its popover — into the rows' module (Akshil, 2026-09-16).
_HOP = os.path.join("frontend", "src", "apps", "claude", "sched", "scheduled.ts")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def page() -> str:
    return _read(_PAGE)


@pytest.fixture(scope="module")
def modal() -> str:
    return _read(_MODAL)


@pytest.fixture(scope="module")
def sched_button() -> str:
    return _read(_SCHED_BUTTON)


@pytest.fixture(scope="module")
def hop() -> str:
    return _read(_HOP)


@pytest.fixture(scope="module")
def sched_button_code(sched_button) -> str:
    """SchedButton.tsx with comments stripped, for the same reason `code` above
    strips template.html's: the file's own docstring RECOUNTS the old shape it
    replaced — a sessionStorage stash, a `?message=` param — to explain why it
    is gone, and an absence check has to search the code, not the history
    lesson, or it would fail on the very sentence that says the thing is gone."""
    without_block = re.sub(r"/\*.*?\*/", "", sched_button, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.M)


# ------------------------------------------------------------- the template


# ------------------------------------------------------------- the confirm


# ------------------------------------------------------------- the hop


# ------------------------------------------------------------- the page


def test_the_page_reads_the_params_the_template_writes(page, hop):
    """The contract, spelled in two files now — `SchedButton.schedulerUrl` is
    the one writer of the hop and this page's `?new=1` arm is the one reader.
    A rename on either side leaves the button navigating to a Schedule page
    that simply ignores it — no error, no modal, nothing to debug from. Only
    the KEY and the way back travel now (design "one record", §1): the words,
    the tray and the session used to ride as `?message=`, `?attachments=` and
    `?session_id=`, three copies of a thing the server already holds."""
    assert "?new=1&draft=" in hop
    assert "&from=" in hop
    # …and the FOLDER, which is the one fact a session key cannot state: without
    # it the card opened on the reader's home and wrote that home path onto the
    # conversation's own record (Akshil, 2026-09-16).
    assert "&target=" in hop
    assert 'q.get("new") !== "1"' in page
    for param in ("draft", "target", "from"):
        assert f'q.get("{param}")' in page, f"the page ignores {param}"


def test_the_link_opens_the_form_immediately(page):
    """Landing on a page with a button still to press would make one control
    read as two."""
    effect = page[page.index('q.get("new")'):]
    effect = effect[:effect.index("}, []);")]
    # `openForm` rather than `setCreating` since 2026-08-18: every opening of the
    # form goes through one door, and that door is what bumps the modal's React
    # key so a fresh card cannot inherit the previous one's answers. The deep link
    # is an opening like any other, and what this test cares about is unchanged —
    # it opens on arrival, prefilled, with no button left to press.
    assert "const at = new Date(Date.now() + NEW_LINK_LEAD_MS);" in effect
    # A bare `?new=1` with no key — the app page's own "+ New task" link, and an
    # EMPTY never-sent composer's Schedule, which has no record to hand over —
    # opens on the lead date with nothing stored behind it. It still honours the
    # folder and the route back when the link names them (Akshil, 2026-09-16):
    # a blank card that opened on the reader's home with no way out would be the
    # press half working.
    assert "openForm(\n        at,\n        null," in effect
    assert 'from ? { key: "", from } : NO_HOP,' in effect
    assert 'at0 ? { id: "", form: { target: at0 } } : null,' in effect
    # A KEYED HOP READS THE RECORD FIRST (design "one record", §1). The key that
    # travels is the chat's own draft key — not a `HopSeed` built out of loose
    # words, a tray and a session id that used to ride the URL — and turning
    # what is stored under that key into the seed is ONE call, `chatHopSeed`,
    # not a merge to get wrong.
    # …through the one function every door onto a chat record now takes — the
    # hop, a draft row's press and the bound-form line — so the seeding rule
    # cannot be right in one of them and wrong in another.
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
    for param in ("new", "draft", "target", "from", "edit"):
        assert f'q.delete("{param}")' in effect, f"{param} outlives its own navigation"
    assert "history.replaceState(" in effect
    assert "pushState" not in effect


def test_the_deep_linked_values_do_not_outlive_their_own_modal(page, sched_button_code):
    """Left standing they would prefill the next "+ New task" with a folder, a
    draft and a session the user arrived from some time ago — the same class of bug
    as a When pill that survived its send."""
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
    # THE HOP ITSELF NO LONGER CARRIES A SENTENCE (design "one record", §1): the
    # button used to stash a copy in `sessionStorage`, a copy on the URL as
    # `&message=`, and let the task form mint a THIRD under `stashDraft` on
    # arrival — three copies of one half-written thing, and every bug in this
    # feature was two of them disagreeing. Only the record's key crosses now.
    for gone in ("&message=", "stashDraft", "sessionStorage"):
        assert gone not in sched_button_code, f"{gone} outlived the one-record redesign"


# ------------------------------------------------------------- the modal


def test_the_link_beats_the_guess_and_an_edit_beats_the_link(modal):
    """Three sources for one field, in order: a stored target (Edit), the folder
    a link named, and only then defaultTargetOf() — the server's resolved
    workspace, which is what you offer when nobody said."""
    # ONE const, read twice — not the same expression written out twice
    # (Akshil, 2026-09-11). It used to be the literal in both seats and this
    # counted them; the draft work added a fourth source in front of the three
    # (a re-opened draft's own stored target), and repeating a four-term
    # precedence chain in two places is exactly the drift the count was guarding
    # against. The guarantee is unchanged and now structural: there is one
    # expression, so the state and the dirty baseline cannot disagree.
    assert modal.count('const initialTargetValue = ') == 1
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
    Counting a prefill as dirty is what made ✕ look broken (QA 2026-08-14),
    twice already — this is the same bug one prefill earlier."""
    baseline = modal[modal.index("const [initial, setInitial] = useState(() => ({"):]
    baseline = baseline[:baseline.index("}));")]
    assert "initialTarget" in baseline


