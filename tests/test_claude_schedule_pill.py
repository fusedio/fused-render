"""Scheduling from the claude template's composer (the "Send now" pill).

Scheduling lives HERE and not on a settings page, because this row already knows
the folder (the template is bound to one target) and already holds the message —
so the only thing it was missing is *when*. These are structural assertions over
the template source, the same approach test_claude_kind.py takes: the pill is
inline vanilla JS in a 9000-line document, so what can be pinned is that the
wiring exists, that it reaches the right endpoint with the right guard, and that
the two properties it would be easy to get wrong stay true.
"""
import os
import re

import pytest

from fused_render import schedule

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


def test_both_composers_carry_the_when_pill(code):
    """The chat composer AND the home one: a first message is exactly as
    schedulable as a follow-up, and a pill on only one of them would make that
    depend on whether a session happened to exist yet."""
    assert code.count('class="pill when-sel"') == 2
    assert 'id="when"' in code
    assert 'id="hwhen"' in code


def test_the_pill_posts_to_the_schedule_api_with_the_write_guard(code):
    assert '"/api/schedule"' in code
    # D3: without the header the endpoint answers 403, and the template is the
    # one caller here that does not go through platform/lib/api.ts's helper.
    assert '"X-Fused": "1"' in code


def _schedule_message_fn(code: str) -> str:
    """The body of `scheduleMessage`. Scoped to the function rather than to a
    character window after the fetch URL — the earlier version of this helper broke
    the moment a line moved above the request, which is not a fact worth failing on."""
    body = code[code.index("async function scheduleMessage("):]
    return body[:body.index("\n}")]


def test_it_sends_the_target_it_is_bound_to_and_the_composers_approval_mode(code):
    """No path input anywhere: the target is FILE, the param this template is
    opened with. That is the whole reason scheduling moved here."""
    body = _schedule_message_fn(code)
    assert "target: FILE" in body
    assert "permission_mode" in body
    # the approvals pill applies to the scheduled turn too — same question, and
    # the case where the answer matters most
    assert "perm-sel" in body


def test_the_due_time_is_sent_as_a_naive_local_stamp(code):
    """`schedule.parse_due` reads a naive timestamp as LOCAL time, so the pill
    must send local wall-clock and never a UTC conversion — otherwise a 9am
    choice fires at the wrong hour for everyone off UTC."""
    assert "localDueString" in code
    assert "due: localDueString(at)" in code
    # the giveaways of a timezone conversion on the way out
    assert "toISOString()" not in code.split("localDueString")[1][:600]


def test_the_deferral_does_not_outlive_its_own_message(code):
    """The model/effort/approval pills persist in `fused.params` because they
    describe how the chat behaves. "Send at 6pm" describes ONE message, and a
    choice that survived its send would silently defer whatever was typed next."""
    assert "function resetWhen()" in code
    # not persisted like its neighbours
    assert 'fused.params.set("when"' not in code
    assert 'fused.params.get("when")' not in code


def test_a_scheduled_message_requires_words(code):
    """An annotation on its own is sendable NOW — its meaning is the screen it was
    drawn on — but there is nothing to defer in it, and the crop would describe a
    pane that has since changed."""
    assert "A scheduled message needs some text" in code


def test_the_presets_are_resolved_at_send_time(code):
    """"Tomorrow 9am" means tomorrow from the moment the user commits, not from
    whenever the pill happened to be touched."""
    assert "WHEN_RESOLVERS" in code
    # each resolver builds its Date when called, so none of them may be a value
    resolvers = code[code.index("const WHEN_RESOLVERS"):]
    resolvers = resolvers[:resolvers.index("document.querySelectorAll(\".when-sel\")")]
    assert resolvers.count("() =>") >= 5


def test_the_permission_modes_offered_are_the_ones_the_scheduler_accepts(code):
    """The pill hands its value straight to `schedule.create`, which validates
    against its own tuple and raises on anything else — so a mode the composer can
    offer but the store refuses would be a 400 the user cannot explain."""
    listed = re.search(r"const PERMISSION_MODES = \[([^\]]*)\]", code)
    assert listed, "the template's PERMISSION_MODES list moved"
    offered = set(re.findall(r'"([a-zA-Z]+)"', listed.group(1)))
    assert offered <= set(schedule.PERMISSION_MODES), (
        f"composer offers {offered - set(schedule.PERMISSION_MODES)}, which "
        f"schedule.create refuses")


def test_deferring_is_decided_before_the_live_run_queue(code):
    """Scheduling is a store write, not a send, so it must not care whether a turn
    is running. When this check sat BELOW the `activeRun` branch, a message the user
    had deferred to tomorrow was parked by the queue and fired the moment the
    current turn ended — the one outcome the pill exists to prevent."""
    body = code[code.index("function submitChat()"):]
    body = body[:body.index("\n}")]
    assert body.index("whenChoice()") < body.index("if (activeRun)"), \
        "the When check must come first, above the queue"


def test_a_second_submit_cannot_store_a_second_copy(code):
    """The box is cleared only on SUCCESS (the draft must survive a refusal), so
    without a guard a second Enter during the round trip stores another unattended
    job. A flag of its own, not `sending` — a scheduled message starts no run."""
    assert "let scheduling = false;" in code
    assert code.count("if (scheduling) return;") == 2  # both composers
    assert code.count("scheduling = true;") == 2
    assert code.count("scheduling = false;") >= 3      # the declaration + both resets


def test_a_scheduled_follow_up_continues_the_chat_it_was_written_in(code):
    """Scheduling from an open chat is "and then do this next", so a fresh session
    would throw away the thread the prompt was written against — the context that
    made it worth deferring from HERE rather than from a page."""
    body = _schedule_message_fn(code)
    assert "session_id: session" in body
    assert 'fused.params.get("session_id")' in body


def test_a_refused_home_schedule_puts_the_draft_where_the_user_now_is(code):
    """The home path enters the chat before the POST resolves (the confirmation
    lands in the transcript, which home hides), so a refusal must not leave the
    text in the box that navigation just hid, where it reads as lost."""
    home = code[code.index('document.getElementById("card").onsubmit'):]
    home = home[:home.index("homebox.addEventListener")]
    assert "box.value = message;" in home
    assert "focusBox(box)" in home


def test_deferring_shows_the_approvals_mode_that_will_actually_run(code):
    """"ask every time" cannot work unattended — nobody polls `decide`, so the
    first tool call parks until the permission timeout denies it. A deferred send
    therefore runs under `auto`, and the pill SAYS so the moment a time is picked:
    this loosens approvals, and doing that behind the user's back would be worse
    than the parked turn it avoids."""
    assert "function scheduledPerm(" in code
    assert 'mode === "prompt" ? "auto" : mode' in code

    # ONE owner of pill state. The first cut had a second function running after
    # each syncSelects, and every desync came from a caller that ran one and not
    # the other — `resetWhen` leaving `auto` on screen after a successful schedule,
    # a model change writing the params value back over the substitution. So the
    # deferral is decided INSIDE syncSelects and there is nothing to forget.
    assert "function syncPermForWhen(" not in code
    sync = code[code.index("function syncSelects()"):]
    sync = sync[:sync.index("\n}")]
    assert "whenChoice()" in sync, "syncSelects must know whether a time is picked"
    assert "scheduledPerm(" in sync
    # a mode that cannot be honoured is not OFFERED — the pill informs, it does not
    # argue with a choice the user just made (which is what substituting on every
    # change did: picking "ask every time" snapped straight back to auto)
    assert "opt.disabled = deferred" in sync
    # never written to params: the CHAT's own mode is not what is being changed
    assert 'params.set("permission"' not in sync

    # and the wire carries the effective mode, not the one that was on screen before
    assert "permission_mode: mode," in _schedule_message_fn(code)


def test_resetting_the_time_hands_the_approvals_pill_back(code):
    """Setting the When value alone left the row reading `auto` with `prompt` still
    disabled after a successful schedule, while the next send-now would have used
    `prompt`. It goes through syncSelects, which owns both."""
    body = code[code.index("function resetWhen()"):]
    body = body[:body.index("\n}")]
    assert "syncSelects()" in body


def test_the_presets_include_one_short_enough_to_watch(code):
    """Five minutes is the preset that doubles as the way to TRY scheduling: the
    whole path plays out while the user is still looking at the screen."""
    assert '"5m": "In 5 minutes"' in code
    assert '"5m": () =>' in code


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

    # and the reload caller keeps the conservative default — passing no opts
    assert "await resumeRun(run_id);" in code


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


def test_the_confirmation_promises_only_what_the_page_now_delivers(code):
    """It says the turn will appear here, which is true only because
    pollScheduledRuns exists. The two have to move together."""
    assert "appears" in code and "here when it does" in code
    assert "function pollScheduledRuns(" in code


def test_the_page_links_a_ran_message_to_its_session_in_the_inbox():
    """This page can say a message ran; only the transcript knows what it DID. The
    Inbox addresses a session by the id the watcher captured (`?peek=<id>`), so a
    row that has one hands the reader straight to the conversation — and a row that
    does not simply offers no link rather than pointing at nothing."""
    with open(os.path.join("frontend", "src", "shell", "Scheduled.tsx"),
              encoding="utf-8") as f:
        page = f.read()
    assert "/sessions?peek=" in page
    assert "entry.claude_session_id &&" in page, "the link must be gated on having an id"
    # the shell's own navigation, not a full page load
    assert "navigateUrl(" in page
    assert "encodeURIComponent" in page


def test_the_settings_page_no_longer_asks_for_what_the_composer_knows():
    """The page keeps the LIST (nowhere else shows every folder's schedule at
    once) and loses the compose form, which asked the user to retype the folder
    and the message they had already typed in the chat."""
    with open(os.path.join("frontend", "src", "shell", "Scheduled.tsx"),
              encoding="utf-8") as f:
        page = f.read()
    assert "scheduleMessage" not in page      # the create call is gone
    assert "datetime-local" not in page       # and its When field with it
    assert "cancelScheduledMessage" in page   # cancelling stays
    assert "/api/schedule" not in page or "getSchedule" in page
    # and it points at where scheduling now happens
    assert "composer" in page
