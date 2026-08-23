"""Benchmark runs, kept forever at ~/.fused-render/ai_benchmarks.json (SPEC AI-14).

One more shell-state resource in the shape `shell/prefs.py` established: a
private `_path()` over `storage.home_dir()`, then `storage.read_json` /
`storage.write_json` and nothing else. It lives under `ai/` rather than
`shell/` only because its readers are the AI subsystem and its router — the
mechanism is identical.

**On disk, unlike `server/ai_metrics.py`, and that is the whole reason this
module exists.** That module is deliberately in-memory and forgotten on restart
(a passive counter of what happened to pass through), and it says so. A
benchmark is the opposite: minutes of compute somebody spent ON PURPOSE to
answer "is this model faster than that one, on this laptop, in this app
version". The answer is worthless the moment it is forgotten, so it is written
down.

What that buys has to be paid for somewhere, and it is paid in a **hard cap**
rather than in ai_metrics' fixed ring: runs are individually meaningful (you
cannot merge two of them into a bucket the way you can merge token counts), so
the bound is a count of whole records, `MAX_RUNS`, with the OLDEST dropped on
append. Dropping the newest would throw away the run whose button was just
pressed, which is the one thing a user is watching.

**The list is append-only and oldest-first.** Append order IS the chart's x
axis — the UI draws runs in the order they were taken — so nothing here sorts,
re-keys or dedupes. Two runs of the same model on the same workload are two
rows on purpose: that pair is the delta the page shows.

The store is **schema-agnostic about the METRICS** — it persists whatever
`benchmark.run()` built, so adding a metric never touches storage — but it is
not schema-agnostic about the three keys its readers dereference. `read()`
guarantees `id` (a string) and `metrics`/`workload` (objects), and drops a record
that lacks them.

That line is drawn here rather than in each reader because **this function is the
one door every consumer comes through**, and because the promise below is
otherwise not kept. The filter was `isinstance(run, dict)` alone, which let a
hand-edited record with no `metrics` reach the Benchmark tab — where
`lib/benchmark.ts` does `run.metrics[key]` and `run.workload.revision` and threw
a `TypeError` mid-render, taking the page down. A guard in the reader would have
fixed that one reader; a guard here fixes the next one too, which will not have
been told the rule.

A corrupt or absent file reads as **no runs, never a raise** — same contract
`storage.read_json` already gives for both. The history endpoint is a GET that
has to answer on a machine which has never benchmarked anything, and a
hand-edited file must not be able to take the AI Models page down.

Two costs, both accepted and neither hidden: a truly corrupt file is silently
replaced by the next append, and an unreadable RECORD is likewise dropped from
disk by the next append (`append`/`delete` are read-modify-write over `read()`).
Self-healing is the right direction for a disposable measurement log — it is not
the bookmarks — and the alternative, carrying a record forward that no reader can
render, is a file that stays broken forever.
"""
from __future__ import annotations

import os

from fused_render.shell import storage

#: How many runs are kept. Generous on purpose — a run record is a few hundred
#: bytes, so 500 of them is well under a megabyte, and the point of the feature
#: is a history long enough to see a regression across app versions. A user who
#: benchmarks four models a week reaches this in two years.
MAX_RUNS = 500

#: Envelope version. Nothing reads it yet; it exists so a future shape change
#: can migrate rather than guess, exactly as the bookmarks/deployments stores do.
VERSION = 1


def _path() -> str:
    return os.path.join(storage.home_dir(), "ai_benchmarks.json")


def _readable(run) -> bool:
    """Can a consumer render this record without guarding every field?

    The three keys every reader dereferences, and nothing else — this is not a
    schema check and must never grow into one, or adding a metric server-side
    would start silently deleting the runs recorded before it. `id` because
    `delete` and every React key need it; `metrics` and `workload` because the
    page reaches INTO them (`run.metrics[key]`, `run.workload.revision`) and a
    missing one is a `TypeError` mid-render rather than a blank cell.
    """
    return (
        isinstance(run, dict)
        and isinstance(run.get("id"), str)
        and isinstance(run.get("metrics"), dict)
        and isinstance(run.get("workload"), dict)
    )


def read() -> list[dict]:
    """Every stored run, oldest first. Absent, corrupt or wrong-shaped → `[]`.

    Records that no reader could render are dropped rather than passed on — see
    `_readable` and the module docstring.
    """
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    if not isinstance(runs, list):
        return []
    return [run for run in runs if _readable(run)]


def _write(runs: list[dict]) -> None:
    storage.write_json(_path(), {"version": VERSION, "runs": runs})


def append(run: dict) -> None:
    """Add `run` as the newest, pruning the oldest past `MAX_RUNS`.

    Read-modify-write with no locking, like every other store here: last write
    wins (D3, a single local user). Two benchmarks cannot race for it anyway —
    the router serialises runs per capability, and a run takes minutes.
    """
    runs = read()
    runs.append(run)
    if len(runs) > MAX_RUNS:
        runs = runs[len(runs) - MAX_RUNS:]
    _write(runs)


def delete(ids) -> int:
    """Drop the runs whose `id` is in `ids`; return how many actually went.

    An id that is not there is not an error — the caller wanted it gone and it
    is gone. The count is what lets the endpoint report a no-op honestly
    without turning a double-click into a 404.
    """
    wanted = set(ids)
    if not wanted:
        return 0
    runs = read()
    kept = [run for run in runs if run.get("id") not in wanted]
    removed = len(runs) - len(kept)
    if removed:
        _write(kept)
    return removed


def clear() -> None:
    """Forget every run. Separate from `delete` rather than "delete all the ids
    you just listed", so wiping the history does not depend on the client
    having read a complete, current list first."""
    _write([])
