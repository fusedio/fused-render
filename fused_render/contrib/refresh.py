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
rows untouched: the listing is byte for byte what it was — except, with the
Marketplace switch on (`flags.marketplace_ui`, §18.8), marketplace copies'
cards and GitHub-link copies' block warnings.

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
_inbox_reached: dict[str, bool] = {}   # key -> did the last finished listing reach GitHub
_inbox_last: dict[str, list] = {}      # key -> the last list GitHub gave, kept for display
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
    "remix" | "marketplace", §18.4) and its source line (§13) — or None for a record that has no
    card (a scratch Try copy, or a shape this build doesn't know)."""
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind == "github" and record.get("repo"):
        return "github", f"GitHub link · follows {record['repo']}"
    if kind == "marketplace":
        listing = record.get("listing") if isinstance(record.get("listing"), dict) else {}
        return "marketplace", ("Community · no longer listed"
                               if listing.get("status") == "delisted"
                               else "Community · reviewed")
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
    """Record an inbox list GitHub just answered: its size (the card's
    count) and the items themselves (what the page keeps showing while a
    later listing runs)."""
    if isinstance(items, list):
        key = _key(app_dir)
        with _lock:
            _remember(_counts, key, len(items))
            _remember(_inbox_last, key, list(items))


def _list_inbox(app_dir: str, **kwargs) -> None:
    """`inbox.list` for the open bundle, the sweep and the page's kick. The
    list is recorded only when the listing actually reached GitHub —
    `inbox.list` answers `[]` on failure too, and `cached()` staying None
    is what tells the two apart — so a failure never reads as "no
    proposals". Either way the attempt's outcome is recorded
    (`_inbox_reached`: True reached GitHub, False did not)."""
    inbox = _mod("inbox")
    items = None
    try:
        inbox.list(app_dir, **kwargs)
        items = inbox.cached(app_dir)
    finally:
        key = _key(app_dir)
        with _lock:
            _remember(_inbox_reached, key, isinstance(items, list))
    note_inbox(app_dir, items)


def inbox_snapshot(app_dir: str) -> dict:
    """The page's `inbox` (§12.1) with NO network:
    `{items, count, loading, unavailable?}`.

    Listed → the list, `loading: false`. Not listed right now (never, or
    the cache was dropped) → a listing is kicked at open priority (at most
    once per INBOX_KICK_TTL_S), and the answer is the LAST list GitHub gave
    (empty if none) with `loading` true while that attempt — or the open
    bundle, which lists too and so is not doubled — is queued or running.
    `unavailable: true` whenever the last attempt could not reach GitHub,
    including while a re-kick is loading, so an offline page never flickers
    from "can't reach GitHub" to "loading" and back on every poll."""
    inbox = _mod("inbox")
    # The page path starts the review of any item that has none (queued
    # behind inbox's review slots); the sweep and _list_inbox never do.
    items = inbox.cached(app_dir, start_reviews=True)
    if isinstance(items, list):
        note_inbox(app_dir, items)
        # A list kept across an invalidate (minus the closed proposal) is
        # shown with a loading mark until the fresh one lands.
        return _with_age({"items": items, "count": len(items),
                          "loading": bool(inbox.refreshing(app_dir))}, inbox, app_dir)
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
        items = inbox.cached(app_dir, start_reviews=True)  # an inline run (tests) may have listed
        if isinstance(items, list):
            return _with_age({"items": items, "count": len(items),
                              "loading": bool(inbox.refreshing(app_dir))}, inbox, app_dir)
    with _lock:
        running = ("inbox-open", key) in _queued or ("open", key) in _queued
        failed = _inbox_reached.get(key) is False
        shown = list(_inbox_last.get(key) or [])
    # Not failed = never tried, or the cache was dropped after a good list:
    # either way a listing is on its way (kicked now or within the TTL).
    out = {"items": shown, "count": len(shown), "loading": running or not failed}
    if failed:
        out["unavailable"] = True
    return out


def inbox_changed(app_dir: str, number: int | None = None) -> None:
    """A proposal just left the inbox here (Accept, Decline): `inbox` has
    dropped or trimmed its cached list, so forget this app's kick throttle
    and last outcome — otherwise the next read would wait out
    INBOX_KICK_TTL_S and answer `unavailable` for a list that is merely
    stale — drop `number` from the list kept for display, and list again
    now. The page reads `loading: true` meanwhile, with the rest still
    shown."""
    key = _key(app_dir)
    with _lock:
        _inbox_kicked.pop(key, None)
        _inbox_reached.pop(key, None)
        _remember(_inbox_kicked, key, time.monotonic())
        kept = _inbox_last.get(key)
        if kept is not None and number is not None:
            _remember(_inbox_last, key,
                      [i for i in kept if not (isinstance(i, dict) and i.get("number") == number)])
    _submit(_P_OPEN, "inbox-open", key, lambda: _list_inbox(app_dir))


def _card(key: str, record: dict, inbox_on: bool) -> dict | None:
    src = source(record)
    if src is None:
        return None
    count = _counts.get(key, 0) if inbox_on and own_with_repo(record) else 0
    card = {"label": src[1], "inbox": count, "kind": src[0]}
    # §18.4 B4: the block a marketplace copy ran into (`{level, reason,
    # what_to_do, confirmed, …}` — the card's badge and the soft-block
    # confirm sheet), or a GitHub-link copy's warning. Record only: no git.
    block = record.get("block") if src[0] == "marketplace" else record.get("block_warn")
    if isinstance(block, dict):
        card["block"] = _block_view(block, warn=src[0] != "marketplace")
    return card


def _block_view(block: dict, *, warn: bool) -> dict | None:
    try:
        view = _mod("blocks").view(block)
    except Exception:  # noqa: BLE001 — a card must never fail the listing
        return None
    if view is not None and warn:
        view["warn"] = True
    return view


def cards() -> dict:
    """`{app_dir: {label, inbox, kind}}` for every recorded app whose folder
    still exists (keys are the store's realpath'd ones) — the
    GET /api/contrib/cards answer. `{}` with sources off."""
    flags = _mod("flags").snapshot()
    market_ui = flags["sources"] or _mod("flags").marketplace_ui()
    if not market_ui:
        return {}
    records = {k: r for k, r in (_mod("store").all() or {}).items() if os.path.isdir(k)}
    out = {}
    with _lock:
        for key, record in records.items():
            if not flags["sources"] and not _market_card(record):
                continue
            card = _card(key, record, flags["inbox"])
            if card is not None:
                out[key] = card
    return out


def _market_card(record) -> bool:
    """A card the Marketplace switch alone shows (§18.8): a marketplace
    copy's, and a GitHub-link copy's block warning (block enforcement and
    its words are switch-independent)."""
    return isinstance(record, dict) and (
        record.get("kind") == "marketplace"
        or (record.get("kind") == "github" and isinstance(record.get("block_warn"), dict)))


def decorate(rows: list[dict]) -> list[dict]:
    """Add `contrib: {label, inbox, kind}` to each listing row that has a record, in
    place. Rows must be the caller's own dicts — never the discovery
    snapshot's shared ones. Rows with no record yet are handed to
    `classify_soon` so the next listing has their label."""
    try:
        flags = _mod("flags").snapshot()
        if not rows or not (flags["sources"] or _mod("flags").marketplace_ui()):
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
                if flags["sources"]:
                    unrecorded.append(row["path"])
                continue
            if not flags["sources"] and not _market_card(record):
                continue  # the loop off: only the Marketplace's own cards (§18.8)
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


def _recheck_identity(app_dir: str) -> None:
    """Before a forced refresh classifies a copy whose ownership was never
    checked (classified while signed out): ask GitHub who the user is NOW,
    past `gh.identity`'s 10-minute cache and `github_setup`'s snapshot — the
    page's "Sign in so fused can tell whether this app is yours" ends in
    exactly this refresh, and a cached "signed out" would make the
    `classify.ensure` that follows re-check against nobody."""
    record = _mod("store").get(app_dir)
    if not record or record.get("kind") != "github" or record.get("owner_checked"):
        return
    _safely("identity refresh", _mod("gh").identity, refresh=True)
    from fused_render import github_setup

    _safely("github status refresh", github_setup.summary_refreshed)


def _refresh_app(app_dir: str, *, force: bool) -> None:
    """The open bundle: classify, then each refresh its phase and the
    record's kind allow. Runs on a worker (or the POST /refresh thread)."""
    flags = _mod("flags")
    if not flags.sources() or not os.path.isdir(app_dir):
        return
    if force:
        _recheck_identity(app_dir)
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
    cached state it reads next is at most 10 s old) — except the ownership
    re-check, which runs regardless: it is what a sign-in that just finished
    is waiting on, and it costs one GitHub call."""
    key = _key(app_dir)
    now = time.monotonic()
    with _lock:
        last = _last_force.get(key)
        throttled = last is not None and now - last < FORCE_TTL_S
        if not throttled:
            _remember(_last_force, key, now)
            # A forced check is also an open's worth of freshness.
            _remember(_last_open, key, now)
    if throttled:
        _recheck_identity(app_dir)
        _safely("classify", _mod("classify").ensure, app_dir)
        return False
    _refresh_app(app_dir, force=True)
    return True


def _with_age(out: dict, inbox, app_dir: str) -> dict:
    """`listed_at` (epoch seconds the list was read from GitHub) on the
    snapshot when the inbox knows it — the page says "from 2 min ago";
    absent for a stand-in module without the question (the route tests)."""
    fn = getattr(inbox, "listed_at", None)
    at = fn(app_dir) if callable(fn) else None
    if at:
        out["listed_at"] = at
    return out


def forced_ago(app_dir: str) -> float | None:
    """Seconds since the last forced check of this app ran (None: never)."""
    with _lock:
        last = _last_force.get(_key(app_dir))
    return None if last is None else max(0.0, time.monotonic() - last)


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
        # §18.4 B4: the Marketplace block list, every 30 minutes (its own
        # cadence inside), enforced whatever the switches say.
        _safely("block list", _mod("blocks").sweep_tick)


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
        _inbox_reached.clear()
        _inbox_last.clear()
