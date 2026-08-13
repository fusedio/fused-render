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


def test_it_sends_the_target_it_is_bound_to_and_the_composers_approval_mode(code):
    """No path input anywhere: the target is FILE, the param this template is
    opened with. That is the whole reason scheduling moved here."""
    body = code[code.index('"/api/schedule""'.rstrip('"')):]
    body = body[:2000]
    assert "target: FILE" in body
    assert "permission_mode" in body
    # the approvals pill applies to the scheduled turn too — same question, and
    # the case where the answer matters most
    assert "perm-sel" in body or "perm ?" in body


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
