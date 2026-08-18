"""A scheduled task firing INTO this chat (templates/claude/template.html).

The composer used to schedule on its own — the "Send now" pill, deleted
2026-08-16 — and this file was its suite. What is left is the other half, which
outlived it: a task is stored by the Schedule page and spawned by the SERVER, so
nothing in this page ever sets `activeRun`, and a chat left open past its own
scheduled time would otherwise watch the session run, finish and edit files with
no sign of any of it. The watcher below is what puts that turn on screen.

The composer's side of scheduling now lives in test_claude_schedule_button.py:
one calendar button that hands the draft, the folder and the session to the
Schedule page. These are structural assertions over the template source, the same
approach test_claude_kind.py takes — inline vanilla JS in a 12000-line document,
so what can be pinned is that the wiring exists and that the properties it would
be easy to get wrong stay true.
"""
import os
import re

import pytest

_TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def code(source) -> str:
    """The template with comments stripped — every "X is not there" assertion
    needs it, because this file's comments RECORD the decisions and would
    otherwise satisfy a search for the thing they say was rejected."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


def test_the_model_and_effort_pills_validate_their_param(code):
    """A URL carrying `?model=` with anything outside MODELS set a <select> value
    matching no option, which renders as a BLANK pill — fitSelect early-returns with
    no selected option and the caret sits alone in an unfitted box. The perm pill
    always validated; these two did not, and `curModel()` is also what a run is
    launched with, so an unknown model reached the CLI too."""
    for fn, values, default in (("curModel", "MODELS", "DEFAULT_MODEL"),
                                ("curEffort", "EFFORTS", "DEFAULT_EFFORT")):
        body = code[code.index(f"const {fn} = ()"):]
        body = body[:body.index("\n};")]
        assert f"{values}.includes(want)" in body, f"{fn} does not validate"
        assert default in body


def test_a_chat_left_open_picks_up_its_own_scheduled_send(code):
    """The gap this closes, reported from use: a scheduled message is spawned by
    the SERVER, so nothing in the page ever set `activeRun` and the turn ran
    entirely off-screen. A chat left open past its own scheduled time sat on
    "Scheduled for 12:20" while the session ran, finished and edited files.

    It goes through `resumeRun`, which is already written for a run this frame did
    not start — live (stream the rest) or already finished (append it, see
    neverShown) — rather than a second attach path beside it."""
    assert "function pollScheduledRuns(" in code
    body = code[code.index("async function pollScheduledRuns("):]
    body = body[:body.index("\npollScheduledRuns();")]
    assert "resumeRun(entry.run_id, { neverShown: true })" in body
    assert 'e.target === FILE' in body or "entry.target === FILE" in body, \
        "only this template's own target"
    # never over a live turn — resumeRun would fight pollLoop for the frame
    assert "if (activeRun || sending) return;" in body
    # and the first pass is silent, or every already-recorded run would re-render
    assert "scheduleBaselined" in body
    assert "setInterval(pollScheduledRuns" in code


def test_a_scheduled_turn_that_FINISHED_between_polls_is_appended(code):
    """The first cut of this only worked while the run was still live, which made
    the common case — a short turn finishing inside the 15s window — still
    invisible, with the composer's note promising otherwise.

    `resumeRun`'s done path repairs only what it can PROVE is missing (an empty log,
    or a last user bubble that is this run's message) because on the reload path the
    restored transcript may already hold the turn. A scheduled send is the opposite:
    it fired after this frame rendered, so the turn cannot be on screen and the
    caller knows it. Hence an explicit opt-in rather than loosening the default."""
    assert "neverShown" in code
    assert "{ neverShown: true }" in code
    done = code[code.index("if (probe.done) {"):]
    done = done[:done.index("scrollBottom();")]
    assert "(!users.length || neverShown) && probeMsg" in done, \
        "a finished run must APPEND when the caller says the turn was never shown"
    # a failed turn needs its own user line too, or the error reads as belonging to
    # whatever the reader last said
    assert "if (probeMsg) addUser(probeMsg);" in done

    # and the reload caller keeps the conservative default — it never opts into
    # neverShown. Pinned as "this call does not ask for it" rather than the old
    # "passing no opts at all" (`await resumeRun(run_id);`): the boot legitimately
    # grew an unrelated `{ retryUnknown: true }`, and an exact-argument anchor made
    # every future opt look like this test's regression.
    assert "await resumeRun(run_id" in code, "the boot never re-attaches to the run"
    reattach = code[code.index("await resumeRun(run_id"):]
    reattach = reattach[:reattach.index(";")]
    assert "neverShown" not in reattach, \
        "the reload path must repair only what it can prove is missing"


def test_never_shown_kills_matching_rather_than_only_adding_a_branch(code):
    """The second bug in this area, and the reason the flag is not just an extra
    append branch: with `matches` still preferred, the SAME PROMPT SENT TWICE broke
    it. Say "run the tests" now, then schedule those same words for later — the
    earlier identical bubble matched, and the repair stripped everything after it,
    DELETING that turn's real reply to hang the scheduled answer there.

    The same coincidence hits the live path, which strips partial rows on a match,
    so the flag has to suppress matching for both — which it does by being folded
    into `matches` itself, in one place, rather than checked per branch."""
    fn = code[code.index("async function resumeRun("):]
    fn = fn[:fn.index("\nfunction submitChat()")]
    assert "const matches = !neverShown &&" in fn, \
        "matching must be off entirely when the turn was never on screen"
    # every destructive strip is reached only through `matches`, so gating it there
    # covers the live path as well as the done path
    lines = fn.split("\n")
    strips = [i for i, l in enumerate(lines) if "lastTurn.nextElementSibling" in l]
    assert strips, "the strip sites moved — re-check what guards them"
    for i in strips:
        guard = next(lines[j] for j in range(i, i - 6, -1) if "if (" in lines[j])
        assert "if (matches)" in guard, f"unguarded strip near: {lines[i].strip()}"


def test_the_baseline_is_taken_at_load_not_one_interval_later(code):
    """Baselining on the first INTERVAL wrote off anything firing in the opening 15s
    as predating a frame it had fired inside — and that window is exactly when a
    reader opens the chat, because the note tells them to leave it open."""
    assert "\npollScheduledRuns();\nsetInterval(pollScheduledRuns" in code


def test_a_run_is_only_written_off_once_it_is_really_handled(code):
    """Marking an id handled before the attach could succeed lost the turn
    entirely: `resumeRun` returns immediately if `sending` went true meanwhile, and
    the id was already written off. So the live-turn guard sits adjacent to the call
    with nothing awaited between, and a run that cannot be taken now is left
    unmarked for the next tick."""
    body = code[code.index("async function pollScheduledRuns("):]
    body = body[:body.index("\npollScheduledRuns();")]
    guard = body.index("if (activeRun || sending) return;")
    mark = body.index("SCHEDULE_ATTACHED.add(entry.run_id);", guard)
    call = body.index("await resumeRun(", guard)
    assert guard < mark < call, "guard, then mark, then call — in that order"
    assert "await" not in body[guard:call].replace("await resumeRun(", ""), \
        "nothing may be awaited between the guard and the call"


def test_an_attached_run_goes_on_the_url(code):
    """`sendMessage` puts the run on the URL so a reload re-attaches. Without the
    same here, a reload or a mode switch dropped the stream — and the next frame's
    baseline then wrote the run off as predating it."""
    body = code[code.index("async function pollScheduledRuns("):]
    body = body[:body.index("\npollScheduledRuns();")]
    assert 'fused.params.set("run", entry.run_id' in body


def test_replacing_the_transcript_re_baselines_the_watcher(code):
    """`neverShown` forces matching off for every scheduled attach, which is only
    sound while "this run fired after the visible transcript was rendered" holds.
    A session SWITCH breaks that: `loadHistory` restores the new session's history,
    which already contains any scheduled turn that ran in it, and attaching then
    appended a second copy of the same prompt and reply.

    So the reset lives where the transcript is thrown away, beside the other
    per-transcript clears — and it is what makes `neverShown` true by construction
    for the attaches that survive."""
    assert "function scheduleResetForNewTranscript(" in code
    body = code[code.index("function scheduleResetForNewTranscript("):]
    body = body[:body.index("\n}")]
    assert "SCHEDULE_ATTACHED.clear()" in body
    assert "SCHEDULE_NOTED.clear()" in body
    assert "scheduleBaselined = false" in body, \
        "the next poll must re-baseline, or already-fired runs attach again"

    # called from loadHistory, BEFORE the visible conversation is replaced
    lh = code[code.index("async function loadHistory("):]
    lh = lh[:lh.index("\n}")]
    assert "scheduleResetForNewTranscript();" in lh
    assert lh.index("scheduleResetForNewTranscript();") < lh.index("renderLogSkeleton()")


def test_another_sessions_run_is_noted_without_being_rendered(code):
    """Two sets answer two different questions: ATTACHED stops a run being rendered
    twice, NOTED stops the same "ran in another session" line repeating every 15s.

    NOTED is deliberately NOT a claim that the run can be adopted here later — that
    was the original reasoning and it was wrong. Switching to that session calls
    `loadHistory`, which restores the turn from history and re-baselines the watcher
    (see scheduleResetForNewTranscript); attaching on top of that appended a second
    copy."""
    assert "SCHEDULE_ATTACHED" in code and "SCHEDULE_NOTED" in code
    body = code[code.index("async function pollScheduledRuns("):]
    body = body[:body.index("\npollScheduledRuns();")]
    foreign = body[body.index("if (!scheduledRunIsOurs(entry)) {"):]
    foreign = foreign[:foreign.index("continue;")]
    assert "SCHEDULE_NOTED.add" in foreign
    assert "SCHEDULE_ATTACHED.add" not in foreign


def test_it_only_attaches_a_run_that_belongs_on_this_screen(code):
    """Splicing another conversation's turn into this transcript would be the page
    telling a lie about what was said where — worse than not attaching at all."""
    assert "function scheduledRunIsOurs(" in code
    body = code[code.index("function scheduledRunIsOurs("):]
    body = body[:body.index("\n}")]
    # a session on screen: only that same conversation, by either id
    assert "entry.session_id === mine" in body
    assert "ran === mine" in body
    # no session on screen: only a send that resumed nothing, so there is a fresh
    # session for this frame to adopt
    assert "return !entry.session_id;" in body


def test_the_reader_is_told_before_the_turn_starts_writing(code):
    """The one line this page owes someone who left a chat open: text appearing on
    its own, in a conversation they are not currently having, is otherwise just
    strange. The note and the attach are the same event and must not drift apart —
    it is written immediately before the run is adopted."""
    body = code[code.index("async function pollScheduledRuns("):]
    body = body[:body.index("\npollScheduledRuns();")]
    note = 'addNote("Your scheduled message is running now.", null, "◷");'
    assert note in body
    assert body.index(note) < body.index("await resumeRun(")


def test_the_page_links_a_ran_message_to_its_session():
    """This page can say a message ran; only the transcript knows what it DID. So a
    row whose watcher captured a session id hands the reader straight to that
    conversation — and a row without one simply offers no link rather than pointing
    at nothing.

    The invariant is about the LINK, not the file, so it has followed the rows
    twice: out of Scheduled.tsx into ScheduleTaskViews.tsx (the flow-app tree +
    kanban port, 2026-08-16), and then into tasks-lib.ts when the page moved to
    the task/thread/message model (2026-08-17). `taskHref` is where the gate
    lives now: a task carries `session_id` (empty until its first run), and the
    href is null rather than a URL while it is empty.

    A message adds a second half — the anchor that scrolls the transcript to
    that turn — and it inherits the same gate by construction, because it is
    built from `taskHref` and returns null whenever that does."""
    with open(os.path.join("frontend", "src", "shell", "tasks-lib.ts"),
              encoding="utf-8") as f:
        lib = f.read()
    assert "explorerUrl(" in lib
    assert "task.session_id" in lib, "the link must be gated on having an id"
    assert "return null" in lib, "no id must mean no link, not a link to nothing"
    # The message link is the task link plus an anchor, so it cannot acquire a
    # session id the task does not have.
    assert "const base = taskHref(task)" in lib
    assert "if (!base) return null" in lib

    with open(os.path.join("frontend", "src", "shell", "ScheduleTaskViews.tsx"),
              encoding="utf-8") as f:
        page = f.read()
    # the shell's own navigation, not a full page load
    assert "navigateUrl(" in page
