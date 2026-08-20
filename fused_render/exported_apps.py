"""Exported apps: ``.fused`` app files anywhere on disk, listed on /apps.

The workspace walk sees folders inside ``~/Fused`` and the registered-apps
store sees folders the user explicitly opened — but an exported ``.fused``
file (SPEC §43) lands wherever the browser saved it and is invisible to both.
Being a FILE with a known extension, its discovery is an INDEX FACT, exactly
like git_repos.py's ".git dirs row" fact: one duckdb query over the files
table (``ext = 'fused'``), zero walks, zero stats beyond a per-row existence
check. The /apps hub merges the results in under the reserved virtual tag
``exported``.

Two deliberate deviations from the git_repos posture:

* NO not-ready/error states. /api/apps must keep serving workspace and
  registered apps whatever the index's condition, so "the index cannot answer"
  is zero exported rows, silently — never a 502, never a reason field. The
  freshness nudge (`_note_tab_opened`) still fires so a hub visit is a chance
  for the index to catch up.
* The index rows are UNIONED with this module's own recents store. A file
  exported five minutes ago is exactly the one the user is looking for and
  exactly the one a debounced scan has not seen yet; recording the open
  (``record_open``, called by POST /api/appfile/open — the one moment the
  source path is known) both feeds `opened_at` AND bridges the staleness gap.

The recents store is ``~/.fused-render/appfile_recents.json``
(``{"entries": [{"path": <abs .fused>, "openedAt": <iso>}]}``, newest-first,
one entry per path). Deliberately NOT ``registered_apps.json``: that store's
``record_open`` validates isdir + app_entry because everything in it feeds
folder syscalls, and loosening it to accept files would weaken a posture the
listing depends on.

Screening mirrors git_repos: ``junk_path`` on the file's path (named cost: a
``.fused`` kept inside a dotted directory is not listed), MountGuard string
checks before any syscall, then one ``os.path.isfile``. Rows are deduped by
normcase'd abspath; two copies of the same content at two paths are honestly
two cards.
"""
import itertools
import logging
import os
import threading
import time
from datetime import datetime, timezone

from fused_render._view_url_codec import canonical_fs_path
from fused_render.index.config import load_config
from fused_render.index.ignore import MountGuard
from fused_render.index.query import files_src
from fused_render.index.store import read_manifest
from fused_render.shell import storage

logger = logging.getLogger(__name__)

# The reserved virtual tag exported app files share on the /apps hub — the
# "Repo" facet chip. Same posture as registered_apps.REGISTERED_TAG: a
# workspace folder literally named `exported/` merges chips with it, accepted.
EXPORTED_TAG = "exported"

# Same cap posture as APP_RECENTS_CAP / REGISTERED_APPS_CAP: the store is
# user-writable and otherwise unbounded.
APPFILE_RECENTS_CAP = 200

# /api/apps is a hot endpoint and `ext` has no partition prune (extension says
# nothing about path prefixes), so the query is a full files-table scan. A
# short TTL keeps repeat hub renders from re-scanning parquet while staying
# well inside "the index itself only moves on a scan" freshness.
_CACHE_TTL_S = 20.0
_cache_lock = threading.Lock()
# Keyed by the index dir: the store location can move (FUSED_RENDER_HOME,
# branch-scoped homes), and a cached answer for one store must not answer
# for another.
_cache: tuple[str, float, list[tuple[str, float]]] | None = None

# Round-robin turn counter for the freshness nudge — same shape and reasoning
# as git_repos._root_turn (one root offered per request, counter never reset).
_root_turn = itertools.count()


def _recents_path() -> str:
    return os.path.join(storage.home_dir(), "appfile_recents.json")


def read_recents() -> list[dict]:
    """The store's valid entries, stored order (newest-open first). Corrupt or
    missing file reads as empty — a store degrades, never raises."""
    data = storage.read_json(_recents_path())
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    return [
        e
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict)
        and isinstance(e.get("path"), str)
        and os.path.isabs(e["path"])
    ]


def record_open(path: str) -> bool:
    """Record an open of the ``.fused`` at ``path`` (or refresh its
    ``openedAt``). False for anything that isn't a plain existing ``.fused``
    file on an unwedged mount — the same benign no-op posture as
    registered_apps.record_open, since everything stored here feeds syscalls
    in GET /api/apps."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    path = os.path.abspath(path)
    if not path.lower().endswith(".fused"):
        return False
    # BEFORE any syscall on the candidate: the guard answers from mount
    # records with pure string work.
    if MountGuard().blocks(path):
        return False
    try:
        if not os.path.isfile(path):
            return False
    except OSError:
        return False
    key = os.path.normcase(path)
    kept = [e for e in read_recents()
            if os.path.normcase(os.path.abspath(e["path"])) != key]
    entry = {"path": path, "openedAt": datetime.now(timezone.utc).isoformat()}
    storage.write_json(_recents_path(),
                       {"entries": [entry, *kept][:APPFILE_RECENTS_CAP]})
    return True


def _opened_epoch(ts) -> float | None:
    """`openedAt` as epoch seconds, or None for a malformed user-edited value
    — same tolerance as registered_apps._opened_epoch."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _note_hub_opened(cfg) -> None:
    """The git_repos freshness nudge, for the same reason: this listing reads
    nothing but the index, so a hub visit is its one chance to notice a stale
    one. One configured root per request, round-robin (see git_repos.
    _note_tab_opened on why offering every root checks only the first)."""
    try:
        from fused_render.server.routers.index import note_folder_opened, scan_roots

        roots = scan_roots(cfg)
        if not roots:
            return
        note_folder_opened(roots[next(_root_turn) % len(roots)])
    except Exception:  # noqa: BLE001 - housekeeping must never become the answer
        logger.debug("could not check index freshness for exported apps",
                     exc_info=True)


def _indexed_fused_files(cfg) -> list[tuple[str, float]]:
    """(path, mtime) for every indexed ``.fused`` file, TTL-cached. Any
    index condition that prevents an answer — never built, unreadable,
    query failure — is an empty list, not an error (module docstring)."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if (_cache is not None and _cache[0] == cfg.dir
                and now - _cache[1] < _CACHE_TTL_S):
            return _cache[2]
    rows: list[tuple[str, float]] = []
    try:
        manifest = read_manifest(cfg)
        parts = (manifest or {}).get("partitions") or []
        if parts:
            import duckdb

            con = duckdb.connect()
            rows = con.execute(
                f"SELECT path, mtime FROM {files_src(cfg, parts)} "
                "WHERE ext = 'fused'").fetchall()
    except Exception:  # noqa: BLE001 - an unanswerable index is zero rows
        logger.debug("the exported-apps index query failed", exc_info=True)
        rows = []
    with _cache_lock:
        _cache = (cfg.dir, now, rows)
    return rows


def _clear_cache() -> None:
    """Test hook: drop the TTL cache so a rebuilt index is seen immediately."""
    global _cache
    with _cache_lock:
        _cache = None


def exported_apps() -> list[dict]:
    """Every discoverable ``.fused`` file as an /apps listing dict.

    Shape rides the app_listing.app_dict contract so the hub's sort, chips
    and cards need no special cases: ``entry`` is the ``.fused`` file itself
    (a card click opens it in the explorer, where the fusedapp template
    renders it — no new open path), ``entry_html`` is None (the entry is not
    a renderable page, so the card shows the empty thumb and /render is never
    pointed at it), ``kind: "appfile"`` is the one new field, for the surfaces
    that must not offer folder actions on a file."""
    from fused_render.server.walk import junk_path

    cfg = load_config()
    # Fire-and-forget, like git_repos: the list is served from whatever the
    # index holds right now.
    _note_hub_opened(cfg)

    # Index rows first, then recents-only paths (a just-exported file the
    # scan hasn't seen). The index's mtime wins where both know the file.
    candidates: dict[str, tuple[str, float | None]] = {}
    for path, mtime in _indexed_fused_files(cfg):
        candidates.setdefault(os.path.normcase(path), (path, mtime))
    opened_at: dict[str, float | None] = {}
    for e in read_recents():
        path = os.path.abspath(e["path"])
        key = os.path.normcase(path)
        opened_at.setdefault(key, _opened_epoch(e.get("openedAt")))
        candidates.setdefault(key, (path, None))

    guard = MountGuard()
    apps: list[dict] = []
    for key, (path, mtime) in candidates.items():
        # Same screening order as git_repos: junk first (pure string), guard
        # before any syscall, then the one existence probe.
        if junk_path(path) or guard.blocks(path):
            continue
        try:
            if not os.path.isfile(path):
                continue
            if mtime is None:
                mtime = os.path.getmtime(path)
        except OSError:
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        canonical = canonical_fs_path(path)
        apps.append({
            "name": name,
            "tag": EXPORTED_TAG,
            "kind": "appfile",
            "path": canonical,
            "entry": canonical,
            "entry_html": None,
            "preview_image": None,
            "category": None,
            "title": None,
            "updated_at": mtime,
            "opened_at": opened_at.get(key),
        })
    return apps
