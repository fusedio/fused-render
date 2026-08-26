"""Measured resident footprints at ~/.fused-render/ai_footprints.json (SPEC
AI-16a, D497).

One more shell-state resource in the shape `bench_store.py` establishes: a
private `_path()` over `storage.home_dir()`, then `storage.read_json` /
`storage.write_json` and nothing else. It lives under `ai/` rather than
`shell/` for the same reason `bench_store.py` does — its readers are the AI
subsystem and its router.

**Why this file exists at all.** A model's PEAK resident footprint is already
measured, for free, on every load and every benchmark run — `supervisor.
refresh_memory` re-reads `/health` on every poll, and `AI-8c`'s
`peak_resident_bytes` is a true high-water mark whenever the runner supplies
one. Nothing kept it. `fit.py` (AI-16) needs a NUMBER, not a re-measurement —
the whole point of the "measured" rung on its precedence ladder is a machine
that has already run a model once knowing better than a curator's guess or a
download's byte count — so the number the app already has has to be written
down somewhere a later page load can read it back.

**Written by `supervisor.refresh_memory`**, never by this module's own
callers directly: a Worker only knows what it measured, and `refresh_memory`
is the one place that already re-reads every live worker's health on a
cadence the rest of the app relies on. A benchmark run (AI-14) is covered by
construction rather than by a second writer — `benchmark._memory_and_device`
reads the same `describe()` rows this file's writer already covers.

**Keyed by `<capability>/<model_id>`, not by repo.** Since AI-11j the same
checkpoint can serve two capabilities with two different footprints — an
mlx-vlm load that touches the vision tower is not the load that does not —
so a repo id alone would conflate two real, different numbers under one key.

**The MACHINE is recorded once, at the top of the file, and a mismatch
discards the file wholesale.** `benchmark.machine()` states the reason a
per-run copy of machine identity exists at all: "a home directory gets
restored onto a new laptop". Every number in this file was measured on the
machine recorded here — a machine identity mismatch means every number in
the file describes hardware this process is not running on, so there is
nothing to salvage and nothing to reconcile row-by-row. Compared on
`platform`/`arch`/`totalMemoryBytes` only — NOT `cpuCount`, which
`benchmark.machine()` also reports but which is not part of what "the same
machine" means here (a VM reconfigured with a different core count while
keeping the same RAM and architecture is still the same memory budget this
file is about).

A corrupt or absent file reads as no observations, never a raise — the exact
contract `storage.read_json` already gives, restated because `fit.py`'s own
degrading precedence ladder depends on "no measurement yet" being silent
rather than a 500 on the AI Models page.

**Bounded by construction**, the discipline `server/ai_metrics.py` and
`bench_store.py` both state: at most `MAX_MODELS` rows, oldest `observedAt`
dropped on insert. A high-water is only rewritten when it GROWS, past a small
tolerance — `_GROWTH_TOLERANCE` — so a resident figure jittering by a few
bytes across polls does not rewrite the file on every `/health` read.
"""
from __future__ import annotations

import os
import time

from fused_render.shell import storage

#: `benchmark` is imported lazily, inside the functions that need `machine()`,
#: rather than at module level: `benchmark.py` imports `supervisor` at its own
#: module level (for `LOAD_WAIT_TIMEOUT_S`), and `supervisor.py` imports THIS
#: module (for `refresh_memory`'s write) — a top-level `import benchmark` here
#: would close that cycle at import time (`supervisor -> footprints ->
#: benchmark -> supervisor`), which Python reports as a partially-initialized
#: module rather than resolving. Deferred imports break the cycle without
#: changing what either module DOES.

#: How many <capability>/<model_id> rows are kept. A row is a few dozen
#: bytes; the point of the bound is not disk space, it is the same reasoning
#: `server/ai_metrics.py` and `bench_store.py` both give for their own caps —
#: an unbounded file kept by every model a user has ever loaded, on a machine
#: that never restarts the app, is a slow leak rather than a feature.
MAX_MODELS = 200

#: Envelope version, in the shape `bench_store.py` reserves one for: nothing
#: reads it yet, and it exists so a future shape change can migrate rather
#: than guess.
VERSION = 1

#: A high-water mark is only rewritten when the new reading beats the old one
#: by more than this fraction — otherwise every `/health` poll that measures
#: a few bytes of jitter around the same peak would rewrite the file. Not
#: about correctness (a slightly-late growth is still recorded on the NEXT
#: poll that clears the bar) — about not turning a passive measurement into
#: a write-storm.
_GROWTH_TOLERANCE = 0.02

#: The subset of `benchmark.machine()`'s fields that decide whether this
#: file's numbers still describe the machine running now. `cpuCount` is
#: deliberately excluded — see the module docstring.
_IDENTITY_KEYS = ("platform", "arch", "totalMemoryBytes")


def _path() -> str:
    return os.path.join(storage.home_dir(), "ai_footprints.json")


def _key(capability: str, model_id: str) -> str:
    return f"{capability}/{model_id}"


def _same_machine(recorded) -> bool:
    """Is `recorded` (the file's own `machine` object) THIS machine?

    Not `isinstance` alone — a hand-edited or truncated file could carry a
    `machine` that is not even a dict — because a malformed identity object
    must read as "not this machine" (discard and start over) rather than
    raise into a route the AI Models page depends on.
    """
    if not isinstance(recorded, dict):
        return False
    from fused_render.ai import benchmark

    current = benchmark.machine()
    return all(recorded.get(key) == current.get(key) for key in _IDENTITY_KEYS)


def _load() -> dict | None:
    """The store, already confirmed to describe THIS machine — or None.

    None covers every reason there is nothing to read: no file yet, a
    corrupt one (`storage.read_json` already returns None for that), a
    wrong-shaped one, or one written on a different machine. Every caller
    treats all four identically, which is the point — `read()` on a fresh
    machine and `read()` on a first-ever run must answer the same way.
    """
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return None
    if not _same_machine(data.get("machine")):
        return None
    models = data.get("models")
    if not isinstance(models, dict):
        return None
    return {"machine": data["machine"], "models": models}


def _write(store: dict) -> None:
    storage.write_json(_path(), {"version": VERSION, **store})


def _bounded(models: dict) -> dict:
    """`models`, capped at `MAX_MODELS` rows, oldest `observedAt` dropped.

    `_load` validates the envelope (`data`, `machine`, `models`) but never
    each ROW's shape — `peak_from_store`/`read` tolerate a non-dict row by
    `isinstance`-checking it and answering None (code review). A hand-edited
    or partially-written file can still carry one, and this is the one place
    that reads INTO a row rather than just checking for one, so it needs the
    same guard: a non-dict row sorts as `observedAt=0`, the oldest possible,
    so it is the first thing dropped once the store is over the cap rather
    than raising trying to bound it.
    """
    if len(models) <= MAX_MODELS:
        return models
    def _observed_at(kv):
        value = kv[1]
        return value.get("observedAt", 0) if isinstance(value, dict) else 0
    ordered = sorted(models.items(), key=_observed_at)
    keep = ordered[len(ordered) - MAX_MODELS:]
    return dict(keep)


def load_store() -> dict | None:
    """The whole validated store, or None — the shape `_load` returns,
    exposed for a caller that needs to answer MANY `<capability>/<model_id>`
    lookups in one request (SPEC AI-16, `fit.py`, and code review: the AI
    Models page's catalog route was calling `read()` once PER ENTRY — dozens
    of `storage.read_json` opens, JSON parses and `benchmark.machine()`
    identity checks per `GET /api/ai/catalog`, a route the picker polls).
    Load this ONCE per request and answer every entry from it through
    `peak_from_store` instead.
    """
    return _load()


def peak_from_store(store: dict | None, capability: str, model_id: str) -> int | None:
    """`read()`'s answer, but over an already-loaded `store` (from
    `load_store()`) rather than a fresh disk read — the batch half of the
    contract `read()` restates below for a single, standalone lookup.
    """
    if store is None:
        return None
    entry = store["models"].get(_key(capability, model_id))
    if not isinstance(entry, dict):
        return None
    peak = entry.get("peakBytes")
    return peak if isinstance(peak, int) and peak > 0 else None


def read(capability: str, model_id: str) -> int | None:
    """The measured peak footprint in bytes for `<capability>/<model_id>` on
    THIS machine, or None — never a figure measured on a different one.

    A single fresh load per call — correct for a one-off lookup (a load
    completing, a benchmark run), and exactly the cost `peak_from_store`
    exists to let a BATCH of lookups avoid paying once per entry.
    """
    return peak_from_store(_load(), capability, model_id)


def record(capability: str, model_id: str, peak_bytes: int) -> None:
    """Remember `peak_bytes` as this model's peak, if it is a real reading
    and either the file has nothing better or this GROWS the high-water mark.

    Read-modify-write with no locking, like every other store here (D3, a
    single local user) — `supervisor.refresh_memory` is the only writer, and
    it already runs under the supervisor's own lock for the snapshot it reads
    from, so two writers racing this file is not a real scenario.
    """
    if not isinstance(peak_bytes, int) or peak_bytes <= 0:
        return
    store = _load()
    if store is None:
        from fused_render.ai import benchmark

        # No usable prior file — a fresh one, absent, corrupt, wrong-shaped,
        # or for a DIFFERENT machine (see `_load`/`_same_machine`): every
        # number that might have been in it describes hardware this is not,
        # so nothing here is a partial update, it is a fresh start.
        store = {"machine": benchmark.machine(), "models": {}}
    key = _key(capability, model_id)
    existing = store["models"].get(key)
    existing_peak = existing.get("peakBytes") if isinstance(existing, dict) else None
    if isinstance(existing_peak, int) and peak_bytes <= existing_peak * (1 + _GROWTH_TOLERANCE):
        return
    store["models"][key] = {"peakBytes": peak_bytes, "observedAt": time.time()}
    store["models"] = _bounded(store["models"])
    _write(store)


def clear() -> None:
    """Forget every measurement. Separate from `record`, like `bench_store.
    clear`, so wiping the store does not depend on a caller having read a
    complete, current model list first."""
    from fused_render.ai import benchmark

    _write({"machine": benchmark.machine(), "models": {}})
