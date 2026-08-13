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
    # the display follows the choice, in both directions
    assert "function syncPermForWhen(" in code
    assert code.count("syncPermForWhen()") >= 3  # when-change, perm-change, + itself
    # and the wire carries the effective mode, not the one that was on screen before
    assert "permission_mode: mode," in _schedule_message_fn(code)
    # …without writing it to params: the CHAT's own mode is not being changed
    perm_fn = code[code.index("function syncPermForWhen("):]
    perm_fn = perm_fn[:perm_fn.index("\n}")]
    assert "params.set" not in perm_fn


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
