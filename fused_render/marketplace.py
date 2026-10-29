"""Backend for the Marketplace (the shell's /marketplace pages) — the
reviewed marketplace of docs/CONTRIB_LOOP.md §18 (stage B).

A browsable catalog of community apps, each a POINTER into its author's
GitHub repo: the Fused index repo (`index_repo()`, default
``fusedio/fused-apps-index``) holds one row per app — repo, branch and the
**approved commit** an automated review passed — and a backend canvas reads
the index and serves the catalog. Installing makes a real clone of the
author's repo AT the approved commit (stage A's installer, appfile_git.py),
recorded as a `marketplace` copy (contrib/store.py); from then on the
contribution engine keeps it on the row's approved commit, never the
author's tip (§18.0 M1). No app code comes from Fused storage any more
(M2): the release-package path (`marketplace_release`, the tar.gz download,
its extractor, the per-file stamp writer, the staging sweep) is deleted.
Deliberately thin: this module plus routers/marketplace.py and the shell
routes; git behaviour is
the engine's (contrib/market.py, contrib/blocks.py), never re-done here.

It is NOT community.py (the /apps hub's old Showcase tab, retired while the
Marketplace is on: routers/community.py answers `{status: "retired"}`).

**The API** is a set of GET endpoints under `api_base()` (the constant
`API_BASE`, overridable by `FUSED_RENDER_MARKETPLACE_API`, read per call).
Every request asks for `<endpoint>.json` (the public canvas path needs the
extension; backend_canvas.py) and every answer is a JSON list of rows;
an error is HTTP 200 with ONE row `{error_code, error_message}`. `_udf_get`
is the single seam: it builds that URL, goes through `_http_get` (never
raises) and raises `SourceError` — `error_code` kept — for a transport
failure, a non-list body or an error row. Endpoints (§18.3):
`marketplace_catalog` (one row per app: the listing fields plus `repo,
branch, commit, release_id (== commit), remix_of, remix_count,
contributor_count, last_release_at, releases [{commit, at, whats_new,
version, blocked}]` newest first), `marketplace_categories`,
`marketplace_collections` (the home shelves; `featured` = the curated
shelf), `marketplace_search`, `marketplace_app_detail` (readme, files,
releases) and `marketplace_blocklist` (B4). `client_version` is
`fused_render.__version__`.

**State dir** is ``~/.fused-render/marketplace/`` (FUSED_RENDER_HOME
honoured) — UNNESTED by branch ref, exactly like community.STATE_DIR: the
catalog, block list and asset cache describe a remote service, not this
checkout, so parallel branches share one copy. What IS per-setup is the
workspace the apps install into (`seed.fused_dir()`), read per call.

  catalog.json                 the last snapshot + fetched_at/checked_at/stale
  blocklist.json               the last block list + checked_at/stale/status
  assets/<release_id>/<slug>/  bytes served by /api/marketplace/asset
  acks.json                    the Python notices the user accepted

**Snapshot** = {source: "udf-api", api, catalog_version, generated_at,
apps: {slug: catalog row}, categories: [..], collections: [..], status:
"ok", stale, warning, fetched_at, checked_at} — one build makes three API
calls. `status == "ok"` and not `stale` is what contrib/market reads as
FRESH (only then may a missing slug mean "delisted", §18.4). A catalog.json written for another API (`api !=
api_base()`) or by the GitHub-backed code before it (no `source ==
"udf-api"`) is treated as no cache, so swapping `API_BASE` for the canvas's
share URL drops the old backend's rows by itself.

**Cache policy.** A catalog younger than 10 minutes (by `checked_at`, the
last ATTEMPT — so an offline machine backs off too instead of re-trying the
network on every page view, and a crashing build likewise) is served as-is
unless `refresh=1`; a `refresh=1` within 60 s of the last attempt is served
the cache too. A refresh that fails serves the old cache with `stale: true`
and a warning; with no cache at all the answer is `{status: "error",
message}`. Only GET /catalog (`get_snapshot`) — and the explicit Check
(`get_snapshot(force=True)`, the Installed page's "Check for a new release"
and the engine's check action) — rebuild an OLD cache; assets, detail,
install, installed and the engine's `market.row_for` (`cached_snapshot`)
take any cache regardless of age, without the build lock, and build only
when there is none — a rebuild never holds up an <img>. The block list
(`blocklist()`) has the same TTL and back-off in its own file, and offline
the last list it saw applies (§18.4 B4).

**Assets.** A row's icon/preview URLs are raw.githubusercontent.com URLs at
the app's approved commit; they are served through our own proxy
(`/api/marketplace/asset/<slug>/<rel>?v=<release_id[:12]>`) so the browser
never talks to GitHub and a readme's relative images resolve against the
same base. The proxy fetches only under the row's own base, `_asset_base`:
``https://raw.githubusercontent.com/<repo>/<commit>/`` (the app is the repo
root — no slug directory), derived from `repo` + `commit`, else parsed off
`source_url` (``https://github.com/<repo>``, or the old backend's
``…/tree/<commit>/<slug>`` form, whose base keeps the slug directory). And
only for a path that is the row's own icon or preview or one the app's
detail lists (memoised per (slug, release_id) in `_DETAIL_MEMO`, so the page
and the proxy share one API call).

**Installed copies** (`installed_copies()`) come from two places, keyed by
slug: `marketplace` records in the contribution store (`store.all()`, kind
`marketplace`, `record.slug`, folder present) — the clones this module
installs — and LEGACY stamps, `app/*/.fused/marketplace.json`, which the old
package installer wrote, for folders with no marketplace record AND no
`.git`. A stamped folder that IS a clone is asked its kind (`classify`):
`own` is the author's own copy ("Your app", `installed.kind == "own"`: no
switch, no update), `github` is a link copy (install's confirm/switch); for
both the stamp is dead bookkeeping and is removed. An `own` record whose
repo a listed row names is "Your app" too, stamp or not. A legacy
copy keeps working and never updates; once its slug is listed the Installed
page offers "Switch to the reviewed version" (POST /adopt →
`market.adopt_legacy`, which turns the folder into a marketplace clone in
place and deletes the stamp). A HUSK (a folder holding only `.fused/`, what
a keep-data uninstall leaves) is not installed.

**Install / update / switch** run as background jobs (one per slug,
idempotent while running; finished jobs linger 60 s for the UI's success
state), each through `plan_install` — the ONE answer to "what would
installing <slug> do here", which the router, the bots' card
(bots/tools._market_target) and the job all read:
  install  → `appfile_git.clone(url, repo, commit=row.commit)` into the
             link installer's staging dir, packed into a `.fused` exactly
             like a GitHub link (appfetch.download_github_app), then
             `appfile_git.install(…, kind="marketplace", market={slug,
             approved})`. The folder is the link installer's, so it is
             named after the REPO (`app/<repo>`), not the slug: every
             "where is <slug>" answer comes from records/stamps.
  update   → a marketplace copy exists: `contrib.engine.bring_in(path)`,
             which runs the engine's release move (the record's approved
             commit moved; Activity line + Undo).
  switch   → a `github` (link) copy of the same repo exists: the first
             call answers `{status: "confirm", switch: "reviewed", path,
             who}` and nothing runs; called again with `switch=True` the
             job runs `contrib.market.switch_to_reviewed(path, row)`
             (D13: changing the trust level is the user's explicit choice).
  refuse   → a legacy copy ("Switch to the reviewed version first"), the
             user's own repo, an unrecorded clone, a row without a
             reviewed source (the old backend's rows have no `repo`).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:  # POSIX only; the acks lock below is advisory and degrades to the thread lock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlencode

import httpx

from fused_render import backend_canvas

logger = logging.getLogger(__name__)

#: The backend's base URL: the `fused_app_marketplace` canvas (§18.3),
#: shared from Fused's `unstable` environment for now (the owner moves it to
#: prod with the index repo later). FUSED_RENDER_MARKETPLACE_API overrides
#: it, read per call. Rows without a `repo` (an older backend) browse but do
#: not install (`plan_install` refuses with a sentence).
API_BASE = backend_canvas.API_BASE  # the one backend canvas (backend_canvas.py)
#: The Fused index repo the backend reads (§18.1 "Index"); the catalog
#: footer links it. One of the only two places that know the name (the
#: backend canvas holds the other); FUSED_RENDER_MARKETPLACE_INDEX overrides.
INDEX_REPO = "fusedio/fused-apps-index"
RAW_BASE = "https://raw.githubusercontent.com"
SOURCE = "udf-api"
#: How many "new and updated" apps the home shelf asks the API for.
NEW_AND_UPDATED_LIMIT = 12

#: A cached catalog (or block list) younger than this (since the last
#: attempt) is served as-is.
CACHE_TTL_S = 10 * 60
#: A forced `refresh=1` within this long of the last attempt serves the cache
#: (a reload-hammering user must not hammer the API).
REFRESH_MIN_INTERVAL_S = 60
#: Finished jobs stay in /jobs this long, so the UI can show "done"/"error".
JOB_TTL_S = 60

HTTP_TIMEOUT_S = 20
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_README_BYTES = 512 * 1024

#: The LEGACY install stamp (the old package installer's): read, never written.
STAMP_NAME = "marketplace.json"
BLOCKLIST_NAME = "blocklist.json"

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: `<owner>/<repo>` as GitHub spells them (the shape appfile_git._URL_RE takes).
_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
#: A release id becomes a cache directory name: no separators, no dots-only.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
#: The old backend's `source_url` (a folder of the community-apps repo).
_SOURCE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/tree/([0-9a-f]{40,64})/([a-z0-9-]+)/?")
#: §18.3's `source_url`: the app's repo (the app at its root).
_REPO_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")
_BLOCK_LEVELS = ("hard", "soft")


class SourceError(Exception):
    """The API could not answer; the message is user-facing. ``error_code`` is
    the API's own code (e.g. ``app_not_found``) when it sent one."""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class InstallError(Exception):
    """An install/update/switch failed or cannot start; the message is shown
    on the job (or the bot's result) verbatim."""


# ---------------------------------------------------------------------------
# configuration (read per call — tests and env overrides take effect live)
# ---------------------------------------------------------------------------

def state_dir() -> str:
    """``~/.fused-render/marketplace`` — unnested by branch, like
    community.STATE_DIR (module docstring)."""
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "marketplace")


def catalog_path() -> str:
    return os.path.join(state_dir(), "catalog.json")


def blocklist_path() -> str:
    return os.path.join(state_dir(), BLOCKLIST_NAME)


def install_root() -> str:
    """``<workspace>/app`` — the tag folder every installed app lands in (the
    `.fused` installer's, the GitHub link installer's, and so the
    Marketplace's: `plan_install`'s new copies are link-installer clones)."""
    from fused_render.appfile import install_dir

    return install_dir()


def api_base() -> str:
    """The API's base URL (no trailing slash); `FUSED_RENDER_MARKETPLACE_API`
    overrides the constant, read per call (backend_canvas.api_base)."""
    return backend_canvas.api_base()


def index_repo() -> str:
    """`<owner>/<repo>` of the Fused index (§18.1); `FUSED_RENDER_MARKETPLACE_
    INDEX` overrides the constant, read per call. A malformed override reads
    as the constant — it ends up in a link on the page."""
    v = (os.environ.get("FUSED_RENDER_MARKETPLACE_INDEX") or "").strip().strip("/")
    return v if _REPO_RE.fullmatch(v) else INDEX_REPO


def is_slug(slug) -> bool:
    # The index's row-file name rule (§18.2); also what keeps a crafted slug
    # from ever forming a path outside the cache.
    return isinstance(slug, str) and bool(_SLUG_RE.fullmatch(slug))


def is_sha(v) -> bool:
    """A full git object id (sha1 or sha256)."""
    return isinstance(v, str) and bool(_SHA_RE.fullmatch(v))


def is_repo(v) -> bool:
    """`<owner>/<repo>` as GitHub spells them."""
    return isinstance(v, str) and bool(_REPO_RE.fullmatch(v)) and not v.endswith((".", ".git"))


def _safe_rel(path) -> bool:
    """A file path we are willing to serve: relative, forward slashes, no
    empty/`.`/`..` segment, no NUL or backslash."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    if "\\" in path or "\x00" in path:
        return False
    return all(seg not in ("", ".", "..") for seg in path.split("/"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _version_tuple(v) -> tuple | None:
    """`1.2.3` (any pre-release/build suffix ignored) → (1, 2, 3); None when
    there is no leading number to compare."""
    if not isinstance(v, str):
        return None
    m = re.match(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v)
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def _compatible(min_version) -> bool:
    from fused_render import __version__

    need = _version_tuple(min_version)
    have = _version_tuple(__version__)
    return need is None or have is None or have >= need


# ---------------------------------------------------------------------------
# HTTP (never raises) and the API seam
# ---------------------------------------------------------------------------

def _http_get(url: str, *, headers: dict | None = None,
              timeout: float = HTTP_TIMEOUT_S) -> tuple[httpx.Response | None, str | None]:
    """One GET: (response, error). Never raises — like hub_models._get, an
    unreachable service is a sentence on the page, not a 500 from this
    server."""
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        return None, f"could not reach the Community ({e.__class__.__name__})"
    if r.status_code == 429:
        return None, "the Community is rate-limiting requests"
    if r.status_code == 404:
        return None, "not found (404)"
    if r.status_code >= 400:
        return None, f"the Community answered {r.status_code}"
    return r, None


def _udf_get(endpoint: str, *, timeout: float = HTTP_TIMEOUT_S, **params) -> list[dict]:
    """GET `<api>/<endpoint>.json?...` → the list of row dicts.
    `timeout` (seconds) bounds the request; it is never sent as a parameter.
    Params that are None are dropped (an empty string is kept). Raises
    SourceError on a transport failure, a body that is not a list of objects,
    or an error row (HTTP 200, one row with `error_code`). An empty list is
    a valid answer (an empty block list). A self-describing object (`{...,
    "entries": [...]}`, the block list's newer shape) is answered as its one
    row; any other object is unexpected."""
    from fused_render import __version__

    query = {k: v for k, v in params.items() if v is not None}
    url = f"{api_base()}/{endpoint}.json" + (f"?{urlencode(query)}" if query else "")
    r, err = _http_get(url, headers={"Accept": "application/json",
                                     "User-Agent": f"fused-render/{__version__}"},
                       timeout=timeout)
    if err:
        raise SourceError(err)
    try:
        rows = r.json()
    except ValueError:
        raise SourceError("the Community answered something unexpected")
    if isinstance(rows, dict) and isinstance(rows.get("entries"), list):
        rows = [rows]
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        raise SourceError("the Community answered something unexpected")
    if len(rows) == 1 and rows[0].get("error_code"):
        code = str(rows[0]["error_code"])
        raise SourceError(str(rows[0].get("error_message") or code), code)
    return rows


# ---------------------------------------------------------------------------
# API → snapshot
# ---------------------------------------------------------------------------

def _parse_metadata(raw: bytes | str) -> dict | None:
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return meta if isinstance(meta, dict) else None


def _group_collections(rows: list[dict], apps: dict) -> list[dict]:
    """Collection rows → [{id, kind, title, slugs}] grouped by (collection,
    shelf_id), groups in order of first appearance, slugs by `position`,
    slugs the catalog does not hold dropped."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        kind = r.get("collection")
        if kind not in ("shelf", "new_and_updated"):
            continue
        if kind == "shelf" and not r.get("shelf_id"):
            continue  # an unnamed shelf has no id to key, link or order by
        key = (kind, r.get("shelf_id"))
        g = groups.setdefault(key, {
            "id": (r.get("shelf_id") if kind == "shelf" else None) or "new-and-updated",
            "kind": kind, "title": _opt_str(r.get("title")) or "", "_rows": []})
        slug = r.get("slug")
        if slug in apps:
            pos = r.get("position")
            g["_rows"].append((pos if isinstance(pos, (int, float)) else 0, slug))
    out = []
    for g in groups.values():
        rows_sorted = sorted(g.pop("_rows"), key=lambda t: t[0])  # stable: ties keep API order
        out.append({**g, "slugs": [s for _p, s in rows_sorted]})
    return out


def _opt_text(v) -> str | None:
    """`str(v)` for anything the API sent, None when it sent nothing."""
    return None if v is None else str(v)


def _count(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _build_snapshot() -> dict:
    """A fresh snapshot from the API (three calls, in parallel). Raises
    SourceError when any of them fails."""
    from fused_render import __version__

    with ThreadPoolExecutor(3) as pool:
        f_cat = pool.submit(_udf_get, "marketplace_catalog", client_version=__version__)
        f_cats = pool.submit(_udf_get, "marketplace_categories")
        f_cols = pool.submit(_udf_get, "marketplace_collections",
                             new_and_updated_limit=NEW_AND_UPDATED_LIMIT)
        rows, cats, cols = f_cat.result(), f_cats.result(), f_cols.result()
    apps = {r["slug"]: r for r in rows if is_slug(r.get("slug"))}
    first = rows[0] if rows else {}
    cats = [c for c in cats if isinstance(c, dict)]
    categories = [{"id": c["id"], "label": _opt_str(c.get("label")) or str(c["id"]),
                   "count": _count(c.get("count")), "order": c.get("order")}
                  for c in sorted(cats, key=lambda c: c.get("order") if isinstance(c.get("order"), (int, float)) else 0)
                  if isinstance(c.get("id"), str) and c["id"]]
    return {"source": SOURCE, "api": api_base(),
            "catalog_version": _opt_text(first.get("catalog_version")),
            "generated_at": _opt_text(first.get("generated_at")),
            "apps": apps, "categories": categories,
            "collections": _group_collections(cols, apps),
            # The backend answered from its last copy of the index (GitHub
            # unreadable): never "fresh" here (`_fresh`), so a slug missing
            # from it is unknown, not delisted.
            "backend_stale": any(bool(r.get("stale")) for r in rows)}


def _implausible(snap: dict, cached: dict | None) -> str | None:
    """Why a freshly built catalog cannot be taken as the index's current
    answer yet, or None: no apps at all, or more than half of the apps the
    cache holds missing at once. Either is how an unreadable or truncated
    index looks; trusting it would mark every copy of the missing apps
    delisted. The same answer twice in a row (`suspect`, kept on the cache
    by the first) is believed: a real mass delisting reads that way too."""
    apps = sorted(snap.get("apps") or {})
    known = set((cached or {}).get("apps") or {})
    if (cached or {}).get("suspect") == apps:
        return None
    if not apps and (known or cached is None):
        return "the Community answered an empty catalog"
    if known and len(known - set(apps)) * 2 > len(known):
        return "the Community's catalog is missing most of the apps it had"
    return None


# ---------------------------------------------------------------------------
# catalog cache
# ---------------------------------------------------------------------------

#: Held only while a catalog is being BUILT. Never waited on while a cache
#: exists: readers (assets, detail, install, installed) read catalog.json
#: without it, and a /catalog request that finds a rebuild already running
#: serves the cache instead of queueing behind it — a slow build must not
#: soak up FastAPI's threadpool.
_CATALOG_LOCK = threading.Lock()


def _read_cache() -> dict | None:
    try:
        with open(catalog_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        return None
    if data.get("source") != SOURCE or data.get("api") != api_base():
        return None  # an older backend's cache, or one for a different API
    return data


def _write_cache(snap: dict) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    # Unique per writer: other worktrees' servers share this state dir.
    tmp = f"{catalog_path()}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, catalog_path())
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _release_ids(snap: dict) -> set[str]:
    return {k for k in (_release_key(r) for r in snap.get("apps", {}).values()
                        if isinstance(r, dict)) if k}


def _prune_asset_cache(keep: set[str]) -> None:
    """Drop `assets/<release_id>` directories no app in the catalog is at."""
    root = os.path.join(state_dir(), "assets")
    try:
        names = os.listdir(root)
    except OSError:
        return
    for n in names:
        if n not in keep:
            shutil.rmtree(os.path.join(root, n), ignore_errors=True)


def _age(cached: dict, now: float) -> float:
    try:
        return now - float(cached.get("checked_at") or 0)
    except (TypeError, ValueError):
        return float("inf")


def _fresh_enough(cached: dict | None, refresh: bool, now: float) -> bool:
    if cached is None:
        return False
    return _age(cached, now) < (REFRESH_MIN_INTERVAL_S if refresh else CACHE_TTL_S)


def _mark_failed(cached: dict, now: float, warning: str | None) -> dict:
    """The old cache, flagged stale and stamped as just-checked (so the next
    requests back off for a TTL instead of each retrying a failing build)."""
    cached = dict(cached)
    cached["stale"] = True
    cached["checked_at"] = now
    if warning:
        cached["warning"] = warning
    try:
        _write_cache(cached)
    except OSError:
        pass
    return cached


def _build_locked(cached: dict | None) -> tuple[dict | None, str | None]:
    """Build, store and answer a snapshot (caller holds _CATALOG_LOCK);
    ``cached`` is what to fall back to. Never raises."""
    now = time.time()
    try:
        snap = _build_snapshot()
    except SourceError as exc:
        msg = str(exc)
        logger.warning("marketplace: catalog refresh failed: %s", msg)
        if cached:
            return _mark_failed(cached, now, f"Could not refresh the catalog ({msg}) — "
                                             f"showing apps cached at {cached.get('fetched_at')}"), None
        return None, f"Could not load the community catalog: {msg}"
    except Exception as exc:  # noqa: BLE001 — a bug must not take the page down
        logger.exception("marketplace: catalog build crashed")
        if cached:
            return _mark_failed(cached, now, None), None
        return None, f"Could not load the community catalog: {exc.__class__.__name__}"
    # The backend answered from its last copy of the index, or an answer
    # that cannot be the index's current one (`_implausible`): never fresh
    # here. With a cache, the last good build stands, flagged stale (an
    # implausible answer is remembered as `suspect`: the next build that
    # answers the same apps is believed). Without one, the answer is kept,
    # flagged stale.
    older = bool(snap.pop("backend_stale", False))
    odd = ("the Community answered from an older copy of its catalog" if older
           else _implausible(snap, cached))
    if odd and cached:
        logger.warning("marketplace: catalog answer not trusted (%s)", odd)
        suspect = {} if older else {"suspect": sorted(snap.get("apps") or {})}
        return _mark_failed({**cached, **suspect}, now,
                            f"Could not refresh the catalog ({odd}) — showing apps "
                            f"cached at {cached.get('fetched_at')}"), None
    snap["fetched_at"] = _utc_now()
    snap["checked_at"] = now
    snap["stale"] = bool(odd)
    # The backend's current answer (module docstring "Snapshot"); a failed
    # refresh keeps it but `_mark_failed` sets `stale`, so a fallback never
    # reads as fresh.
    snap["status"] = "ok"
    snap["warning"] = f"{odd[:1].upper()}{odd[1:]}." if odd else None
    try:
        _write_cache(snap)
    except OSError:
        logger.warning("marketplace: could not write %s", catalog_path(), exc_info=True)
    if (not cached or snap.get("catalog_version") is None
            or cached.get("catalog_version") != snap.get("catalog_version")):
        _prune_asset_cache(_release_ids(snap))
    return snap, None


#: How long `force` waits for a build already in flight (`check`).
FORCE_WAIT_S = 30.0


def get_snapshot(refresh: bool = False, force: bool = False) -> tuple[dict | None, str | None]:
    """The catalog snapshot for GET /catalog — the ONLY caller allowed to
    rebuild a cache because it is old: rebuilt when older than CACHE_TTL_S,
    or on ``refresh`` when the last attempt is over REFRESH_MIN_INTERVAL_S
    old (``force`` skips both age checks: a rebuild now, for a caller that
    just learned the cache is wrong). While another request is already rebuilding, the existing cache is
    answered at once rather than waiting. Answers (snapshot, None) or, when
    there is no cache at all and the build failed, (None, message). Never
    raises. Everything else uses `cached_snapshot()`."""
    cached = _read_cache()
    if not force and _fresh_enough(cached, refresh, time.time()):
        return cached, None
    if cached is not None and not force:
        if not _CATALOG_LOCK.acquire(blocking=False):
            return cached, None  # a rebuild is in flight; it will land shortly
    elif cached is not None:
        # `force` is a caller that must not answer from the old cache (the
        # "Check for a new release" button): wait for a build in flight,
        # bounded, then build again — what it built may predate the ask.
        if not _CATALOG_LOCK.acquire(timeout=FORCE_WAIT_S):
            stale = dict(cached)
            stale["stale"] = True
            return stale, None
    else:
        _CATALOG_LOCK.acquire()  # nothing to serve without a build anyway
    try:
        # Re-read: the build we may have waited behind just stored one.
        cached = _read_cache()
        if not force and _fresh_enough(cached, refresh, time.time()):
            return cached, None
        return _build_locked(cached)
    finally:
        _CATALOG_LOCK.release()


def cached_snapshot() -> tuple[dict | None, str | None]:
    """The catalog for everything but GET /catalog (assets, detail, install,
    installed): whatever is cached, REGARDLESS of age, read without the
    build lock — so thirty parallel <img> requests after the TTL never queue
    behind a rebuild. Builds only when there is no cache at all."""
    cached = _read_cache()
    if cached is not None:
        return cached, None
    with _CATALOG_LOCK:
        cached = _read_cache()
        if cached is not None:
            return cached, None
        return _build_locked(None)


# ---------------------------------------------------------------------------
# installed copies
# ---------------------------------------------------------------------------

def stamp_path(app_dir: str) -> str:
    """A LEGACY copy's stamp (module docstring): read, never written."""
    return os.path.join(app_dir, ".fused", STAMP_NAME)


def read_stamp(app_dir: str) -> dict | None:
    try:
        with open(stamp_path(app_dir), encoding="utf-8") as f:
            stamp = json.load(f)
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


#: (path, mtime_ns, size) → sha256, so a legacy copy's `modified` on every
#: catalog read only re-hashes files that actually changed.
_HASH_MEMO: dict[tuple, str] = {}
_HASH_MEMO_LOCK = threading.Lock()


def _sha256_file(path: str) -> str:
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    with _HASH_MEMO_LOCK:
        hit = _HASH_MEMO.get(key)
    if hit:
        return hit
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with _HASH_MEMO_LOCK:
        if len(_HASH_MEMO) > 20000:
            _HASH_MEMO.clear()
        _HASH_MEMO[key] = digest
    return digest


def _modified(app_dir: str, stamp: dict) -> bool:
    """Any stamped file whose bytes no longer match (or that is gone)."""
    files = stamp.get("files")
    if not isinstance(files, dict):
        return False
    for rel, digest in files.items():
        if not _safe_rel(rel):
            continue
        p = os.path.join(app_dir, *rel.split("/"))
        try:
            if os.path.islink(p) or not os.path.isfile(p) or _sha256_file(p) != digest:
                return True
        except OSError:
            return True
    return False


def _is_husk(app_dir: str) -> bool:
    """A folder holding nothing but `.fused/` (what a keep-data uninstall
    leaves): the place the app's data waits for the next install."""
    try:
        names = [n for n in os.listdir(app_dir) if n != ".DS_Store"]
    except OSError:
        return False
    return names == [".fused"]


def installed_apps() -> dict[str, dict]:
    """slug → {path, stamp} for every LEGACY stamp, `app/*/.fused/
    marketplace.json`, whose folder still holds the app (a husk is not
    installed). Raw: `installed_copies` drops the stamps of folders that are
    marketplace clones now (a husk's stale stamp carried into a clone, an
    adopt that crashed before deleting it).

    Keyed by the row it belongs to, never by a name: the index lists apps by
    their `fused-app-id` (owner 2026-10-27), while an old package's stamp
    carries the old store's name slug. The copy's own entry-page id wins
    when the cached catalog lists it; else the stamp's slug; the folder
    name only as a last resort for a stamp that names nothing (it then
    matches no row: "no longer in the Community")."""
    from fused_render import app_id as app_ids

    root = install_root()
    out: dict[str, dict] = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    listed = set(((_read_cache() or {}).get("apps") or {}))
    for name in names:
        if name.startswith("."):
            continue
        app_dir = os.path.join(root, name)
        stamp = read_stamp(app_dir)
        if stamp is None or _is_husk(app_dir):
            continue
        try:
            entry = _entry_of(app_dir)
        except OSError:
            entry = None
        aid = app_ids.app_id(entry) if entry else None
        stamped = stamp.get("slug") if is_slug(stamp.get("slug")) else None
        if aid and is_slug(aid) and (aid in listed or not stamped):
            slug = aid
        else:
            slug = stamped or name
        out.setdefault(slug, {"path": app_dir, "stamp": stamp})
    return out


def _entry_of(app_dir: str) -> str | None:
    """The app's entry page (`app_listing.app_entry`), a seam for tests."""
    from fused_render import app_listing

    return app_listing.app_entry(app_dir)


def _store_key(path: str) -> str | None:
    try:
        from fused_render.contrib import store

        return store.key(path)
    except Exception:  # noqa: BLE001 — no store: no record can name it
        return None


def marketplace_records() -> dict[str, dict]:
    """slug → {path, record} for every `marketplace` record in the
    contribution store whose folder is still there (store.all() keeps
    orphans for adoption; a folder that is gone is not installed). Two
    copies of one slug: the most recently classified wins. Records are raw
    (`store.all()` does no `_view`): every new §18.4 key is `.get`."""
    try:
        from fused_render.contrib import store

        rows = store.all()
    except Exception:  # noqa: BLE001 — an unreadable store lists nothing
        logger.debug("marketplace: contrib store unreadable", exc_info=True)
        return {}
    out: dict[str, tuple] = {}
    for k, rec in rows.items():
        if not isinstance(rec, dict) or rec.get("kind") != "marketplace":
            continue
        slug = rec.get("slug")
        if not is_slug(slug) or not os.path.isdir(k):
            continue
        at = rec.get("classified_at") if isinstance(rec.get("classified_at"), (int, float)) else 0
        if slug not in out or at > out[slug][0]:
            out[slug] = (at, {"path": k, "record": rec})
    return {slug: v for slug, (_at, v) in out.items()}


def _clone_kind(path: str) -> str | None:
    """The contribution loop's kind of the clone at `path`: the record's
    (`classify.kind_of`, a store read), else a first classification
    (`classify.ensure`, which may ask GitHub who the user is once — cached by
    gh.py; it answers None while that cannot be told). None = unknown."""
    try:
        from fused_render.contrib import classify

        kind = classify.kind_of(path)
        if kind is None:
            rec = classify.ensure(path)
            kind = rec.get("kind") if rec else None
        return kind
    except Exception:  # noqa: BLE001 — unknown, never "legacy"
        logger.debug("marketplace: could not classify %s", path, exc_info=True)
        return None


def _drop_dead_stamp(path: str) -> None:
    """A legacy stamp in a folder that is a git clone of the app's repo (the
    author's own clone, or a GitHub-link copy) is DEAD bookkeeping: the old
    package installer merged its files into a folder that was already a
    clone, and the clone — not the stamp — is what the copy is now. Left
    there it reads as a legacy install and offers "Switch to the reviewed
    version" (adopt) on a repo, so it goes. Best-effort."""
    with contextlib.suppress(OSError):
        os.remove(stamp_path(path))
        logger.info("marketplace: removed a stale legacy stamp from the clone %s", path)


def _own_records() -> dict[str, dict]:
    """`repo (lowercased) → {path, record}` for every `own` record whose
    folder is there — the author's own copies of listed apps."""
    try:
        from fused_render.contrib import store

        rows = store.all()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for k, rec in rows.items():
        if (isinstance(rec, dict) and rec.get("kind") == "own" and isinstance(rec.get("repo"), str)
                and os.path.isdir(k)):
            out.setdefault(rec["repo"].lower(), {"path": k, "record": rec})
    return out


def installed_copies() -> dict[str, dict]:
    """slug → the copy of that app in the workspace (module docstring
    "Installed copies"):
      {"path", "kind": "marketplace", "record"}  a marketplace clone;
      {"path", "kind": "legacy", "stamp"}        an old package install —
          the stamp AND no `.git` (a folder with a repo is never legacy);
      {"path", "kind": "own", "record"}          the AUTHOR's own copy of a
          listed app ("Your app": no switch, no update — it follows her own
          pushes), found by a stamped clone that classifies `own` or by an
          `own` record whose repo a cached catalog row names.
    A stamped clone that classifies `github` is not listed here: installing
    from the Marketplace reaches it through `plan_install`'s confirm/switch.
    A record beats a stamp for the same slug or folder."""
    out: dict[str, dict] = {s: {**v, "kind": "marketplace"} for s, v in marketplace_records().items()}
    clones = {_store_key(v["path"]) for v in out.values()}
    for slug, rec in installed_apps().items():
        if slug in out or _store_key(rec["path"]) in clones:
            continue
        if not os.path.lexists(os.path.join(rec["path"], ".git")):
            out[slug] = {**rec, "kind": "legacy"}
            continue
        kind = _clone_kind(rec["path"])
        if kind in ("own", "github"):
            _drop_dead_stamp(rec["path"])
        if kind == "own":
            out[slug] = {"path": rec["path"], "kind": "own", "record": None}
        # marketplace: its record is listed above (or binds on the next read);
        # github / unknown: not an installed Marketplace copy.
    owned = _own_records()
    if owned:
        apps = (_read_cache() or {}).get("apps") or {}
        for slug, row in apps.items():
            repo = row_repo(row) if isinstance(row, dict) else None
            hit = owned.get(repo.lower()) if repo else None
            if hit and slug not in out:
                out[slug] = {**hit, "kind": "own"}
    return out


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _block_view(b) -> dict | None:
    """A record's `block` (§18.4) for the page: the notice's words and state,
    None when there is none."""
    if not isinstance(b, dict) or b.get("level") not in _BLOCK_LEVELS:
        return None
    return {"level": b["level"], "commit": _opt_str(b.get("commit")),
            "reason": _opt_str(b.get("reason")) or "", "what_to_do": _opt_str(b.get("what_to_do")) or "",
            "at": _opt_str(b.get("at")), "rolled_back_to": _opt_str(b.get("rolled_back_to")),
            # The rollback target's release date, for "Rolled back to the
            # release of <date>" (the engine writes it when it rolls back).
            "rolled_back_to_at": _opt_str(b.get("rolled_back_to_at")),
            "conflict": b.get("conflict") is True,
            "confirmed": isinstance(b.get("confirmed_at"), (int, float))}


def _installed_info(copy: dict | None, row: dict | None, *, catalog_fresh: bool = False) -> dict | None:
    """The AppEntry's `installed` (§18.5): `{path, kind, commit, version,
    installed_at, update_available, modified, delisted, block}`. `row` is
    the catalog row (None: the catalog does not list the slug)."""
    if not copy:
        return None
    path = copy["path"].replace(os.sep, "/")
    if copy["kind"] == "marketplace":
        rec = copy["record"]
        approved = _dict(rec.get("approved"))
        commit = approved.get("commit") if is_sha(approved.get("commit")) else None
        listed = _dict(rec.get("listing")).get("status")
        return {
            "path": path, "kind": "marketplace", "commit": commit,
            "version": _opt_str(approved.get("version")),
            "installed_at": None,
            # The record's approved commit is what the copy runs; the row's
            # is the newest release. They differ until the next check moves
            # the copy (engine.refresh_approved + the release move).
            "update_available": bool(row is not None and commit and is_sha(row.get("commit"))
                                     and row["commit"] != commit),
            "modified": None,
            # A listed row is the truth; otherwise the engine's last reading,
            # or the catalog's own (fresh and without the slug).
            "delisted": row is None and (listed == "delisted" or catalog_fresh),
            "block": _block_view(rec.get("block")),
        }
    if copy["kind"] == "own":
        # The author's own copy ("Your app"): it follows her own pushes, so
        # there is nothing to update to and nothing to switch.
        return {"path": path, "kind": "own", "commit": None, "version": None, "installed_at": None,
                "update_available": False, "modified": None, "delisted": row is None, "block": None}
    stamp = copy["stamp"]
    return {
        "path": path, "kind": "legacy",
        "commit": stamp.get("commit") if is_sha(stamp.get("commit")) else None,
        "version": _opt_str(stamp.get("version")),
        "installed_at": _opt_str(stamp.get("installed_at")),
        # A legacy copy never updates: it offers the switch (adopt) instead.
        "update_available": False,
        "modified": _modified(copy["path"], stamp),
        # Unlisted → the Installed row says "No longer updated".
        "delisted": row is None,
        "block": None,
    }


# ---------------------------------------------------------------------------
# AppEntry
# ---------------------------------------------------------------------------

def _str_list(v) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _opt_str(v) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _source_url(row: dict) -> str | None:
    """The row's `source_url` for the page's "View source" link — ONLY a
    `https://github.com/` page (the row comes from the backend canvas and
    the index; a `javascript:` or any other scheme there would be a live
    link on the page), else one built from the row's repo, else None."""
    v = _opt_str(row.get("source_url"))
    if v and v.startswith("https://github.com/") and not any(c in v for c in "\"'<> \t\r\n"):
        return v
    repo = row_repo(row)
    return f"https://github.com/{repo}" if repo else None


def _author(v) -> dict:
    if isinstance(v, str):
        return {"name": v, "github": None}
    if isinstance(v, dict):
        return {"name": _opt_str(v.get("name")), "github": _opt_str(v.get("github"))}
    return {"name": None, "github": None}


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _release_key(row: dict) -> str | None:
    """The row's release id (§18.3: == its approved commit), else its commit
    — the asset cache's directory name and the `?v=` key, so shape-checked."""
    for k in ("release_id", "commit"):
        v = row.get(k)
        if isinstance(v, str) and _ID_RE.fullmatch(v):
            return v
    return None


def _releases(v) -> list[dict]:
    """§18.3 `releases`, newest first: `[{commit, at, whats_new, version,
    blocked: "hard"|"soft"|None}]`; entries without a commit are dropped."""
    out = []
    for r in v if isinstance(v, list) else []:
        if not isinstance(r, dict) or not is_sha(r.get("commit")):
            continue
        out.append({"commit": r["commit"], "at": _opt_str(r.get("at")),
                    "whats_new": _opt_str(r.get("whats_new")), "version": _opt_str(r.get("version")),
                    "blocked": r.get("blocked") if r.get("blocked") in _BLOCK_LEVELS else None})
    return out


def _current(row: dict, releases: list[dict], key: str, *, top: bool = True, newest: bool = True):
    """`key` of the row's CURRENT commit: the row's own top-level value
    (`top`; the backend serves `whats_new`/`blocked` for it), else the release
    entry of `row.commit`, else (`newest`) the newest release's."""
    if top and _opt_str(row.get(key)):
        return row[key]
    rel = next((r for r in releases if r["commit"] == row.get("commit")), None)
    if rel is not None:
        return rel.get(key)
    return releases[0].get(key) if newest and releases else None


def row_repo(row: dict) -> str | None:
    """`<owner>/<repo>` of a catalog row: its `repo`, else its `source_url`
    (§18.3: ``https://github.com/<repo>``). None for the old backend's rows
    (a folder of the community-apps repo is not an app's own repo)."""
    if is_repo(row.get("repo")):
        return row["repo"]
    m = _REPO_URL_RE.fullmatch(row.get("source_url") or "")
    return m.group(1) if m and is_repo(m.group(1)) else None


def _asset_base(slug: str, row: dict) -> str | None:
    """The only place the asset proxy ever fetches from (module docstring):
    `https://raw.githubusercontent.com/<repo>/<commit>/` — off `repo` (or
    `source_url`) + `commit`; the old backend's `…/tree/<commit>/<slug>`
    source keeps its slug directory; failing both, the prefix of the row's
    own icon/preview URL. None when the row names no GitHub commit."""
    repo, commit = row_repo(row), row.get("commit")
    if repo and is_sha(commit):
        return f"{RAW_BASE}/{repo}/{commit}/"
    m = _SOURCE_URL_RE.fullmatch(row.get("source_url") or "")
    if m and m.group(3) == slug:
        return f"{RAW_BASE}/{m.group(1)}/{m.group(2)}/{slug}/"
    for key in ("icon_url", "preview_url"):
        m = re.match(rf"({re.escape(RAW_BASE)}/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9a-f]{{40,64}}/)",
                     row.get(key) or "")
        if m:
            return m.group(1)
    return None


def _asset_version(row: dict) -> str | None:
    """The `?v=` cache key catalog asset URLs carry: the release id,
    shortened (a new release is new content, so a new URL)."""
    rid = _release_key(row)
    return rid[:12] if rid else None


def _asset_rel(slug: str, row: dict, url) -> str | None:
    """The path of a row's raw GitHub icon/preview `url` under the app's own
    base; None when the row has none or it is not under that base. A bare
    relative path (§18.2's row `icon: "icon.png"`) is taken as-is."""
    if not isinstance(url, str) or not url:
        return None
    if not url.startswith(("https://", "http://")):
        rel = url
    else:
        base = _asset_base(slug, row)
        if not base or not url.startswith(base):
            return None
        rel = url[len(base):]
    rel = unquote(rel.split("?", 1)[0].split("#", 1)[0])
    return rel if _safe_rel(rel) else None


def _proxied(slug: str, row: dict, url) -> str | None:
    """Our /api/marketplace/asset URL for a row's raw GitHub icon/preview URL;
    None when the row has none or it is not under the app's own base."""
    rel = _asset_rel(slug, row, url)
    if rel is None or _asset_base(slug, row) is None:
        return None
    ver = _asset_version(row)
    return f"/api/marketplace/asset/{slug}/{quote(rel, safe='/')}" + (f"?v={ver}" if ver else "")


def app_entry(slug: str, row: dict, installed: dict | None, *, catalog_fresh: bool = False,
              listed: bool = True) -> dict:
    """One AppEntry (the contract's shape, §18.5) out of an API catalog row.
    `installed` is `installed_copies()`'s entry; `listed` False for a copy
    the catalog does not hold (`_local_entry`)."""
    inst = _installed_info(installed, row if listed else None, catalog_fresh=catalog_fresh)
    releases = _releases(row.get("releases"))
    return {
        "slug": slug,
        "name": _opt_str(row.get("name")) or slug,
        "description": row.get("description") if isinstance(row.get("description"), str) else "",
        "author": _author(row.get("author")),
        "tags": _str_list(row.get("tags")),
        "category": _opt_str(row.get("category")) or "other",
        "version": _opt_str(row.get("version")) or "0.0.0",
        "requires_python": bool(row.get("requires_python")),
        "min_fused_render": _opt_str(row.get("min_fused_render")),
        "ai_capabilities": _str_list(row.get("ai_capabilities")),
        "ai_models": _str_list(row.get("ai_models")),
        "ai_model_param": _opt_str(row.get("ai_model_param")),
        "icon_url": _proxied(slug, row, row.get("icon_url")),
        "preview_url": _proxied(slug, row, row.get("preview_url")),
        "size_bytes": _int(row.get("size_bytes")),
        "file_count": _int(row.get("file_count")),
        "release_id": _opt_str(row.get("release_id")) or _opt_str(row.get("commit")),
        "commit": _opt_str(row.get("commit")),
        "repo": row_repo(row),
        "branch": _opt_str(row.get("branch")),
        "published_at": _opt_str(row.get("published_at")),
        "updated_at": _opt_str(row.get("updated_at")),
        "last_release_at": _opt_str(row.get("last_release_at")) or _opt_str(row.get("updated_at")),
        "source_url": _source_url(row),
        "featured": row.get("featured") is True,
        "remix_of": row.get("remix_of") if is_slug(row.get("remix_of")) else None,
        "remix_count": _int(row.get("remix_count")),
        "contributor_count": _int(row.get("contributor_count")),
        # §18.12: installs the marketplace counted (fused_app_install asks).
        "install_count": _int(row.get("installs")),
        "releases": releases,
        # The CURRENT commit's (the backend's top-level keys; after a
        # rollback releases[0] is not the current commit), else its release
        # entry's, else the newest's.
        "whats_new": _current(row, releases, "whats_new"),
        "blocked": row.get("blocked") if row.get("blocked") in _BLOCK_LEVELS
        else _current(row, releases, "blocked", top=False, newest=False),
        # A row the backend could not pin to a repo + commit browses but
        # does not install (`plan_install`).
        "installable": bool(row_repo(row) and is_sha(row.get("commit"))),
        "installed": inst,
        "update_available": bool(inst and inst["update_available"]),
        "compatible": _compatible(row.get("min_fused_render")),
    }


def _fresh(snap: dict | None) -> bool:
    """A snapshot that is the backend's current answer (not a stale fallback):
    only then does a slug's absence mean "delisted"."""
    return bool(snap) and not snap.get("stale")


def catalog(refresh: bool = False) -> dict:
    """GET /api/marketplace/catalog's payload."""
    snap, error = get_snapshot(refresh=refresh)
    if snap is None:
        return {"status": "error", "message": error, "apps": [], "categories": [],
                "collections": [], "index": index_repo()}
    installed = installed_copies()
    apps = snap["apps"]
    entries = [app_entry(slug, row, installed.get(slug)) for slug, row in sorted(apps.items())]
    collections = [{"id": c.get("id"), "kind": c.get("kind"), "title": c.get("title") or "",
                    "slugs": [s for s in c.get("slugs") or [] if s in apps]}
                   for c in snap.get("collections") or [] if isinstance(c, dict)]
    return {
        "status": "ok",
        "source": snap.get("source"),
        "api": snap.get("api"),
        # The index repo the catalog is read from (§18.1), for the footer link.
        "index": index_repo(),
        "catalog_version": snap.get("catalog_version"),
        "generated_at": snap.get("generated_at"),
        "fetched_at": snap.get("fetched_at"),
        "stale": bool(snap.get("stale")),
        "warning": snap.get("warning") or None,
        "categories": [{"id": c["id"], "label": c.get("label") or c["id"],
                        "count": _int(c.get("count"))} for c in snap.get("categories") or []],
        "collections": collections,
        "apps": entries,
    }


def search(q: str, category: str = "", cursor: str = "", limit: int = 24) -> dict:
    """GET /api/marketplace/search's payload: the server's ranked answer, each
    AppEntry carrying its `score` and `highlights`. Installed copies are
    merged in like the catalog's. SourceError → `{status: "error", message}`."""
    from fused_render import __version__

    try:
        rows = _udf_get("marketplace_search", q=q or "", category=category or None,
                        cursor=cursor or None, limit=limit, client_version=__version__)
    except SourceError as exc:
        return {"status": "error", "message": str(exc)}
    installed = installed_copies()
    snap, _err = cached_snapshot()
    catalog_apps = (snap or {}).get("apps") or {}
    apps = []
    for row in rows:
        slug = row.get("slug")
        if not is_slug(slug):
            continue
        # The catalog's row underneath: a search row without `commit` must
        # not read an installed current copy as updatable.
        entry = app_entry(slug, {**(catalog_apps.get(slug) or {}), **row}, installed.get(slug))
        entry["score"] = row.get("score") if isinstance(row.get("score"), (int, float)) else 0
        entry["highlights"] = [{"field": str(h.get("field") or ""), "snippet": str(h.get("snippet") or "")}
                               for h in row.get("highlights") or [] if isinstance(h, dict)]
        apps.append(entry)
    nxt = next((r.get("next_cursor") for r in rows if r.get("next_cursor")), None)
    return {"status": "ok", "apps": apps, "next_cursor": nxt if isinstance(nxt, str) else None}


def _local_entry(slug: str, copy: dict, *, catalog_fresh: bool = False) -> dict:
    """An AppEntry for a copy the catalog does not list (delisted, or the
    catalog unavailable), from what its record/stamp and its own
    metadata.json know. Never anything that points at the catalog: no
    assets to serve, nothing to update to."""
    try:
        with open(os.path.join(copy["path"], "metadata.json"), "rb") as f:
            meta = _parse_metadata(f.read()) or {}
    except OSError:
        meta = {}
    size = count = 0
    version = None
    if copy["kind"] == "legacy":
        paths = [rel for rel in sorted(copy["stamp"].get("files") or {}) if _safe_rel(rel)]
        for rel in paths:
            try:
                size += os.path.getsize(os.path.join(copy["path"], *rel.split("/")))
            except OSError:
                pass
        count = len(paths)
        version = _opt_str(copy["stamp"].get("version"))
    else:
        version = _opt_str(_dict(_dict(copy.get("record")).get("approved")).get("version"))
    row = {**{k: v for k, v in meta.items() if k not in (
        "icon_url", "preview_url", "source_url", "featured", "release_id", "commit", "repo",
        "releases", "remix_count", "contributor_count", "install_count")},
        "size_bytes": size, "file_count": count}
    entry = app_entry(slug, row, copy, catalog_fresh=catalog_fresh, listed=False)
    entry.update(update_available=False, featured=False, installable=False)
    if not meta.get("version") and version:
        entry["version"] = version
    return entry


def installed_list() -> dict:
    """GET /api/marketplace/installed — every Marketplace copy (marketplace
    clones and legacy installs), catalog-backed where the catalog (cached;
    never forces a network refresh) still lists it, from the copy itself
    where it does not (the orphans stay: delisted copies keep working)."""
    snap, _error = cached_snapshot()
    apps = (snap or {}).get("apps") or {}
    fresh = _fresh(snap)
    out = []
    for slug, copy in sorted(installed_copies().items()):
        if slug in apps:
            out.append(app_entry(slug, apps[slug], copy))
        else:
            out.append(_local_entry(slug, copy, catalog_fresh=fresh))
    return {"apps": out}


# ---------------------------------------------------------------------------
# acknowledgements
# ---------------------------------------------------------------------------
#
# ``<state_dir>/acks.json`` = ``{"python": ["<slug>", ...]}``: the apps whose
# "this app runs Python on your machine" notice the user has accepted, so the
# install flow asks once per app, not once per install. It was the browser's
# localStorage (``fused-render:marketplace:python-ack``) until D1386: storage
# is per origin and the desktop app walks ports 1777..1787 then an ephemeral
# one, so a relaunch on another port asked again for every app. The client
# imports its old list once (one POST per slug) and removes the key.

_ACKS_LOCK = threading.Lock()


def acks_path() -> str:
    return os.path.join(state_dir(), "acks.json")


def python_acks() -> list[str]:
    """The acknowledged slugs, sorted; ``[]`` when absent or unreadable.
    Entries that are not slugs are skipped (a hand-edited file degrades)."""
    try:
        with open(acks_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    slugs = data.get("python") if isinstance(data, dict) else None
    if not isinstance(slugs, list):
        return []
    return sorted({s for s in slugs if is_slug(s)})


def ack_python(slug: str) -> list[str]:
    """Record that the user accepted `slug`'s Python notice; idempotent.
    Returns the acknowledged slugs. ValueError when `slug` is not a slug."""
    if not is_slug(slug):
        raise ValueError("not a Community slug")
    os.makedirs(state_dir(), exist_ok=True)
    # `_ACKS_LOCK` serialises this process's threads; the advisory flock on a
    # sidecar file serialises PROCESSES — other worktrees' servers share this
    # state dir (it is not nested by branch), and two of them acknowledging
    # different slugs at once would each write the list it read, the slower
    # one dropping the other's slug (shell/notifications.py's `_file_lock`
    # posture). Released when the file closes; absent `fcntl` the thread lock
    # is all there is.
    with _ACKS_LOCK, open(acks_path() + ".lock", "a+") as lock_fh:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        slugs = python_acks()
        if slug in slugs:
            return slugs
        slugs = sorted({*slugs, slug})
        # Unique per writer, like the catalog cache.
        tmp = f"{acks_path()}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"python": slugs}, f)
            os.replace(tmp, acks_path())
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
        return slugs


# ---------------------------------------------------------------------------
# assets + detail
# ---------------------------------------------------------------------------

#: (slug, release_id) → the parsed `marketplace_app_detail` row. Answers the
#: detail page without a network call and is what the asset proxy checks a
#: path against (the snapshot lists no files). Bounded; the oldest-used entry
#: goes first.
_DETAIL_MEMO: dict[tuple, dict] = {}
_DETAIL_MEMO_LOCK = threading.Lock()
_DETAIL_MEMO_MAX = 256
#: The detail page and the asset proxy must not hang on a slow API.
DETAIL_TIMEOUT_S = 8


def _memo_get(key: tuple) -> dict | None:
    with _DETAIL_MEMO_LOCK:
        row = _DETAIL_MEMO.pop(key, None)
        if row is not None:
            _DETAIL_MEMO[key] = row  # most recently used goes last
        return row


def _memo_put(key: tuple, row: dict) -> None:
    with _DETAIL_MEMO_LOCK:
        _DETAIL_MEMO.pop(key, None)
        while len(_DETAIL_MEMO) >= _DETAIL_MEMO_MAX:
            _DETAIL_MEMO.pop(next(iter(_DETAIL_MEMO)))
        _DETAIL_MEMO[key] = row


def _detail_row(slug: str, snap_row: dict | None) -> dict | None:
    """The API's `marketplace_app_detail` row for `slug` (None when the API
    does not list it). Memoised under the snapshot row's (slug, release id),
    and only while the answer is that very release. Raises SourceError."""
    key = (slug, _release_key(snap_row) if snap_row else None)
    if snap_row is not None:
        hit = _memo_get(key)
        if hit is not None:
            return hit
    from fused_render import __version__

    rows = _udf_get("marketplace_app_detail", timeout=DETAIL_TIMEOUT_S,
                    slug=slug, client_version=__version__)
    row = next((r for r in rows if r.get("slug") == slug), None)
    if row is not None and snap_row is not None and key[1] and _release_key(row) == key[1]:
        _memo_put(key, row)
    return row


def _asset_allowed(slug: str, rel: str, snap_row: dict) -> bool:
    """The asset proxy's allow-list: the snapshot row's own icon or preview, or
    a file the app's detail lists. Anything else is a 404 — the proxy is not a
    window onto the whole repo."""
    if rel in (_asset_rel(slug, snap_row, snap_row.get("icon_url")),
               _asset_rel(slug, snap_row, snap_row.get("preview_url"))):
        return True
    try:
        row = _detail_row(slug, snap_row)
    except SourceError:
        return False
    return any(isinstance(f, dict) and f.get("path") == rel for f in (row or {}).get("files") or [])


def _content_type(path: str) -> str:
    low = path.lower()
    if low.endswith(".svg"):
        return "image/svg+xml"
    if low.endswith((".md", ".markdown")):
        return "text/markdown; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path)
    if guessed and (guessed.startswith("text/") or guessed in ("application/json",
                                                               "application/javascript")):
        return guessed + "; charset=utf-8"
    return guessed or "application/octet-stream"


def _fetch_asset_bytes(base: str, rel: str) -> bytes | None:
    """The bytes of `<rel>` under the app's raw GitHub `base` (pinned to the
    approved commit), or None."""
    r, err = _http_get(base + quote(rel, safe="/"))
    return r.content if not err and r is not None else None


def get_asset(slug: str, rel: str) -> tuple[bytes, str] | None:
    """(bytes, content type) for one file of a catalog app, or None (→ 404):
    unknown slug, an unsafe path, too large, or unfetchable."""
    got = get_asset_versioned(slug, rel)
    return got[:2] if got else None


def get_asset_versioned(slug: str, rel: str) -> tuple[bytes, str, str | None] | None:
    """`get_asset` plus the `?v=` key of the SAME catalog snapshot the bytes
    came from. The asset route grants long browser caching only when the
    request's `v` equals it — an arbitrary `?v=1` in a readme link proves
    nothing — and reading the key from a second cache read could pair new-key
    with old bytes if a refresh landed in between. Only ever fetches under
    the row's own raw-GitHub base, and only for the row's icon/preview or a
    file its detail lists (`_asset_allowed`; bytes already on disk were
    admitted by that check when they were fetched)."""
    if not is_slug(slug) or not _safe_rel(rel):
        return None
    snap, _err = cached_snapshot()
    if not snap:
        return None
    row = snap["apps"].get(slug)
    if not row:
        return None
    version = _asset_version(row)
    base = _asset_base(slug, row)
    rid = _release_key(row)
    if version is None or base is None or rid is None:
        return None  # the release id is a cache directory name; never trust it unshaped
    cached = os.path.join(state_dir(), "assets", rid, slug, *rel.split("/"))
    try:
        with open(cached, "rb") as f:
            return f.read(), _content_type(rel), version
    except OSError:
        pass
    if not _asset_allowed(slug, rel, row):
        return None
    data = _fetch_asset_bytes(base, rel)
    if data is None or len(data) > MAX_ASSET_BYTES:
        return None
    try:
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        tmp = f"{cached}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cached)
    except OSError:
        pass  # served uncached
    return data, _content_type(rel), version


def _installed_detail(slug: str, copy: dict, *, catalog_fresh: bool = False) -> dict:
    """Installed but gone from the catalog: describe the local copy."""
    entry = _local_entry(slug, copy, catalog_fresh=catalog_fresh)
    readme = ""
    for name in ("readme.md", "README.md", "Readme.md"):
        try:
            with open(os.path.join(copy["path"], name), "rb") as f:
                readme = f.read(MAX_README_BYTES).decode("utf-8", "replace")
            break
        except OSError:
            continue
    stamp_files = (copy.get("stamp") or {}).get("files") or {}
    files = [{"path": p, "size": 0} for p in sorted(stamp_files) if _safe_rel(p)]
    return {"status": "ok", "app": entry, "readme": readme, "files": files,
            "readme_base": f"/api/marketplace/asset/{slug}/", "releases": []}


def app_detail(slug: str) -> dict | None:
    """GET /api/marketplace/app/{slug}'s payload, or None (→ 404).
    `releases` is §18.3's list (`{commit, at, whats_new, version, blocked}`,
    newest first) — the AppEntry carries the same list."""
    if not is_slug(slug):
        return None
    installed = installed_copies()
    snap, _error = cached_snapshot()
    snap_row = ((snap or {}).get("apps") or {}).get(slug)
    try:
        row = _detail_row(slug, snap_row)
    except SourceError as exc:
        if exc.error_code != "app_not_found" and snap_row is not None:
            # The API is unreachable, but the cached catalog knows the app:
            # the card still renders; only the readme and file list are lost.
            entry = app_entry(slug, snap_row, installed.get(slug))
            return {"status": "ok", "app": entry, "readme": "", "files": [],
                    "readme_base": f"/api/marketplace/asset/{slug}/", "releases": entry["releases"]}
        row = None
    if row is not None:
        files = [{"path": f["path"], "size": _int(f.get("size"))}
                 for f in row.get("files") or []
                 if isinstance(f, dict) and isinstance(f.get("path"), str)]
        readme = row.get("readme_markdown")
        entry = app_entry(slug, {**(snap_row or {}), **row}, installed.get(slug))
        return {"status": "ok", "app": entry,
                "readme": readme[:MAX_README_BYTES] if isinstance(readme, str) else "",
                "files": files, "readme_base": f"/api/marketplace/asset/{slug}/",
                "releases": entry["releases"]}
    if slug in installed:
        return _installed_detail(slug, installed[slug], catalog_fresh=_fresh(snap))
    return None


# ---------------------------------------------------------------------------
# install / update / switch
# ---------------------------------------------------------------------------

def _who(row: dict) -> str:
    """The author's login as the page says it: the row's author GitHub
    login, else the repo's owner."""
    gh = _author(row.get("author")).get("github")
    if gh:
        return gh
    repo = row_repo(row)
    return repo.split("/", 1)[0] if repo else "the author"


def _record_kind(path: str) -> str | None:
    try:
        from fused_render.contrib import store

        rec = store.get(path)
    except Exception:  # noqa: BLE001 — unreadable reads as "no record"
        return None
    return rec.get("kind") if rec else None


def plan_install(slug: str, row: dict) -> dict:
    """What installing `slug` (catalog row `row`) would do HERE — the one
    answer the router, the job and the bots' card read (module docstring):
    `{"action": "install"|"update"|"switch"|"refuse", "path": str|None,
    "who": str, "reason": str|None, "code": str|None}`. Read-only; the
    GitHub-link copy lookup costs one git call per workspace clone."""
    who = _who(row)
    plan = {"action": "install", "path": None, "who": who, "reason": None, "code": None}
    copy = installed_copies().get(slug)
    if copy and copy["kind"] == "legacy":
        # A legacy copy never updates in place (M2: no packages); the
        # Installed row's "Switch to the reviewed version" (adopt) is the way.
        return {**plan, "action": "refuse", "path": copy["path"], "code": "legacy",
                "reason": "Switch to the reviewed version first: this app was installed from the "
                          "old app store. Use “Switch to the reviewed version” on the Installed page."}
    if copy and copy["kind"] == "own":
        return {**plan, "action": "refuse", "path": copy["path"], "code": "own",
                "reason": f"This is your own app; it's already at {copy['path']}."}
    if copy:
        return {**plan, "action": "update", "path": copy["path"]}
    repo, commit = row_repo(row), row.get("commit")
    if not repo or not is_sha(commit):
        return {**plan, "action": "refuse", "code": "no-source",
                "reason": "This app has no reviewed version to install yet."}
    from fused_render import appfile_git

    clone = appfile_git.installed_clone_of(f"https://github.com/{repo}")
    if clone is None:
        return plan
    kind = _record_kind(clone)
    if kind == "marketplace":
        # A marketplace copy whose record names another slug (a relisting):
        # still this repo's reviewed copy.
        return {**plan, "action": "update", "path": clone}
    if kind == "github":
        return {**plan, "action": "switch", "path": clone}
    if kind == "own":
        return {**plan, "action": "refuse", "path": clone, "code": "own",
                "reason": f"This is your own app; it's already at {clone}."}
    return {**plan, "action": "refuse", "path": clone, "code": "exists",
            "reason": f"You already have a copy of this app at {clone}."}


_JOBS: dict[str, dict] = {}
_JOB_FINISHED: dict[str, float] = {}
_JOBS_LOCK = threading.Lock()
_ACTIVE = ("queued", "downloading", "installing")


def _prune_jobs_locked() -> None:
    now = time.monotonic()
    for slug, t in list(_JOB_FINISHED.items()):
        if now - t > JOB_TTL_S:
            _JOB_FINISHED.pop(slug, None)
            _JOBS.pop(slug, None)


def list_jobs() -> dict:
    with _JOBS_LOCK:
        _prune_jobs_locked()
        return {"jobs": {slug: dict(job) for slug, job in _JOBS.items()}}


def _update_job(slug: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(slug)
        if job is None:
            return
        job.update(fields)
        if job["state"] in ("done", "error"):
            _JOB_FINISHED[slug] = time.monotonic()


def start_install(slug: str, *, update: bool = False, switch: bool = False) -> dict:
    """Start installing, updating or switching `slug` in the background
    (`plan_install` decides which; `update` is the bot tool's flag and
    changes nothing here — what is installed decides). Answers the job —
    the running one if any: idempotent — or, when a GitHub-link copy of the
    same repo exists and `switch` is not set, the question `{"status":
    "confirm", "switch": "reviewed", "path", "who", "slug", "message"}`
    with nothing started. Raises InstallError for a request that cannot
    start (bad/unknown slug, a refusing plan)."""
    del update  # documented above: the plan, not the caller, decides
    if not is_slug(slug):
        raise InstallError("invalid app slug")
    with _JOBS_LOCK:
        _prune_jobs_locked()
        job = _JOBS.get(slug)
        if job and job["state"] in _ACTIVE:
            return dict(job)
    snap, error = cached_snapshot()
    if snap is None:
        raise InstallError(error or "the catalog is unavailable")
    row = snap["apps"].get(slug)
    if row is None:
        raise InstallError(f"no app named {slug!r} in the catalog")
    plan = plan_install(slug, row)
    if plan["action"] == "refuse":
        raise InstallError(plan["reason"])
    if plan["action"] == "switch" and not switch:
        return {"status": "confirm", "switch": "reviewed", "slug": slug,
                "path": plan["path"].replace(os.sep, "/"), "who": plan["who"],
                "message": "Switch to reviewed releases? You follow "
                           f"{plan['who']} directly now and get every change, unreviewed."}
    action = plan["action"]
    with _JOBS_LOCK:
        job = _JOBS.get(slug)
        if job and job["state"] in _ACTIVE:
            return dict(job)
        job = {"slug": slug, "action": action, "state": "queued", "step": "download",
               "progress": 0.0, "bytes_done": 0, "bytes_total": _int(row.get("size_bytes")),
               "message": "Waiting to start…", "path": None, "error": None}
        _JOBS[slug] = job
        _JOB_FINISHED.pop(slug, None)
        out = dict(job)
    threading.Thread(target=_run_job, args=(slug, row, plan), daemon=True,
                     name=f"marketplace-{action}-{slug}").start()
    return out


def resolve_install(slug: str, row: dict) -> dict:
    """The row an install goes by: the marketplace's answer RIGHT NOW
    (`fused_app_install`, §18.12), falling back to the cached catalog row
    `row`. Why ask: the local catalog can be ten minutes behind the index,
    so an install right after a release would pin the previous commit; and
    the ask is what the marketplace counts as one install — the request
    carries the slug and nothing else (no telemetry call exists: this is the
    install's own lookup). Can't be asked, or answers nonsense → the cached
    row, uncounted, logged. Answers a hard block → InstallError (the cached
    row may predate the block)."""
    try:
        rows = _udf_get("fused_app_install", timeout=DETAIL_TIMEOUT_S, slug=slug)
    except SourceError as exc:
        logger.warning("marketplace: installing %s by the cached row (%s)", slug, exc)
        return row
    live = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
    if not live or live.get("slug") != slug or not row_repo(live) or not is_sha(live.get("commit")):
        logger.warning("marketplace: installing %s by the cached row (unexpected answer)", slug)
        return row
    if live.get("blocked") == "hard":
        raise InstallError("Fused withdrew this release, so it can't be installed right now.")
    if (row_repo(live), live["commit"]) == (row_repo(row), row.get("commit")):
        return row
    logger.info("marketplace: %s moved to %s@%s since the catalog was read; installing that",
                slug, row_repo(live), live["commit"][:7])
    out = dict(row)
    out["repo"] = row_repo(live)
    out["source_url"] = f"https://github.com/{out['repo']}"
    for k in ("commit", "release_id", "branch", "version", "name"):
        if _opt_str(live.get(k)):
            out[k] = live[k]
    # The cached row's `blocked` was about ITS commit, not the live one.
    out["blocked"] = live.get("blocked") if live.get("blocked") in _BLOCK_LEVELS else None
    # The catalog's releases list does not know this commit: the vetted
    # entry (`install_app`, `market._pick`) then takes the version from the
    # row and leaves What's new empty.
    out["whats_new"] = None
    return out


def install_app(slug: str, row: dict, report=lambda **kw: None) -> dict:
    """A NEW marketplace copy of `slug`, synchronously: the author's repo
    cloned at the row's approved commit and installed through the GitHub
    link installer as kind `marketplace` (module docstring). `report(**job
    fields)` receives progress. Answers `{action, path}`. Raises
    InstallError."""
    from fused_render import app_listing, appfetch, appfile, appfile_git

    from fused_render.contrib import market as contrib_market

    row = resolve_install(slug, row)
    repo, commit = row_repo(row), row.get("commit")
    if not repo or not is_sha(commit):
        raise InstallError("This app has no reviewed version to install yet.")
    # The commit is VETTED here whichever row it came from (M3): the live
    # answer refuses a hard block itself, but the cached row the fallback
    # uses may predate one. `_pick` skips a release the row or the block
    # list on disk marks hard-blocked and answers the newest one left — an
    # older release, possibly — and the copy records exactly that one.
    ap = contrib_market._pick(row, slug)
    if ap is None or ap.entry() is None:
        raise InstallError("Fused withdrew this release, so it can't be installed right now.")
    commit = ap.commit
    url = f"https://github.com/{repo}"
    report(state="downloading", step="download", progress=0.05,
           message=f"Downloading {repo} at its reviewed version…")
    appfile_git.sweep_staging()
    try:
        clone_dir, commit, committed_at = appfile_git.clone(url, repo.split("/", 1)[1], commit=commit)
    except appfile_git.GitInstallError as exc:
        raise InstallError(str(exc))
    report(progress=0.6)
    built = None
    try:
        try:
            entry = app_listing.app_entry(clone_dir)
        except OSError:
            entry = None
        if entry is None:
            raise InstallError(f'no page at the root of {repo} carries <meta name="fused-app">, '
                               "so it cannot be installed")
        # Packed exactly as a GitHub link packs it (appfetch.download_github_app):
        # the `.fused` only drives install_target/install; the clone itself
        # is what lands. The app id is the page's own, else the repo-derived
        # one `install` re-derives and checks.
        derived = appfile_git.expected_identity(clone_dir, url)[1]
        fd, built = tempfile.mkstemp(dir=appfetch.downloads_dir(), prefix=".market-", suffix=".fused")
        os.close(fd)
        os.remove(built)
        try:
            manifest = appfile.export_app_file(
                clone_dir, built, exported_at=committed_at, app_id=derived,
                extra={"git": {"url": url, "commit": commit}})
        except appfile.AppFileError as exc:
            raise InstallError(str(exc))
        report(state="installing", step="place", progress=0.8, message="Installing…")
        target = appfile.install_target(built)
        if target.get("found_by") == "git":
            raise InstallError(f"You already have a copy of this app at {target['path']}.")
        if target["state"] == appfile.STATE_REPLACE:
            # Another copy of this app (installed from a file) holds its
            # place; replacing it would send it to the Trash. Never on the
            # user's behalf from here.
            raise InstallError(f"{target['path']} already holds this app, installed another way. "
                               "Uninstall it first, then install it from the Community.")
        try:
            # `approved` is the vetted release (`_pick` above), a dict with
            # the 40-hex commit: a bare sha would leave the copy with no
            # approved commit.
            market = {"slug": slug, "approved": ap.entry()}
            if _opt_str(row.get("branch")):
                market["branch"] = row["branch"]
            appfile_git.install(built, target, manifest, force=False, kind="marketplace", market=market)
        except (appfile_git.GitInstallError, appfile.AppFileError) as exc:
            raise InstallError(str(exc))
        dest = os.path.normpath(target["path"])
    except OSError as exc:
        raise InstallError(f"could not install the app ({exc.strerror or exc})")
    finally:
        if built:
            with contextlib.suppress(OSError):
                os.remove(built)
        shutil.rmtree(os.path.dirname(clone_dir), ignore_errors=True)
    # A husk's `.fused/` rides into the clone (appfile_git._place): a stale
    # LEGACY stamp in it would list this folder twice. The record is the
    # truth now (adopt_legacy deletes the stamp the same way).
    with contextlib.suppress(OSError):
        os.remove(stamp_path(dest))
    # The HEAD the copy starts at, so the open gate (`blocks.gate`, a record
    # read) knows it from the first open — before any enforcement ran.
    try:
        from fused_render.contrib import store as contrib_store

        contrib_store.note_head(dest, commit)
    except Exception:  # noqa: BLE001 — the engine's first sync notes it too
        logger.warning("marketplace: could not note the head of %s", dest, exc_info=True)
    _reset_apps_snapshot()
    return {"action": "install", "path": dest}


def _reset_apps_snapshot() -> None:
    """The /apps hub's snapshot lags the disk by up to 20 s; never on the one
    folder just installed (same reset the `.fused` installer does)."""
    try:
        from fused_render.server.routers import apps as apps_router

        apps_router._reset_snapshot()
    except Exception:  # noqa: BLE001
        logger.debug("marketplace: apps snapshot reset failed", exc_info=True)


def _engine_answer(res, fallback: str) -> str:
    """The engine's answer → its message, or InstallError with it."""
    if not isinstance(res, dict) or res.get("ok") is False or res.get("needs_merge"):
        msg = (res or {}).get("message") if isinstance(res, dict) else None
        if isinstance(res, dict) and res.get("needs_merge"):
            msg = (msg or "Your changes overlap this release.") + " Open the app to combine them."
        raise InstallError(msg or fallback)
    return res.get("message") or ""


def update_app(slug: str, path: str, report=lambda **kw: None) -> dict:
    """Bring the marketplace copy at `path` to its row's approved commit —
    the engine's release move (`engine.bring_in`, which reads the record's
    approved commit fresh through `market.refresh_approved`). Raises
    InstallError with the engine's own sentence."""
    from fused_render.contrib import engine

    report(state="installing", step="place", progress=0.3, message="Updating…")
    msg = _engine_answer(engine.bring_in(os.path.realpath(path)),
                         "Couldn't update this app right now. Open it to see why.")
    return {"action": "update", "path": path, "message": msg}


def switch_app(slug: str, row: dict, path: str, report=lambda **kw: None) -> dict:
    """The user said yes to "Switch to reviewed releases?" for the GitHub-link
    copy at `path`: `market.switch_to_reviewed` (one Activity line + Undo)."""
    from fused_render.contrib import market

    report(state="installing", step="place", progress=0.3, message="Switching to reviewed releases…")
    msg = _engine_answer(market.switch_to_reviewed(os.path.realpath(path), row),
                         "Couldn't switch this app right now. Open it to see why.")
    return {"action": "switch", "path": path, "message": msg}


def _run_job(slug: str, row: dict, plan: dict) -> None:
    def report(**fields):
        _update_job(slug, **fields)

    name = _opt_str(row.get("name")) or slug
    try:
        if plan["action"] == "update":
            result = update_app(slug, plan["path"], report)
        elif plan["action"] == "switch":
            result = switch_app(slug, row, plan["path"], report)
        else:
            result = install_app(slug, row, report)
        message = result.get("message") or {
            "install": f"Installed {name}", "update": f"Updated {name}",
            "switch": f"{name} follows reviewed releases now"}[result["action"]]
        with _JOBS_LOCK:
            total = (_JOBS.get(slug) or {}).get("bytes_total") or 0
        _update_job(slug, state="done", step="done", progress=1.0, bytes_done=total,
                    path=result["path"].replace(os.sep, "/"), message=message)
    except InstallError as exc:
        _update_job(slug, state="error", error=str(exc), message=str(exc))
    except Exception as exc:  # noqa: BLE001 — a job must always finish
        logger.exception("marketplace: %s of %s crashed", plan.get("action"), slug)
        msg = f"{plan.get('action') or 'install'} failed ({exc.__class__.__name__}: {exc})"
        _update_job(slug, state="error", error=msg, message=msg)


# ---------------------------------------------------------------------------
# legacy adopt + check (the Installed page's buttons)
# ---------------------------------------------------------------------------

def adopt(slug: str) -> dict:
    """POST /adopt: turn the LEGACY copy of `slug` into a marketplace copy in
    place (`market.adopt_legacy`; the user's edits and data stay). Offered
    only while the slug is listed. Raises InstallError."""
    if not is_slug(slug):
        raise InstallError("invalid app slug")
    copy = installed_copies().get(slug)
    if copy and os.path.lexists(os.path.join(copy["path"], ".git")):
        # Never `git init` over a repo (the author's own clone, a link copy):
        # only a plain-files folder is a legacy install.
        raise InstallError("This is already a copy of the app's repo.")
    if not copy or copy["kind"] != "legacy":
        raise InstallError("There is no copy from the old app store to switch.")
    snap, error = cached_snapshot()
    row = ((snap or {}).get("apps") or {}).get(slug)
    if row is None:
        raise InstallError("This app is no longer in the Community, so there is no reviewed "
                           "version to switch to.")
    if not row_repo(row) or not is_sha(row.get("commit")):
        raise InstallError("This app has no reviewed version to switch to yet.")
    from fused_render.contrib import market

    res = market.adopt_legacy(os.path.realpath(copy["path"]), row)
    msg = _engine_answer(res, "Couldn't switch this app right now.")
    _reset_apps_snapshot()
    return {"path": copy["path"].replace(os.sep, "/"), "message": msg, "result": res}


def check(slug: str | None = None) -> dict:
    """POST /check — "Check for a new release": the catalog rebuilt past its
    TTL, the block list refreshed and enforced on every copy, then (with a
    slug) a sync of that marketplace copy, which the engine turns into the
    release move. Each step is best-effort and reported, never raised: the
    button always answers. A slug that names no marketplace copy is just
    the first two steps."""
    snap, error = get_snapshot(force=True)
    out: dict = {"status": "ok",
                 "catalog": {"ok": snap is not None and not snap.get("stale"),
                             "stale": bool((snap or {}).get("stale")),
                             "fetched_at": (snap or {}).get("fetched_at"),
                             "message": error or (snap or {}).get("warning")},
                 "blocklist": None, "enforced": [], "sync": None, "app": None}
    try:
        from fused_render.contrib import blocks

        ok = blocks.refresh()
        out["enforced"] = [e for e in (blocks.enforce_all() or []) if isinstance(e, dict)]
    except ImportError:
        ok = blocklist(force=True).get("status") == "ok"
    except Exception:  # noqa: BLE001 — a crashing enforcement must not eat the answer
        logger.exception("marketplace: block list check failed")
        ok = False
    bl = blocklist()
    out["blocklist"] = {"ok": bool(ok), "checked_at": bl.get("checked_at"), "count": len(bl["blocked"])}
    if is_slug(slug):
        copy = installed_copies().get(slug)
        if copy and copy["kind"] == "marketplace":
            try:
                from fused_render import git_upstream

                out["sync"] = git_upstream.sync_repo(os.path.realpath(copy["path"]),
                                                     action="Check for a new release", push=False)
            except Exception as exc:  # noqa: BLE001
                logger.exception("marketplace: check sync failed for %s", copy["path"])
                out["sync"] = {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"}
        cur, _e = cached_snapshot()
        row = ((cur or {}).get("apps") or {}).get(slug)
        copy = installed_copies().get(slug)
        if row is not None:
            out["app"] = app_entry(slug, row, copy)
        elif copy:
            out["app"] = _local_entry(slug, copy, catalog_fresh=_fresh(cur))
    return out


# ---------------------------------------------------------------------------
# block list (B4)
# ---------------------------------------------------------------------------
#
# `<state>/blocklist.json` = {"blocked": [{slug, commit, level, reason,
# what_to_do, at}], "checked_at": <last attempt>, "fetched_at": <last
# success, iso>, "generated_at", "stale": bool, "status": "ok"|"stale"|
# "error", "message"}. Same TTL and back-off as the catalog (`checked_at` is
# the last ATTEMPT); `force` skips the TTL (the Check button, the engine's
# check action). A failed fetch keeps the last list (offline, the last list
# applies — §18.4) flagged stale; with no list at all the answer is an
# empty list with `status: "error"`, persisted too so the back-off holds.
# Fail-closed where the data is ours to judge: an entry whose slug or commit
# is malformed is dropped (it can name nothing), a `level` that is neither
# hard nor soft reads as hard (M3).
#
# THE ANSWER'S SHAPE. The backend answers either the older bare list of
# entry rows (each carrying the index's `generated_at`) or the
# self-describing one, `{"generated_at", "entries": [...], "stale"?}` (one
# row, `_udf_get`). An answer the client cannot vouch for is "unknown", never
# a fresh list: a row or header saying `stale: true` (the backend fell back
# to its last copy of the index), a header without `generated_at`, a
# `generated_at` older than the list already here, or EVERY known entry
# gone at once — unless a header with a newer `generated_at` says so (an
# empty bare list, or a garbled one whose rows all fail to parse, is how an
# unreadable index used to look). An unknown answer can only ADD entries:
# the file keeps the last list plus anything new it named, flagged stale
# (`contrib.blocks.refresh` then answers False). `header` records that the
# list on disk came from the self-describing shape — the one answer that
# may also lift a hard block that never rolled back (`contrib.blocks`).

_BLOCKLIST_LOCK = threading.Lock()


def _clean_block(r) -> dict | None:
    if not isinstance(r, dict) or not is_slug(r.get("slug")) or not is_sha(r.get("commit")):
        return None
    return {"slug": r["slug"], "commit": r["commit"],
            "level": r.get("level") if r.get("level") in _BLOCK_LEVELS else "hard",
            "reason": _opt_str(r.get("reason")) or "",
            "what_to_do": _opt_str(r.get("what_to_do")) or "",
            "at": _opt_str(r.get("at"))}


def _read_blocklist() -> dict | None:
    try:
        with open(blocklist_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("blocked"), list):
        return None
    data["blocked"] = [b for b in (_clean_block(r) for r in data["blocked"]) if b]
    return data


def _write_blocklist(data: dict) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    tmp = f"{blocklist_path()}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, blocklist_path())
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _had_list(data: dict | None) -> bool:
    """Whether `data` is a list the backend once answered (vs. the empty
    stand-in written when it never did). contrib/blocks.refresh may rewrite
    the file as just `{blocked, checked_at}`: that is a list too."""
    return data is not None and data.get("status") != "error"


def _blocklist_view(data: dict) -> dict:
    status = data.get("status")
    if status not in ("ok", "stale", "error"):
        status = "stale" if data.get("stale") else "ok"  # the slim shape (see `_had_list`)
    return {"blocked": list(data.get("blocked") or []), "checked_at": data.get("checked_at"),
            "fetched_at": data.get("fetched_at"), "generated_at": data.get("generated_at"),
            "header": data.get("header") is True,
            "stale": bool(data.get("stale")), "status": status, "message": data.get("message")}


def _iso_time(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _blocklist_answer(rows: list[dict], cached: dict | None, now: float) -> dict:
    """The file to keep for a backend answer (the comment above: shapes,
    and when an answer is "unknown")."""
    head = rows[0] if len(rows) == 1 and isinstance(rows[0].get("entries"), list) else None
    if head is not None:
        raw = head["entries"]
        generated = _opt_str(head.get("generated_at"))
        stale = bool(head.get("stale")) or generated is None
    else:
        raw = rows
        generated = next((_opt_text(r.get("generated_at")) for r in rows
                          if r.get("generated_at")), None)
        stale = any(bool(r.get("stale")) for r in rows)
    got = [b for b in (_clean_block(r) for r in raw) if b]
    known = list((cached or {}).get("blocked") or []) if _had_list(cached) else []
    had_gen, new_gen = _iso_time((cached or {}).get("generated_at")), _iso_time(generated)
    # Only a header newer than the list here may say "nothing is blocked any
    # more" after something was.
    newer = head is not None and new_gen is not None and (had_gen is None or new_gen > had_gen)
    why = None
    if stale:
        why = "the Community answered from an older copy of the block list"
    elif had_gen and new_gen and new_gen < had_gen:
        why = "the Community answered an older block list than the one here"
    elif known and not got and not newer:
        why = "the Community's block list came back empty"
    elif any(_clean_block(r) is None or (isinstance(r, dict) and r.get("malformed"))
             for r in raw) and {(b["slug"], b["commit"]) for b in known} - {
                 (b["slug"], b["commit"]) for b in got}:
        # An entry fused cannot read could be the one that left: it may
        # add (below), never lift.
        why = "the Community's block list has entries fused could not read"
    if why is None:
        return {"blocked": got, "checked_at": now, "fetched_at": _utc_now(),
                "generated_at": generated, "header": head is not None,
                "stale": False, "status": "ok", "message": None}
    logger.warning("marketplace: block list answer not trusted (%s); keeping the last list", why)
    have = {(b["slug"], b["commit"]) for b in known}
    merged = known + [b for b in got if (b["slug"], b["commit"]) not in have]
    base = dict(cached) if cached is not None else {"fetched_at": None, "generated_at": None}
    return {**base, "blocked": merged, "checked_at": now, "stale": True,
            "status": "stale" if _had_list(cached) or merged else "error",
            "message": why}


def blocklist(force: bool = False) -> dict:
    """The block list (module comment above): cached for CACHE_TTL_S since
    the last attempt unless `force`. Never raises."""
    cached = _read_blocklist()
    if not force and cached is not None and _age(cached, time.time()) < CACHE_TTL_S:
        return _blocklist_view(cached)
    if cached is not None and not force:
        if not _BLOCKLIST_LOCK.acquire(blocking=False):
            return _blocklist_view(cached)  # a fetch is in flight
    else:
        _BLOCKLIST_LOCK.acquire()
    try:
        now = time.time()
        if not force:
            cached = _read_blocklist()
            if cached is not None and _age(cached, now) < CACHE_TTL_S:
                return _blocklist_view(cached)
        try:
            rows = _udf_get("marketplace_blocklist")
        except SourceError as exc:
            logger.warning("marketplace: block list refresh failed: %s", exc)
            base = dict(cached) if cached is not None else {"blocked": [], "fetched_at": None,
                                                            "generated_at": None}
            data = {**base, "checked_at": now, "stale": True, "message": str(exc),
                    "status": "stale" if _had_list(cached) else "error"}
        except Exception as exc:  # noqa: BLE001 — same as a failed fetch
            logger.exception("marketplace: block list refresh crashed")
            base = dict(cached) if cached is not None else {"blocked": [], "fetched_at": None,
                                                            "generated_at": None}
            data = {**base, "checked_at": now, "stale": True, "message": exc.__class__.__name__,
                    "status": "stale" if _had_list(cached) else "error"}
        else:
            data = _blocklist_answer(rows, cached, now)
        try:
            _write_blocklist(data)
        except OSError:
            logger.warning("marketplace: could not write %s", blocklist_path(), exc_info=True)
        return _blocklist_view(data)
    finally:
        _BLOCKLIST_LOCK.release()


def cached_blocklist() -> dict:
    """The last block list on disk, whatever its age, without the network
    (an empty `status: "error"` list when there is none)."""
    cached = _read_blocklist()
    if cached is None:
        return {"blocked": [], "checked_at": None, "fetched_at": None, "generated_at": None,
                "stale": True, "status": "error", "message": None}
    return _blocklist_view(cached)


# ---------------------------------------------------------------------------
# the open gate (B4 hard block)
# ---------------------------------------------------------------------------

#: How far up from a page the gate looks for the copy's repo root: app
#: pages sit at a clone's root or a few folders down, never deeper.
_GATE_MAX_UP = 8


def copy_root(path: str) -> str | None:
    """The copy holding `path` (a page, or an app folder): the nearest
    folder at or above it heading a `.git` — a marketplace copy is always a
    repo root, and one stat per level keeps this cheap on the render path.
    None when there is none within `_GATE_MAX_UP` levels."""
    d = os.path.abspath(path)
    if not os.path.isdir(d):
        d = os.path.dirname(d)
    for _ in range(_GATE_MAX_UP):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up
    return None


def open_gate_at(path: str) -> tuple[str, dict] | None:
    """`(copy root, {"blocked": {...}})` while the copy holding `path` may
    not run (a hard block stands, or a soft one is not confirmed yet), else
    None — what `render`'s app route, `apps.api_app_entry`, `/api/run` and
    `/api/apps/background/start|restart` answer 423 with (§18.4: the app will
    not open).
    The verdict is `contrib.blocks.gate`'s; without that module (an older
    engine) nothing is gated. Never raises."""
    try:
        from fused_render.contrib import blocks
    except ImportError:
        return None
    try:
        root = copy_root(path)
        if root is None:
            return None
        got = blocks.gate(root)
        return (root, got) if isinstance(got, dict) and got.get("blocked") else None
    except Exception:  # noqa: BLE001 — a broken gate must not take every page down
        logger.exception("marketplace: open gate failed for %s", path)
    return None


def open_gate(path: str) -> dict | None:
    """`open_gate_at`'s payload alone."""
    got = open_gate_at(path)
    return got[1] if got else None


def _page_block(b) -> dict | None:
    """A record's `block` as the page reads it: `contrib.blocks.view` (the
    engine's one shape) when that module is there, else `_block_view`."""
    try:
        from fused_render.contrib import blocks

        return blocks.view(b)
    except ImportError:
        return _block_view(b)


def block_notices(paths) -> dict[str, dict]:
    """`{path: block view}` for the app folders among `paths` that are
    marketplace copies with a `block` on their record — ONE store read for a
    whole listing. Independent of every Community flag: block enforcement
    is (§18.8), so the page must be able to ask before a soft-blocked app
    opens (`level`, `reason`, `what_to_do`, `confirmed`) whatever the flags
    say. Never raises."""
    try:
        from fused_render.contrib import store

        records = store.all() or {}
        out = {}
        for p in paths:
            if not isinstance(p, str):
                continue
            rec = records.get(store.key(p))
            if isinstance(rec, dict) and rec.get("kind") == "marketplace" and isinstance(rec.get("block"), dict):
                view = _page_block(rec["block"])
                if view:
                    out[p] = view
        return out
    except Exception:  # noqa: BLE001 — a card line must never fail the listing
        logger.exception("marketplace: block notices failed")
        return {}


def block_notice(path: str) -> dict | None:
    """`block_notices` for the one copy holding `path`."""
    root = copy_root(path)
    return block_notices([root]).get(root) if root else None


def blocked_page(root: str, notice: dict) -> str:
    """The block notice (hard, or a soft one not confirmed yet) as a minimal HTML page — what `/render` answers
    (423) to a browser or an iframe framing the app, instead of raw JSON:
    the reason, what to do, what it was rolled back to, and a link to the
    app's own page (`/apps/<folder>`, the shell's encoding), which holds the
    BlockedNotice and its buttons. Self-contained (no runtime, no scripts),
    every value escaped; follows the OS light/dark."""
    import html as _html

    b = notice.get("blocked") if isinstance(notice.get("blocked"), dict) else {}
    esc = lambda v: _html.escape(str(v))  # noqa: E731
    soft = b.get("level") == "soft"
    rolled = b.get("rolled_back_date") or b.get("rolled_back_to_at")
    if not rolled and isinstance(b.get("rolled_back_to"), str):
        rolled = b["rolled_back_to"][:7]
    title = "This app asks before it opens" if soft else "This app is blocked"
    lines = [f"<p>{esc(b.get('reason') or ('Fused flagged the version of this app you have.' if soft else 'Fused blocked the version of this app you were running.'))}</p>"]
    if b.get("what_to_do"):
        lines.append(f"<p><strong>What to do:</strong> {esc(b['what_to_do'])}</p>")
    if soft:
        lines.append("<p>Open the app page to read more and decide whether to open it anyway.</p>")
    elif rolled:
        lines.append(f"<p>Rolled back to the release of {esc(rolled)}.</p>")
    href = "/apps/" + "/".join(quote(s, safe="") for s in root.replace(os.sep, "/").split("/") if s)
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>"
            ":root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1e;--muted:#6b6b70;--line:#e3e3e6}"
            "@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f4;--muted:#a1a1a6;--line:#3a3a3e}}"
            "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif}"
            "main{max-width:520px;margin:12vh auto;padding:0 16px}"
            "h1{font-size:20px;margin:0 0 12px}p{margin:0 0 10px}"
            "a{display:inline-block;margin-top:8px;padding:6px 12px;border:1px solid var(--line);"
            "border-radius:8px;color:inherit;text-decoration:none}"
            f"</style></head><body><main><h1>{title}</h1>"
            + "".join(lines)
            + f"<a href='{esc(href)}' target='_top'>Open the app page</a></main></body></html>")


def install_dest(slug: str, row: dict) -> str | None:
    """Where installing `slug` lands (or the copy it updates): the existing
    copy's folder, else ``app/<repo>`` as the link installer names it
    (appfile._slug of the repo's name). For a sentence on a card (the bots'
    approval), not a precondition: the installer decides for real."""
    copy = installed_copies().get(slug)
    if copy:
        return copy["path"]
    repo = row_repo(row)
    if not repo:
        return None
    from fused_render import appfile

    return os.path.join(install_root(), appfile._slug(repo.split("/", 1)[1]))
