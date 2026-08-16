"""A 5-field cron expression, and nothing more.

`minute hour day-of-month month day-of-week` — the classic crontab line, with
the classic vocabulary: `*`, plain numbers, `a-b` ranges, `a,b,c` lists, and
`/n` steps on any of those. Day-of-week runs 0–7 with both 0 and 7 meaning
Sunday. When day-of-month AND day-of-week are both restricted, a day matching
EITHER fires — the standard (if surprising) crontab rule, kept because people
bring their crontab lines with them.

Hand-rolled rather than a dependency (croniter et al.) because the schedule
needs exactly one question answered — "when is the next occurrence after t?" —
and a parser for one grammar plus a day-walk is small enough to own outright,
with a test table pinning the semantics.

All arithmetic is in NAIVE LOCAL time: "daily at 9am" is a promise about the
reader's wall clock, across DST changes too. Callers attach the zone at the
edge (schedule.py stores UTC instants, and converts on the way in and out).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# min/max per field, in crontab order.
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

# How far the day-walk looks before declaring the expression unsatisfiable.
# Four years covers the worst legitimate case (Feb 29) with room to spare;
# anything not matched by then (e.g. `0 0 31 2 *`) never fires.
_HORIZON_DAYS = 366 * 4 + 1


def _parse_field(text: str, lo: int, hi: int, name: str) -> frozenset[int]:
    """One field to the set of values it allows. ValueError on anything else."""
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        step = 1
        stepped = "/" in part
        if stepped:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"{name}: bad step in {text!r}")
            step = int(step_text)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"{name}: bad range in {text!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = int(part)
            # A bare value under a step is a starting point, not a singleton:
            # crontab reads `0/15` as "from 0 to the field's end, every 15" —
            # the first cut kept only the 0, so `0/15 * * * *` fired hourly
            # (Bugbot, PR #529).
            end = hi if stepped else start
        else:
            raise ValueError(f"{name}: cannot read {text!r}")
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"{name}: {text!r} outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


class Cron:
    """A parsed expression. Use `parse()`, not the constructor."""

    def __init__(self, expr: str, fields: tuple[frozenset[int], ...],
                 dom_any: bool, dow_any: bool) -> None:
        self.expr = expr
        self.minutes, self.hours, self.dom, self.months, dow = fields
        # 7 is an alias for Sunday; normalise so matching only ever sees 0-6.
        self.dow = frozenset(v % 7 for v in dow)
        # The dom-or-dow rule needs to know which day fields were left open.
        # "Allows every value it could" is the test used — `1-31` spelt out
        # behaves like `*`, which is also how real crontabs treat it.
        self.dom_any = dom_any
        self.dow_any = dow_any

    def _day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_dom = day.day in self.dom
        # Python: Monday=0 ... Sunday=6. Cron: Sunday=0 ... Saturday=6.
        in_dow = (day.weekday() + 1) % 7 in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return in_dow
        if self.dow_any:
            return in_dom
        return in_dom or in_dow

    def next_after(self, after: datetime) -> datetime:
        """The first matching minute STRICTLY after `after` (naive local).

        Walks days (cheap: month/dom/dow are per-day facts), then picks the
        first allowed hour:minute. Raises ValueError past the horizon."""
        if after.tzinfo is not None:
            raise ValueError("next_after wants a naive local datetime")
        start = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        day = start.date()
        for _ in range(_HORIZON_DAYS):
            if self._day_matches(day):
                floor = start if day == start.date() else None
                for hour in hours:
                    if floor and hour < floor.hour:
                        continue
                    for minute in minutes:
                        if floor and hour == floor.hour and minute < floor.minute:
                            continue
                        return datetime(day.year, day.month, day.day, hour, minute)
            day += timedelta(days=1)
        raise ValueError(f"cron {self.expr!r}: no occurrence within 4 years")


def parse(expr: str) -> Cron:
    """`Cron` for a 5-field expression; ValueError (with the field named) for
    anything the grammar above does not cover."""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("cron: expected 'minute hour day month weekday'")
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron: expected 5 fields (minute hour day month weekday), got {len(parts)}")
    fields = tuple(
        _parse_field(part, lo, hi, name)
        for part, (lo, hi), name in zip(parts, _BOUNDS, _NAMES)
    )
    dom_any = fields[2] == frozenset(range(1, 32))
    dow_any = fields[4] in (frozenset(range(0, 8)), frozenset(range(0, 7)))
    return Cron(expr.strip(), fields, dom_any, dow_any)
