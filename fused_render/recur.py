"""A structured repeat rule — the calendar's vocabulary, not crontab's.

`cron.py` answers the same question this module answers ("when does this run
next?") and stays exactly where it is, for existing entries and for anyone who
brings a crontab line with them. This module exists because the *form* speaks a
different language. "Monthly on the second Wednesday", "every 2 weeks on Mon and
Wed", "ends after 13 occurrences", "ends on Nov 11, 2026" — a 5-field cron line
can express none of those:

* **nth-weekday** would need `dom` and `dow` to AND, and crontab's rule is that
  a restricted pair ORs (see `cron.Cron._day_matches`). `0 9 8-14 * 3` is the
  usual dodge, and it fires on every day of that window as well as every
  Wednesday of the month.
* **intervals** have no anchor to count from. `*/2` on a field steps within the
  field's own range and resets — `0 9 */2 * *` is "the 1st, 3rd, 5th … of every
  month", which is not "every 2 days", and there is no `*/2` for weeks at all.
* **ends** are simply not in the grammar. Cron is a standing rule; it has no way
  to say "thirteen times and stop".

So a rule here is not a grid to be matched but a SERIES to be counted, and that
is the one structural difference to keep in mind while reading: every answer is
"the anchor, plus k steps", where the anchor is the first scheduled run (the
date and time the user picked in the form) and the series INCLUDES it. Counting
from an anchor is what makes intervals and ends mean anything, and it is why
`schedule.py` stores the anchor beside the rule rather than trusting `due`,
which it rewrites to mirror the next occurrence.

All arithmetic is in NAIVE LOCAL time, the same posture (and the same promise)
as `cron.py`: "the second Wednesday at 9am" is about the reader's wall clock,
across DST changes too. Callers attach the zone at the edge.

**Skipped, not clamped.** A month that has no 31st, or no fifth Friday, and a
year that has no Feb 29, produce NO run rather than a nearby one. That is RFC
5545's posture and it is the honest one: a user who picked the 31st asked for
the 31st, and firing on the 30th instead is this module inventing a date they
did not choose. February would otherwise turn "monthly on the 31st" into
"monthly on the 28th, and also the 31st", which is a schedule nobody wrote.

`count` is deliberately NOT enforced here. This module answers "when next", and
"how many have there been" is a fact about the store, not about the calendar —
`schedule.py` keeps it on the template as `made` and stops asking. `until` IS
enforced here, because it is a fact about the calendar alone.

Dependency-free, and imported by `schedule.py` only; keep it that way (nothing
here may import anything under `fused_render.server`).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

# The vocabulary. Deliberately five words and not RFC 5545's — this is what the
# form offers, and a rule the form cannot produce is a rule nobody can edit.
# Ordered shortest-to-longest, which is also the order the repeat menu lists
# them in and the order the "expected one of …" sentence below reads out.
FREQUENCIES = ("hour", "day", "week", "month", "year")
# What "monthly" can mean, and the reason this module exists: "day" is the
# anchor's day-of-month, "nth-weekday" is "the second Wednesday".
MONTHLY_MODES = ("day", "nth-weekday")
FIELDS = ("freq", "interval", "byday", "monthly", "until", "count")

# Bounds on the two numbers a user types. Both are "no sane form offers more"
# rather than anything the arithmetic needs: an interval of 99 months is already
# eight years between runs, and 999 runs of anything is a decade of daily.
MAX_INTERVAL = 99
MAX_COUNT = 999

# How many interval-steps the search may find EMPTY before it calls a rule
# unsatisfiable. Only skipping consumes this budget (a 31st, a fifth Friday, a
# Feb 29), and a step that produces an occurrence resets it, so a long
# projection never runs out. 500 is far past the worst real pattern — the
# day-existence cycle is 12 months, or 4 years once leap days are in it — and
# exists so a hand-edited store cannot spin a tick thread forever.
_MAX_EMPTY_STEPS = 500


# ------------------------------------------------------------------ weekdays
#
# 0=Sunday through 6=Saturday, throughout: the numbering the calendar UI uses
# and the one `cron.py` already normalises to. Python's own `weekday()` is
# Monday=0, so every crossing goes through these two helpers rather than an
# inline `+ 1 % 7` that would eventually be written the other way round.


def weekday(day: date) -> int:
    """`day`'s weekday as 0=Sunday … 6=Saturday."""
    return (day.weekday() + 1) % 7


def week_start(day: date) -> date:
    """The Sunday on or before `day` — the block a weekly rule counts in."""
    return day - timedelta(days=weekday(day))


# ---------------------------------------------------------------- validation


def _whole(value, name: str, lo: int, hi: int) -> int:
    """One integer field, bounded. `bool` is rejected explicitly because it is
    an `int` in Python and `{"interval": true}` is a client bug, not "1"."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: expected a whole number between {lo} and {hi}")
    if not lo <= value <= hi:
        raise ValueError(
            f"{name}: expected a whole number between {lo} and {hi}, got {value}")
    return value


def _until_date(value) -> str:
    """`until` as a normalised YYYY-MM-DD string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("until: expected an end date like 2026-11-11")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"until: expected an end date like 2026-11-11, got {value!r}") from None
    return parsed.isoformat()


def validate_rule(rule: dict) -> dict:
    """Normalise one rule, or raise ValueError saying what is wrong with it.

    The messages are written for a PERSON, not a log: they surface verbatim in
    the 400 the router returns and end up in front of whoever filled the form
    in. That is also why unknown fields are refused rather than ignored — a
    typo'd `untill` that is silently dropped becomes a repeat that never ends,
    discovered weeks later.

    Returns a fresh dict carrying only what the rule actually says, with the
    defaults that are facts about the rule filled in (`interval`, and
    `monthly` for a monthly one). `byday`'s default is NOT filled in here: it
    is the anchor's own weekday, and the anchor is not this function's business
    — `next_occurrence` resolves it, so a rule is storable before anyone has
    decided which day the series starts on.
    """
    if not isinstance(rule, dict):
        raise ValueError("rule: expected an object describing the repeat")

    unknown = sorted(str(key) for key in rule if key not in FIELDS)
    if unknown:
        raise ValueError(
            "rule: don't know what to do with "
            + ", ".join(repr(key) for key in unknown)
            + " — expected " + ", ".join(FIELDS))

    freq = rule.get("freq")
    if freq is None:
        raise ValueError("freq: required — one of " + ", ".join(FREQUENCIES))
    if freq not in FREQUENCIES:
        raise ValueError(
            f"freq: expected one of {', '.join(FREQUENCIES)}, got {freq!r}")

    out: dict = {"freq": freq}
    interval = rule.get("interval")
    out["interval"] = 1 if interval is None else _whole(
        interval, "interval", 1, MAX_INTERVAL)

    byday = rule.get("byday")
    if byday is not None:
        if freq != "week":
            raise ValueError(
                "byday: only a weekly repeat runs on chosen weekdays — "
                "drop it, or set freq to 'week'")
        # `str` is a sequence, and `"12"` would otherwise validate as two days.
        if isinstance(byday, (str, bytes)) or not isinstance(byday, (list, tuple)):
            raise ValueError(
                "byday: expected a list of weekdays, 0 (Sunday) through 6 (Saturday)")
        if not byday:
            raise ValueError(
                "byday: cannot be empty — leave it out to repeat on the "
                "start day's own weekday")
        days = set()
        for value in byday:
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 0 <= value <= 6:
                raise ValueError(
                    f"byday: {value!r} is not a weekday — use 0 (Sunday) "
                    "through 6 (Saturday)")
            days.add(value)
        # Deduped and sorted, so the stored rule has one spelling and the walk
        # can yield a week's runs in order without re-sorting per week.
        out["byday"] = sorted(days)

    monthly = rule.get("monthly")
    if freq == "month":
        monthly = "day" if monthly is None else monthly
        if monthly not in MONTHLY_MODES:
            raise ValueError(
                "monthly: expected 'day' (the same day each month) or "
                "'nth-weekday' (e.g. the second Wednesday)")
        out["monthly"] = monthly
    elif monthly is not None:
        raise ValueError(
            "monthly: only a monthly repeat has a monthly mode — "
            "drop it, or set freq to 'month'")

    until = rule.get("until")
    count = rule.get("count")
    if until is not None and count is not None:
        raise ValueError(
            "until, count: pick one — a repeat ends either on a date or "
            "after a number of runs, not both")
    if until is not None:
        out["until"] = _until_date(until)
    if count is not None:
        out["count"] = _whole(count, "count", 1, MAX_COUNT)
    return out


# ------------------------------------------------------------------ the walk
#
# One generator per frequency, each yielding the series in order from a jumped-to
# starting point. They JUMP rather than step because `after` can be years past
# the anchor — a projection over a long horizon, or a template on a machine that
# was off for a month — and stepping a day at a time to get there would put a
# visible walk inside a request. The jump lands on or before the first candidate
# and the loop confirms; it is never trusted to land exactly.


def _month_of(year: int, month: int, offset: int) -> tuple[int, int]:
    """(year, month) `offset` months from (year, month). Months are counted as
    a single number so the arithmetic cannot produce a month 13."""
    total = year * 12 + (month - 1) + offset
    return total // 12, total % 12 + 1


def _nth_weekday_day(year: int, month: int, want: int, nth: int) -> int:
    """The day-of-month of the `nth` `want`-day in this month, or 0 when the
    month has none — only seven months in twelve have a fifth Friday, and a
    month that does not have one is SKIPPED (see the module docstring)."""
    first = date(year, month, 1)
    day = 1 + (want - weekday(first)) % 7 + (nth - 1) * 7
    return day if day <= calendar.monthrange(year, month)[1] else 0


def _walk_hour(anchor: datetime, interval: int, after: datetime):
    """The only frequency that fires more than once a day, and the only one
    whose step is smaller than the unit `until` is measured in — which is why
    `_walk`'s date comparison is spelled out there rather than here: "ends on
    Nov 11" lets every one of the 11th's runs happen, and stops."""
    step = timedelta(hours=interval)
    when = anchor
    if when <= after:
        when = anchor + step * ((after - anchor) // step)
        while when <= after:
            when += step
    while True:
        yield when
        when += step


def _walk_day(anchor: datetime, interval: int, after: datetime):
    step = timedelta(days=interval)
    when = anchor
    if when <= after:
        when = anchor + step * ((after - anchor) // step)
        while when <= after:
            when += step
    while True:
        yield when
        when += step


def _walk_week(anchor: datetime, interval: int, byday, after: datetime):
    """Sunday-anchored blocks, counted from the anchor's own week.

    The block matters: "every 2 weeks on Mon and Wed" has to mean the same two
    days of the same weeks no matter which of them the user picked as the start,
    so the unit that repeats is the WEEK the anchor falls in, not "14 days from
    each run". Counting from each run instead would let a Wed anchor and a Mon
    anchor of the same week drift onto opposite fortnights.
    """
    # No `byday` means the anchor's own weekday — resolved here rather than in
    # `validate_rule`, which never sees an anchor.
    days = list(byday) if byday else [weekday(anchor.date())]
    origin = week_start(anchor.date())
    block = 0
    if after.date() >= origin:
        # The last COUNTING week at or before `after`; its own days may all be
        # behind us, in which case the loop simply moves on to the next.
        block = ((after.date() - origin).days // 7 // interval) * interval
    at = anchor.time()
    while True:
        start = origin + timedelta(days=block * 7)
        for day in days:
            when = datetime.combine(start + timedelta(days=day), at)
            # `>= anchor` is what makes the anchor's own week a partial one: a
            # Wednesday anchor with byday Mon+Wed does not run on the Monday
            # that has already gone by, but does run every Monday after.
            if when >= anchor and when > after:
                yield when
        block += interval


def _walk_month(anchor: datetime, interval: int, mode: str, after: datetime):
    at = anchor.time()
    # "The second Wednesday" is read OFF the anchor rather than stated in the
    # rule: the user picked a date, and which Wednesday of the month it is
    # cannot then disagree with itself. 1..5, and 5 is the one that skips months.
    nth = (anchor.day - 1) // 7 + 1
    want = weekday(anchor.date())
    offset = 0
    if (after.year, after.month) >= (anchor.year, anchor.month):
        months = (after.year - anchor.year) * 12 + (after.month - anchor.month)
        offset = (months // interval) * interval
    empty = 0
    while empty < _MAX_EMPTY_STEPS:
        year, month = _month_of(anchor.year, anchor.month, offset)
        offset += interval
        if mode == "day":
            day = anchor.day if anchor.day <= calendar.monthrange(year, month)[1] else 0
        else:
            day = _nth_weekday_day(year, month, want, nth)
        if not day:
            empty += 1  # no 31st / no fifth Friday: skipped, never clamped
            continue
        when = datetime.combine(date(year, month, day), at)
        if when < anchor or when <= after:
            continue  # the jump landed a step short; not an empty month
        empty = 0
        yield when


def _walk_year(anchor: datetime, interval: int, after: datetime):
    at = anchor.time()
    offset = 0
    if after.year >= anchor.year:
        offset = ((after.year - anchor.year) // interval) * interval
    empty = 0
    while empty < _MAX_EMPTY_STEPS:
        year = anchor.year + offset
        offset += interval
        if anchor.month == 2 and anchor.day == 29 and not calendar.isleap(year):
            empty += 1  # a Feb 29 rule genuinely has nothing to say in 2027
            continue
        when = datetime.combine(date(year, anchor.month, anchor.day), at)
        if when < anchor or when <= after:
            continue
        empty = 0
        yield when


def _walk(rule: dict, anchor: datetime, after: datetime):
    """Every occurrence strictly after `after`, in order, stopping at `until`.

    Validates rather than trusting: the rule may have come off disk, and a
    hand-edited store must fail with the same sentence a bad form does.
    """
    if anchor.tzinfo is not None or after.tzinfo is not None:
        raise ValueError("recur: wants naive local datetimes")
    rule = validate_rule(rule)
    freq = rule["freq"]
    interval = rule["interval"]
    until = date.fromisoformat(rule["until"]) if "until" in rule else None

    if freq == "hour":
        walk = _walk_hour(anchor, interval, after)
    elif freq == "day":
        walk = _walk_day(anchor, interval, after)
    elif freq == "week":
        walk = _walk_week(anchor, interval, rule.get("byday"), after)
    elif freq == "month":
        walk = _walk_month(anchor, interval, rule["monthly"], after)
    else:
        walk = _walk_year(anchor, interval, after)

    try:
        for when in walk:
            # INCLUSIVE: a run falling ON the end date still happens. "Ends on
            # Nov 11" reads as "the 11th is the last day it can run", and the
            # comparison is on the DATE so the time of day cannot decide it —
            # which is what makes an HOURLY rule end where a person expects,
            # with all twenty-four of the last day's runs and none the day after.
            if until is not None and when.date() > until:
                return
            yield when
    except (OverflowError, ValueError):
        # Off the end of the representable calendar (year 10000). A series with
        # no stated end still has a last computable instant, and running past it
        # is exhaustion — the same answer as `until`, not an error to raise into
        # a listing.
        return


def next_occurrence(rule: dict, anchor: datetime, after: datetime) -> datetime | None:
    """The first run strictly after `after`, or None once the rule is spent.

    Naive local datetimes in, naive local datetime out. `anchor` is the first
    scheduled run and the series includes it, so `next_occurrence(rule, a, x)`
    for any `x` before `a` returns `a` itself (weekly aside, where it returns
    the first chosen weekday at or after the anchor).

    None means "no more" — past `until`, or off the end of the calendar. It does
    NOT mean "not yet": `count` is the store's to enforce, because how many runs
    have happened is not something the calendar knows.
    """
    for when in _walk(rule, anchor, after):
        return when
    return None


def occurrences(rule: dict, anchor: datetime, after: datetime | None = None,
                limit: int = 500) -> list[datetime]:
    """Up to `limit` runs after `after`, in order — the projection helper.

    `after=None` means "from the start of the series", anchor included, which is
    how a `count`-limited rule gets numbered: the only exact way to know whether
    a given run is the 13th is to count from the first one.
    """
    if after is None:
        # A hair before the anchor, since the walk is strictly-after. Naive
        # datetimes carry microseconds, so this cannot skip a real instant.
        after = anchor - timedelta(microseconds=1)
    out: list[datetime] = []
    for when in _walk(rule, anchor, after):
        out.append(when)
        if len(out) >= limit:
            break
    return out
