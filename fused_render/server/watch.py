import asyncio
import json
import os

# The MODULE, not the value: `_STAT_TIMEOUT_S` is a knob fs_stat.py documents as
# monkeypatchable, and a by-value copy here would make patching the module that
# DEFINES it a silent no-op.
from fused_render.server import fs_stat as _server_fs_stat


# ---------------------------------------------------------------------------
# fs/events watch registry
#
#   * ONE stat ticker per unique path, refcounted, fanned out to every socket
#     watching it (so N panes watching the same file = 1 stat/interval, not N).
#   * Stats run OFF the event loop (asyncio.to_thread) with a hard timeout, so
#     a hung stat can never freeze the server's event loop. A timed-out or
#     errored stat reports "unchanged".
#   * A path with a stat still in flight never gets a second stat queued on top
#     of it — a stat hung for minutes must not spawn a thread every tick.
# ---------------------------------------------------------------------------

_LOCAL_POLL_S = 0.2   # cheap os.stat, snappy reload

# Sentinel distinct from every real mtime signal (float or None meaning
# "deleted"): _read() returns it for "no change / could not determine", which
# must NOT be confused with None (a real deletion signal, LR-6).
_UNCHANGED = object()


def _mtime_or_none(path: str):
    """File mtime signal for the poller: st_mtime, or None when the path is
    gone. None is a real change signal (deletion -> reload, LR-6), distinct
    from the _UNCHANGED sentinel returned on timeout."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class _WatchEntry:
    """One coalesced stat ticker for a single path, fanning changes out to
    every subscribed socket."""

    def __init__(self, path: str):
        self.path = path
        self.interval = _LOCAL_POLL_S
        self.subscribers: set = set()  # asyncio.Queue per socket
        self.last = _UNCHANGED  # primed by the first successful read
        self._inflight = None   # in-progress stat task; guards against pile-up
        self.task = None        # the ticker task

    async def _stat_signal(self):
        """The change signal for this path, off the event loop. Never raises:
        any error becomes _UNCHANGED so a transient failure never masquerades
        as a change (which would spuriously reload the pane)."""
        try:
            return await asyncio.to_thread(_mtime_or_none, self.path)
        except Exception:
            return _UNCHANGED

    async def _read(self):
        """One tick's read with a hard timeout and in-flight de-duplication.

        asyncio.wait_for cancels its awaitable on timeout, but the underlying
        stat/listing runs in a thread that cannot be cancelled — so we shield
        the task and, on timeout, leave it running and report _UNCHANGED. The
        still-running task then guards the NEXT tick: while it is hung (possibly
        for minutes) we never stack a second thread on top of it. But once it
        FINISHES (a slow stat that outlived its wait_for), the next tick must
        CONSUME its result rather than discard a done future and start over —
        otherwise a path whose stat always takes >_STAT_TIMEOUT_S never primes
        and 100% of the work is wasted."""
        if self._inflight is not None:
            if not self._inflight.done():
                return _UNCHANGED  # previous read still hanging; skip this tick
            sig = self._inflight.result()  # _stat_signal never raises
            self._inflight = None
            return sig
        self._inflight = asyncio.ensure_future(self._stat_signal())
        try:
            sig = await asyncio.wait_for(
                asyncio.shield(self._inflight), _server_fs_stat._STAT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return _UNCHANGED  # leave _inflight running; consumed on a later tick
        self._inflight = None
        return sig

    def _broadcast(self, sig):
        msg = json.dumps({"path": self.path, "mtime": sig})
        for q in list(self.subscribers):
            q.put_nowait(msg)

    async def run(self):
        # First read primes the baseline WITHOUT broadcasting, so connecting a
        # socket never triggers an immediate reload. A late subscriber joining
        # an already-running ticker inherits the current baseline the same way.
        while True:
            sig = await self._read()
            if sig is not _UNCHANGED:
                if self.last is not _UNCHANGED and sig != self.last:
                    self._broadcast(sig)
                self.last = sig
            await asyncio.sleep(self.interval)


class _WatchRegistry:
    """Module-level map of path -> _WatchEntry, refcounted by subscriber count.
    subscribe() attaches a socket's queue (starting the ticker on the first
    subscriber); unsubscribe() detaches it (stopping the ticker on the last)."""

    def __init__(self):
        self._entries: dict = {}

    async def subscribe(self, path: str, queue):
        entry = self._entries.get(path)
        if entry is None:
            entry = _WatchEntry(path)
            self._entries[path] = entry
            entry.task = asyncio.create_task(entry.run())
        entry.subscribers.add(queue)
        return entry

    def unsubscribe(self, entry, queue):
        entry.subscribers.discard(queue)
        if not entry.subscribers:
            if entry.task is not None:
                entry.task.cancel()
            self._entries.pop(entry.path, None)


_WATCH_REGISTRY = _WatchRegistry()
