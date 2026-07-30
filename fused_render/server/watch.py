import asyncio
import hashlib
import json
import os

# The MODULE, not the value: `_STAT_TIMEOUT_S` is a knob mount.py documents as
# monkeypatchable, and a by-value copy here would make patching the module that
# DEFINES it a silent no-op (the same class of bug as D178's `_STAT_CACHE_GEN`).
from fused_render.server import mount as _server_mount




# ---------------------------------------------------------------------------
# fs/events watch registry
#
# Incident this exists to prevent: a read-only S3-backed rclone NFS mount died
# with the macOS "Server connections interrupted" dialog. Root cause was the
# /api/fs/events WebSocket poller calling os.stat() on every watched path every
# 200ms for the life of each socket. Each stat is a kernel NFS GETATTR; when
# the attribute cache expires it forces rclone to re-list the directory on S3,
# and for a world-scale .zarr on a slow bucket that re-list blows past the
# macOS NFS client's timeo*retrans ceiling (~2min) -> the kernel declares the
# mount dead. During the incident ~5 sockets (open preview panes + the Listing
# view) ran these loops at once, several on paths under the mount.
#
# This registry fixes the whole class of problem:
#   * ONE stat ticker per unique path, refcounted, fanned out to every socket
#     watching it (so N panes watching the same file = 1 stat/interval, not N).
#   * Stats run OFF the event loop (asyncio.to_thread) with a hard timeout, so
#     a hung NFS stat can never freeze the server's event loop. A timed-out or
#     errored stat reports "unchanged".
#   * A path with a stat still in flight never gets a second stat queued on top
#     of it — a stat hung for minutes must not spawn a thread every tick.
#   * Mount-backed paths poll slowly (5s vs 200ms) and answer via the rclone rc
#     API (mounts.rc_mtime_for), not the kernel, removing NFS from the loop
#     entirely. Local paths keep the cheap 200ms os.stat behavior.
# ---------------------------------------------------------------------------

_LOCAL_POLL_S = 0.2   # local files: cheap os.stat, snappy reload
_MOUNT_POLL_S = 5.0   # mount-backed files: rc stat, far less remote pressure
# Mount-backed paths on a remote that is NOT direct_list_capable (e.g.
# source.coop's custom S3 endpoint, not recognized as plain AWS S3): change
# detection there costs a full rc_list_dir of the prefix, not a bounded unsigned
# page. Poll such paths far less often to cut standing remote pressure, and skip
# listing a mount ROOT entirely (see _mount_signal) — fs/events P1 #4.
_MOUNT_SLOW_POLL_S = 60.0

# Sentinel distinct from every real mtime signal (float, RFC3339 str, or None
# meaning "deleted"): _read() returns it for "no change / could not determine",
# which must NOT be confused with None (a real local-deletion signal, LR-6).
_UNCHANGED = object()


def _mtime_or_none(path: str):
    """Local-file mtime signal for the poller: st_mtime, or None when the path
    is gone. None is a real change signal (deletion -> reload, LR-6), distinct
    from the _UNCHANGED sentinel returned on timeout."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _hash_listing(listed) -> str:
    """A stable change signal for a mount-backed DIRECTORY watch: a hash of its
    shallow (Name, Size, ModTime) tuples. Unlike a directory's own ModTime — a
    constant sentinel for synthetic S3 dirs — this moves whenever a child is
    created, deleted, renamed, or resized. The "L" prefix keeps it disjoint from
    a file watch's numeric/str ModTime signal."""
    h = hashlib.sha1()
    for e in listed:
        h.update(repr((e.get("Name"), e.get("Size"), e.get("ModTime"))).encode())
    return "L" + h.hexdigest()


class _WatchEntry:
    """One coalesced stat ticker for a single path, fanning changes out to
    every subscribed socket. Classified once at creation as local or
    mount-backed, which fixes both the poll interval and the stat strategy."""

    def __init__(self, path: str):
        from fused_render.shell import mounts as shell_mounts

        self.path = path
        self.is_mount = shell_mounts.is_mount_backed(path)
        # These flags fix the poll interval and change-detection strategy once,
        # at creation. direct_list_capable can do REMOTE I/O (a memoized
        # config/get rc call the first time a remote is classified), so
        # _WatchRegistry.subscribe constructs each entry OFF the event loop. It
        # resolves NO credentials — the classification is a pure config-shape
        # check (finding 12).
        if self.is_mount:
            # direct_list_capable: the remote can be enumerated by a cheap,
            # bounded page — unsigned (anonymous S3/GCS) or credentialed (signed
            # S3 / bearer GCS). When it CAN'T, a directory child-change signal
            # would require a full rc_list_dir of the prefix — the standing
            # enumeration we avoid (see _mount_signal).
            self._direct_capable = shell_mounts.direct_list_capable(path)
            # A mount ROOT lists the remote's top prefix (or the whole bucket):
            # never poll-list it on a non-direct remote.
            self._is_mount_root = shell_mounts.is_mount_root(path)
            self.interval = (_MOUNT_POLL_S if self._direct_capable
                             else _MOUNT_SLOW_POLL_S)
        else:
            self._direct_capable = False
            self._is_mount_root = False
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
            if self.is_mount:
                # rc API, NOT the kernel — a slow answer here can't kill the
                # mount. We deliberately do NOT fall back to os.stat, which is
                # the GETATTR that caused the incident.
                return await asyncio.to_thread(self._mount_signal)
            return await asyncio.to_thread(_mtime_or_none, self.path)
        except Exception:
            return _UNCHANGED

    def _mount_signal(self):
        """Change signal for a mount-backed watch, off the event loop.

        A DIRECTORY's rclone ModTime is a constant sentinel (2000-01-01) for
        synthetic S3/GCS dirs, so create/delete/rename of children never moves
        it — the mount-dir auto-refresh (Listing LS-1) was silently dead. So for
        a directory the signal is a hash of a BOUNDED shallow listing instead:
        one direct_list_page (direct-listable S3/GCS) or a short-timeout
        rc_list_dir.

        direct_list_capable is a PURE config-shape check (finding 12): it's true
        for a credentialed-SHAPED S3/GCS remote whose creds haven't been resolved.
        When they don't resolve (cloud-auth libs absent, ambient creds expired)
        direct_list_page raises DirectListError. On a non-root dir we fall back to
        rc_list_dir — the recovery the fs/list handler and the
        s3/gcs_direct_capable docstrings promise — flowing into the shared error
        ladder below. Two carve-outs keep _UNCHANGED with NO rc fallback: a mount
        ROOT (an rc listing of its whole prefix is the standing background
        enumeration refused above) and an ANONYMOUS remote (no creds to fail on;
        byte-identical to pre-finding-12 behavior).

        A FILE reaches the ModTime path differently by branch:
          - direct-listable (S3/GCS): direct_list_capable is a pure
            path/config check that can't tell a file from a directory, and
            direct_list_page on a file KEY returns an EMPTY page (the file's own
            key != the "<key>/" listing prefix). An empty page is
            indistinguishable from an empty directory, so we fall back to the
            file's operations/stat ModTime — a real, changing signal for a file;
            harmless for a genuinely empty directory, since the moment a child
            appears the page is non-empty and the listing hash takes over.
          - rc route: rc rejects listing a file as not-a-directory (RcListError),
            which likewise falls back to operations/stat ModTime.
        Any failure/timeout -> _UNCHANGED (never an error storm)."""
        from fused_render.shell import mounts as shell_mounts

        # A mount ROOT lists the remote's top prefix — or the whole bucket for a
        # bucket-root mount — which on a world-scale remote is enormous. When the
        # remote is NOT direct_list_capable (e.g. source.coop's custom S3
        # endpoint, not recognized as plain AWS S3), the only way to hash its
        # listing is an rc_list_dir of that entire prefix. Running that on every
        # tick for the life of an open pane is a standing background enumeration
        # (fs/events P1 #4), so we refuse it: change detection for such a root is
        # best-effort (no live child-change events). Direct-capable roots keep
        # their bounded single unsigned page below; non-root paths (which the
        # widened _MOUNT_SLOW_POLL_S interval already de-pressurizes) are
        # unaffected. self._direct_capable is the init-time classification; the
        # branch below re-derives it live so tests can stub the backend check.
        if self._is_mount_root and not self._direct_capable:
            return _UNCHANGED

        try:
            if shell_mounts.direct_list_capable(self.path):
                try:
                    page, _ = shell_mounts.direct_list_page(
                        self.path, max_keys=1000, timeout=4)
                except shell_mounts.DirectListError:
                    # A credentialed-SHAPED remote is "capable" by config shape
                    # alone (finding 12); when its creds don't resolve the direct
                    # pager fails. Fall back to rc_list_dir (flowing into the
                    # error ladder below) — EXCEPT a mount ROOT, whose rc listing
                    # is the standing enumeration refused above, and an ANONYMOUS
                    # remote, which has no creds to fail on and stays _UNCHANGED
                    # with no rc fallback (byte-identical to prior behavior).
                    if (self._is_mount_root
                            or shell_mounts.direct_list_anonymous(self.path)):
                        return _UNCHANGED
                    listed = shell_mounts.rc_list_dir(self.path, timeout=4)
                    return _hash_listing(listed)
                if not page:
                    # Empty: a file (its key isn't under the "<key>/" prefix) or
                    # an empty dir. Use the rc ModTime — moves for a file's
                    # content, constant-but-harmless for an empty dir.
                    m = shell_mounts.rc_mtime_for(self.path)
                    return _UNCHANGED if m is None else m
                return _hash_listing(page)
            listed = shell_mounts.rc_list_dir(self.path, timeout=4)
            return _hash_listing(listed)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout):
            return _UNCHANGED  # down / too big to list -> treat as unchanged
        except shell_mounts.RcListError:
            # Not a directory (a file): fall back to the file's ModTime.
            m = shell_mounts.rc_mtime_for(self.path)
            return _UNCHANGED if m is None else m
        except Exception:
            return _UNCHANGED  # any other backend failure -> unchanged

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
                asyncio.shield(self._inflight), _server_mount._STAT_TIMEOUT_S)
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
            # _WatchEntry construction does remote I/O (direct_list_capable now
            # consults the credential resolvers — google-auth refresh / ADC
            # metadata / botocore chain), so build it OFF the event loop. This
            # runs on the async /api/fs/events handler's loop; a bare call would
            # block it. Re-check after the await in case a concurrent subscribe
            # created the entry while we were building (only ours starts a task).
            entry = await asyncio.to_thread(_WatchEntry, path)
            existing = self._entries.get(path)
            if existing is not None:
                entry = existing
            else:
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
