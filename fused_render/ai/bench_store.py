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

The store is **schema-agnostic about a run**: it persists whatever
`benchmark.run()` built and reads only `id`, for delete. The record's shape is
that module's business, and keeping it out of here means adding a metric never
touches storage.

A corrupt or absent file reads as **no runs, never a raise** — same contract
`storage.read_json` already gives for both. The history endpoint is a GET that
has to answer on a machine which has never benchmarked anything, and a
hand-edited file must not be able to take the AI Models page down; the cost is
that a truly corrupt file is silently replaced by the next append, which for a
disposable measurement log is the right trade (it is not the bookmarks).
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


def read() -> list[dict]:
    """Every stored run, oldest first. Absent, corrupt or wrong-shaped → `[]`."""
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]


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
