"""fused_render.cron — the 5-field parser and its next-occurrence walk.

A table of (expression, after, expected-next) pairs pins the semantics: the
presets the UI offers, the grammar (`*`, lists, ranges, steps), the two Sunday
spellings, and the standard dom-OR-dow rule. All datetimes here are naive
local, which is the contract next_after states.
"""
from datetime import datetime

import pytest

from fused_render import cron


T = datetime  # brevity in the table


@pytest.mark.parametrize("expr,after,expected", [
    # The presets the UI offers.
    ("0 * * * *",  T(2026, 8, 14, 12, 34), T(2026, 8, 14, 13, 0)),   # hourly
    ("30 9 * * *", T(2026, 8, 14, 12, 0),  T(2026, 8, 15, 9, 30)),   # daily 9:30, already past today
    ("30 9 * * *", T(2026, 8, 14, 8, 0),   T(2026, 8, 14, 9, 30)),   # daily 9:30, still ahead today
    ("0 9 * * 1",  T(2026, 8, 14, 10, 0),  T(2026, 8, 17, 9, 0)),    # weekly Monday (Aug 14 2026 = Friday)
    # Exactly at a match is NOT a match — strictly after.
    ("0 13 * * *", T(2026, 8, 14, 13, 0),  T(2026, 8, 15, 13, 0)),
    # Steps, lists, ranges.
    ("*/15 * * * *",    T(2026, 8, 14, 12, 50), T(2026, 8, 14, 13, 0)),
    ("0 9,17 * * *",    T(2026, 8, 14, 10, 0),  T(2026, 8, 14, 17, 0)),
    ("0 9 * * 1-5",     T(2026, 8, 14, 10, 0),  T(2026, 8, 17, 9, 0)),  # Fri 10am -> Mon (weekend skipped)
    ("0 0 1 * *",       T(2026, 8, 14, 0, 0),   T(2026, 9, 1, 0, 0)),   # monthly, 1st
    ("30 6 * 2 *",      T(2026, 8, 14, 0, 0),   T(2027, 2, 1, 6, 30)),  # month-restricted crosses the year
    # Sunday both ways.
    ("0 8 * * 0", T(2026, 8, 14, 0, 0), T(2026, 8, 16, 8, 0)),
    ("0 8 * * 7", T(2026, 8, 14, 0, 0), T(2026, 8, 16, 8, 0)),
    # dom OR dow when both are restricted: the 13th (Thu) fires before Friday the 14th... i.e.
    # after Wed Aug 12, `0 0 13 * 5` matches Thu the 13th (dom) not just Fri (dow).
    ("0 0 13 * 5", T(2026, 8, 12, 0, 0), T(2026, 8, 13, 0, 0)),
    ("0 0 13 * 5", T(2026, 8, 13, 0, 0), T(2026, 8, 14, 0, 0)),
    # Feb 29 exists only some years; the walk has to reach it.
    ("0 0 29 2 *", T(2026, 8, 14, 0, 0), T(2028, 2, 29, 0, 0)),
])
def test_next_after(expr, after, expected):
    assert cron.parse(expr).next_after(after) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "0 9 * *", "0 9 * * * *",        # wrong arity
    "60 * * * *", "* 24 * * *", "* * 0 * *",    # out of bounds
    "* * * 13 *", "* * * * 8",
    "a * * * *", "1-b * * * *", "*/0 * * * *",  # unreadable pieces
    "5-1 * * * *",                              # inverted range
])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        cron.parse(bad)


def test_never_matching_expression_raises():
    with pytest.raises(ValueError, match="no occurrence"):
        cron.parse("0 0 31 2 *").next_after(T(2026, 1, 1, 0, 0))


def test_next_after_refuses_aware_datetimes():
    from datetime import timezone
    with pytest.raises(ValueError, match="naive"):
        cron.parse("0 * * * *").next_after(datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_spelt_out_ranges_behave_like_star():
    # `1-31` / `0-7` carry the same "left open" meaning as `*` for the
    # dom-OR-dow rule: with dom spelt out and dow restricted, dow wins alone.
    c = cron.parse("0 0 1-31 * 5")
    assert c.next_after(T(2026, 8, 12, 0, 0)) == T(2026, 8, 14, 0, 0)  # Friday, not the 13th


@pytest.mark.parametrize("expr,after,expected", [
    # `N/step` runs from N to the field's end — crontab's reading, not "just N"
    # (Bugbot, PR #529: `0/15` fired hourly instead of every 15 minutes).
    ("0/15 * * * *", T(2026, 8, 14, 12, 1),  T(2026, 8, 14, 12, 15)),
    ("0/15 * * * *", T(2026, 8, 14, 12, 46), T(2026, 8, 14, 13, 0)),
    ("5/20 * * * *", T(2026, 8, 14, 12, 6),  T(2026, 8, 14, 12, 25)),
    ("0 9/6 * * *",  T(2026, 8, 14, 10, 0),  T(2026, 8, 14, 15, 0)),
])
def test_bare_value_with_step_runs_to_the_fields_end(expr, after, expected):
    assert cron.parse(expr).next_after(after) == expected
