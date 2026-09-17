# Decisions and spec corrections — index freshness, export, path bar

## Workstream A — compaction heartbeat

- The spec says "extend `tests/test_index_runtime.py`". That file is unrelated:
  it tests the `fused.fileIndex.*` JS runtime bridge (`static/runtime.js`), not
  the scan/compaction control plane. The tests instead extend
  `tests/test_index_store.py`, which already has `compact()` coverage
  including phase-event assertions, plus one test that imports `runner` to
  check `_looks_abandoned` directly against a compaction's own event stream.
- The fix emits a `phase` event (not a `progress` event) after each partition
  write. `derive_state` overwrites `dirs`/`files`/`reused` with whatever a
  `progress` event carries (defaulting to 0 for fields it omits), and
  `_compact_locked` has no access to the run's dirs/files/reused totals — only
  `run_scan` does. A `progress` event from inside compaction would reset the
  displayed counters to 0 while the merge is still running, which is a visible
  regression the bug report does not ask for. `phase` only touches `state["phase"]`
  and already carries the loop's two existing calls ("writing index",
  "writing signatures"), so a per-partition phase message
  ("writing index (partition i/n)") keeps existing behavior and satisfies the
  watchdog, which only cares about the run directory's mtime, not event type.
- Regression test note: a fast in-test compaction cannot reproduce the bug on
  real wall-clock time (a few small partitions finish in milliseconds either
  way), so `test_compaction_progress_keeps_the_watchdog_from_reporting_abandoned`
  drives `_emit`'s clock through a fake, monotonically-advancing value passed
  to `os.utime` after each emit, sized so that two emits alone (the old
  behavior) would span past `ABANDONED_RUN_S` while the new per-partition
  cadence never lets any single gap approach it.
