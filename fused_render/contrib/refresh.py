"""The contribution loop's read side for listings, and the background work
that keeps it warm (contract §12.1–12.2).

Two things live here because they share one cache and one discipline —
NOTHING ON A REQUEST THREAD EVER TALKS TO GITHUB OR RUNS GIT:

CARDS. Every Apps-grid card carries a source kind and line ("GitHub link ·
follows alice/x", "Your app", "Remix of alice/x") and, for an app of your
own with a repo, how many proposals are waiting: `{label, inbox, kind}`.
`decorate(rows)` adds that as a `contrib` field to `/api/apps` rows, and
`cards()` answers `GET /api/contrib/cards`. Both are built from ONE read of
the record store (`store.all()`, a small JSON file) plus the in-memory inbox
counts below — never `engine.status`, never a subprocess — so a 50-app
listing pays one file read, not 50 git spawns. With `flags.sources()` off, `decorate` returns the
rows untouched: the listing is byte for byte what it was.

BACKGROUND. Three refreshes run when an app is opened (`note_app_opened`, the
same two triggers as `git_upstream.note_app_opened` — GET /render's D301 block
and `/api/apps/entry?opened=1` — plus `GET /api/contrib/app`): the proposal's
state on GitHub (`proposals.refresh`), the author's inbox (`inbox.list`) and,
for a remix, whether the original moved (`remix.check_original`). Each is
gated by its own phase flag and by the record's kind, and the whole bundle is
throttled per app (OPEN_TTL_S); the modules throttle themselves as well. A
slow sweep (SWEEP_EVERY_S, only while `flags.inbox()`) re-lists the inbox of
every own app with a repo so cards carry counts without each app being
opened, and a lowest-priority pass classifies listed apps that have no record
yet (`classify_soon`), so a card the user never opened still gets its label.

ONE LANE, TWO WORKERS. All of it goes through one priority queue served by
WORKERS daemon threads, so GitHub never sees more than two of our calls at a
time from background work, and an app being opened (priority 0) is never
stuck behind a sweep (1) or a classification pass (2). A job already queued
for the same app is not queued twice. Daemon threads, not a
ThreadPoolExecutor: an executor's workers are joined at interpreter exit,
which would hold a quit hostage to a 20 s `gh` timeout.

Every failure is logged and swallowed: a background refresh that can't reach
GitHub simply leaves the cached answer standing until the next one.
"""
from __future__ import annotations

import importlib
import itertools
import logging
import os
import queue
import threading
import time

logger = logging.getLogger(__name__)

#: One background refresh bundle per app per this long, however often it is
#: opened. proposals.refresh throttles itself to the same 60 s.
OPEN_TTL_S = 60.0
#: POST /api/contrib/refresh ("check now"): at most one per app per this long.
FORCE_TTL_S = 10.0
#: A never-listed inbox the page is waiting on is asked for at most this often
#: (an unreachable GitHub must not be re-asked on every page poll).
INBOX_KICK_TTL_S = 15.0
#: The inbox count sweep: first pass a little after startup, then every 10 min.
SWEEP_FIRST_S = 90.0
SWEEP_EVERY_S = 600.0
WORKERS = 2
#: Every per-app memory here (throttle stamps, counts, the classified set)
#: holds at most this many apps, oldest-touched first out — far above any
#: real workspace, so in practice nothing is ever forgotten early.
MAX_APPS = 2000

_P_OPEN, _P_SWEEP, _P_CLASSIFY = 0, 1, 2

#: Test seam: run submitted work inline on the calling thread.
_INLINE = False

_lock = threading.Lock()
_last_open: dict[str, float] = {}
_last_force: dict[str, float] = {}
_queued: set[tuple[str, str]] = set()
_classify_tried: dict[str, bool] = {}
_counts: dict[str, int] = {}
_inbox_kicked: dict[str, float] = {}   # key -> monotonic time of the last kick
_inbox_tried: dict[str, bool] = {}     # key -> a listing attempt has finished
_queue: queue.PriorityQueue = queue.PriorityQueue()
_seq = itertools.count()
_workers: list[threading.Thread] = []
_sweep_thread: threading.Thread | None = None
_sweep_stop = threading.Event()


def _mod(name: str):
    # Lazy and by name: engine imports git_upstream, which intercepts into
    # engine, and the listing must never pay for importing the git engine.
    return importlib.import_module(f"fused_render.contrib.{name}")


def _remember(table: dict, key: str, value) -> None:
    """`table[key] = value` as the newest entry, evicting the oldest past
    MAX_APPS. Caller holds `_lock`."""
    table.pop(key, None)
    table[key] = value
    while len(table) > MAX_APPS:
        del table[next(iter(table))]


def _workspace_app(path: str) -> bool:
    """`path` IS a workspace app folder — the folder the contrib routes'
    `app_git.app_dir_for` would resolve to. String work only."""
    from fused_render import app_git

    return app_git.app_dir_for(path) == os.path.abspath(path)


def _key(app_dir: str) -> str:
    # The store's own key (realpath'd), so a listing row's spelling and the
    # record's always meet.
    return _mod("store").key(app_dir)


# -- cards ---------------------------------------------------------------------


def own_with_repo(record: dict | None) -> bool:
    return bool(record) and record.get("kind") == "own" and bool(record.get("repo"))


def remix_of(record: dict) -> str | None:
    original = record.get("remix_of")
    if isinstance(original, dict):
        return original.get("repo") or None
    return original if isinstance(original, str) and original else None


def source(record: dict | None) -> tuple[str, str] | None:
    """`(kind, label)` — the card's source kind ("github" | "own" |
    "remix") and its source line (§13) — or None for a record that has no
    card (a scratch Try copy, or a shape this build doesn't know)."""
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind == "github" and record.get("repo"):
        return "github", f"GitHub link · follows {record['repo']}"
    if kind == "own":
        original = remix_of(record)
        return ("remix", f"Remix of {original}") if original else ("own", "Your app")
    return None


def inbox_count(app_dir: str) -> int:
    """Open proposals waiting in this app's inbox as of the last background
    list; 0 when never listed. Memory only."""
    with _lock:
        return _counts.get(_key(app_dir), 0)


def note_inbox(app_dir: str, items) -> None:
    """Record the size of an inbox list someone just fetched."""
    if isinstance(items, list):
        key = _key(app_dir)
        with _lock:
            _remember(_counts, key, len(items))


def _list_inbox(app_dir: str, **kwargs) -> None:
    """`inbox.list` for the open bundle, the sweep and the page's kick. The
    count is recorded only when the listing actually reached GitHub —
    `inbox.list` answers `[]` on failure too, and `cached()` staying None
    is what tells the two apart — so a failure never reads as "no
    proposals". Either way the attempt is marked finished."""
    inbox = _mod("inbox")
    items = None
    try:
        inbox.list(app_dir, **kwargs)
        items = inbox.cached(app_dir)
    finally:
        key = _key(app_dir)
        with _lock:
            _remember(_inbox_tried, key, True)
    note_inbox(app_dir, items)


def inbox_snapshot(app_dir: str) -> dict:
    """The page's `inbox` (§12.1) with NO network: `{items, count, loading}`.

    Never listed yet → `items: [], count: 0, loading: true` and a listing is
    kicked right away at open priority (at most once per INBOX_KICK_TTL_S);
    `loading` stays true while that attempt — or the open bundle, which
    lists too and so is not doubled — is queued or running. An attempt
    that finished without reaching GitHub → `loading: false, unavailable:
    true` — never a confident "no proposals"."""
    inbox = _mod("inbox")
    items = inbox.cached(app_dir)
    if isinstance(items, list):
        note_inbox(app_dir, items)
        return {"items": items, "count": len(items), "loading": False}
    key = _key(app_dir)
    now = time.monotonic()
    with _lock:
        last = _inbox_kicked.get(key)
        due = ((last is None or now - last >= INBOX_KICK_TTL_S)
               and ("open", key) not in _queued)
        if due:
            _remember(_inbox_kicked, key, now)
    if due:
        _submit(_P_OPEN, "inbox-open", key, lambda: _list_inbox(app_dir))
        items = inbox.cached(app_dir)  # an inline run (tests) may have listed
        if isinstance(items, list):
            return {"items": items, "count": len(items), "loading": False}
    with _lock:
        loading = (("inbox-open", key) in _queued or ("open", key) in _queued
                   or key not in _inbox_tried)
    out = {"items": [], "count": 0, "loading": loading}
    if not loading:
        out["unavailable"] = True
    return out


def inbox_changed(app_dir: str) -> None:
    """A proposal just left the inbox here (Accept, Decline): `inbox` has
    dropped its cached list, so forget this app's kick throttle and finished
    attempt — otherwise the next read would wait out INBOX_KICK_TTL_S and
    answer `unavailable` ("can't reach GitHub") for a list that is merely
    stale — and list again now. The page reads `loading: true` meanwhile."""
    key = _key(app_dir)
    with _lock:
        _inbox_kicked.pop(key, None)
        _inbox_tried.pop(key, None)
        _remember(_inbox_kicked, key, time.monotonic())
    _submit(_P_OPEN, "inbox-open", key, lambda: _list_inbox(app_dir))


def _card(key: str, record: dict, inbox_on: bool) -> dict | None:
    src = source(record)
    if src is None:
        return None
    count = _counts.get(key, 0) if inbox_on and own_with_repo(record) else 0
    return {"label": src[1], "inbox": count, "kind": src[0]}


def cards() -> dict:
    """`{app_dir: {label, inbox, kind}}` for every recorded app whose folder
    still exists (keys are the store's realpath'd ones) — the
    GET /api/contrib/cards answer. `{}` with sources off."""
    flags = _mod("flags").snapshot()
    if not flags["sources"]:
        return {}
    records = {k: r for k, r in (_mod("store").all() or {}).items() if os.path.isdir(k)}
    out = {}
    with _lock:
        for key, record in records.items():
            card = _card(key, record, flags["inbox"])
            if card is not None:
                out[key] = card
    return out


def decorate(rows: list[dict]) -> list[dict]:
    """Add `contrib: {label, inbox, kind}` to each listing row that has a record, in
    place. Rows must be the caller's own dicts — never the discovery
    snapshot's shared ones. Rows with no record yet are handed to
    `classify_soon` so the next listing has their label."""
    try:
        flags = _mod("flags").snapshot()
        if not flags["sources"] or not rows:
            return rows
        store = _mod("store")
        records = store.all() or {}
    except Exception:  # noqa: BLE001 — a card label must never fail the listing
        logger.warning("contrib card labels unavailable", exc_info=True)
        return rows
    keyed = [(row, store.key(row["path"])) for row in rows
             if isinstance(row.get("path"), str)]
    unrecorded = []
    with _lock:
        for row, key in keyed:
            record = records.get(key)
            if record is None:
                unrecorded.append(row["path"])
                continue
            card = _card(key, record, flags["inbox"])
            if card is not None:
                row["contrib"] = card
    if unrecorded:
        classify_soon(unrecorded)
    return rows


def github_identity() -> dict:
    """`{signed_in, login, checking}` for GET /api/contrib/app, from
    `github_setup.cached_summary()` — the snapshot as it stands, never
    measured inline (`summary()` would re-measure past 60 s: two `gh`
    spawns a page read must not wait on) — with a background re-measure when
    it is missing or stale. `checking: true` = this machine has never been
    measured, so `signed_in: false` is not yet an answer."""
    from fused_render import github_setup

    cached = github_setup.cached_summary()
    if cached is None or cached.get("stale"):
        _submit(_P_OPEN, "github-status", "", github_setup.summary)
    signed_in = bool(cached and cached.get("signed_in"))
    login = cached.get("account") if signed_in else None
    return {"signed_in": signed_in, "login": login if isinstance(login, str) else None,
            "checking": cached is None}


# -- the lane --------------------------------------------------------------------


def _worker() -> None:
    while True:
        _prio, _n, tag, fn = _queue.get()
        try:
            fn()
        except Exception:  # noqa: BLE001 — background, best-effort
            logger.warning("contrib background %s failed", tag[0], exc_info=True)
        finally:
            with _lock:
                _queued.discard(tag)


def _submit(prio: int, what: str, key: str, fn) -> bool:
    tag = (what, key)
    with _lock:
        if tag in _queued:
            return False
        _queued.add(tag)
        if not _INLINE:
            while len(_workers) < WORKERS:
                t = threading.Thread(target=_worker, daemon=True,
                                     name=f"contrib-bg-{len(_workers)}")
                _workers.append(t)
                t.start()
    if _INLINE:
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.warning("contrib background %s failed", what, exc_info=True)
        finally:
            with _lock:
                _queued.discard(tag)
        return True
    _queue.put((prio, next(_seq), tag, fn))
    return True


def _safely(what: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — one piece failing must not skip the rest
        logger.warning("contrib %s failed", what, exc_info=True)
        return None


def _refresh_app(app_dir: str, *, force: bool) -> None:
    """The open bundle: classify, then each refresh its phase and the
    record's kind allow. Runs on a worker (or the POST /refresh thread)."""
    flags = _mod("flags")
    if not flags.sources() or not os.path.isdir(app_dir):
        return
    record = _safely("classify", _mod("classify").ensure, app_dir)
    if not isinstance(record, dict):
        return
    kind = record.get("kind")
    # Any kind with a proposal on record: a remix (now "own") keeps the
    # proposal it sent while it was a GitHub-link copy, and GitHub's answer
    # to it still has to reach the page.
    if flags.changes() and record.get("proposal"):
        _safely("proposal refresh", _mod("proposals").refresh, app_dir, force=force)
    if flags.inbox() and own_with_repo(record):
        _safely("inbox list", _list_inbox, app_dir, force=force)
    if flags.remix() and kind == "own" and remix_of(record):
        _safely("original check", _mod("remix").check_original, app_dir)


def note_app_opened(app_dir: str) -> bool:
    """An app was just opened: queue its refresh bundle, at most once per
    OPEN_TTL_S. Returns whether one was queued. Request-thread cost: a prefs
    read and a dict lookup — no git, no network. Only workspace apps: the
    contrib routes accept nothing else, so nothing else is classified."""
    try:
        if not _workspace_app(app_dir) or not _mod("flags").sources():
            return False
    except Exception:  # noqa: BLE001 — housekeeping must never fail an open
        return False
    key = _key(app_dir)
    now = time.monotonic()
    with _lock:
        last = _last_open.get(key)
        if last is not None and now - last < OPEN_TTL_S:
            return False
        _remember(_last_open, key, now)
    return _submit(_P_OPEN, "open", key, lambda: _refresh_app(app_dir, force=False))


def refresh_now(app_dir: str) -> bool:
    """POST /api/contrib/refresh: run the bundle NOW on the calling thread,
    forcing the modules past their own throttles, at most once per app per
    FORCE_TTL_S. False when throttled (the caller still answers ok: the
    cached state it reads next is at most 10 s old)."""
    key = _key(app_dir)
    now = time.monotonic()
    with _lock:
        last = _last_force.get(key)
        if last is not None and now - last < FORCE_TTL_S:
            return False
        _remember(_last_force, key, now)
        # A forced check is also an open's worth of freshness.
        _remember(_last_open, key, now)
    _refresh_app(app_dir, force=True)
    return True


def classify_soon(paths: list[str]) -> None:
    """Classify listed workspace apps that have no record yet, at the lowest
    priority, each at most once per process (a folder that fails to classify
    is not retried on every listing poll)."""
    classify = None
    for path in paths:
        if not _workspace_app(path):
            continue
        key = _key(path)
        with _lock:
            if key in _classify_tried:
                continue
            _remember(_classify_tried, key, True)
        if classify is None:
            classify = _mod("classify")
        _submit(_P_CLASSIFY, "classify", key, lambda p=path, c=classify: c.ensure(p))


# -- the sweep -------------------------------------------------------------------


def _count_inbox(app_dir: str) -> None:
    flags = _mod("flags")
    if not flags.inbox() or not os.path.isdir(app_dir):
        return
    # review=False: a background count must not start a Claude safety review
    # for every proposal on every app — reviews start when the page opens.
    _list_inbox(app_dir, review=False)


def sweep_once() -> int:
    """Queue an inbox count for every own app with a repo. Returns how many."""
    if not _mod("flags").inbox():
        return 0
    queued = 0
    for app_dir, record in (_mod("store").all() or {}).items():
        if own_with_repo(record) and os.path.isdir(app_dir):
            if _submit(_P_SWEEP, "inbox", app_dir, lambda d=app_dir: _count_inbox(d)):
                queued += 1
    return queued


def _sweep_loop() -> None:
    wait = SWEEP_FIRST_S
    while not _sweep_stop.wait(wait):
        wait = SWEEP_EVERY_S
        _safely("inbox sweep", sweep_once)
        # Whatever the flags say: with the inbox off nothing lists the Try
        # copies, so this pass (the first one a little after startup) is
        # what removes them.
        _safely("try-copy sweep", _mod("trycopy").sweep_orphans)


def start() -> None:
    """Start the sweep thread (server startup). Idempotent. The thread reads
    the flag on every tick, so turning A3 on later needs no restart."""
    global _sweep_thread
    with _lock:
        if _sweep_thread is not None and _sweep_thread.is_alive():
            return
        _sweep_stop.clear()
        _sweep_thread = threading.Thread(target=_sweep_loop, daemon=True,
                                         name="contrib-sweep")
        _sweep_thread.start()


def stop() -> None:
    _sweep_stop.set()


def _reset() -> None:
    """Test hook: forget every throttle, count and queued tag."""
    with _lock:
        _last_open.clear()
        _last_force.clear()
        _queued.clear()
        _classify_tried.clear()
        _counts.clear()
        _inbox_kicked.clear()
        _inbox_tried.clear()
