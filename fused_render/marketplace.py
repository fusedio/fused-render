"""Backend for the Marketplace (the shell's /marketplace pages).

A browsable catalog of community apps served by the udf.ai marketplace API,
with one-click install into the user's workspace (``<workspace>/app/
<slug>``, the same tag folder the `.fused` installer uses) and in-place update. Deliberately SELF-CONTAINED — this module plus
the thin router in server/routers/marketplace.py, a pref
(`shell/prefs.marketplace_enabled`) and the shell routes.

It is NOT community.py (the /apps hub's Showcase tab). That one clones the
repo INTO the workspace as the user's editable tree and never syncs it again;
this one asks the API for a catalog and a signed release package, and stamps
what it wrote so a later update can tell the author's files from the user's.

**The API** is a set of GET endpoints under `api_base()` (the constant
`API_BASE`, overridable by `FUSED_RENDER_MARKETPLACE_API`, read per call).
Every request carries `format=json` (the default is HTML) and every answer is
a JSON list of rows; an error is HTTP 200 with ONE row `{error_code,
error_message}`. `_udf_get` is the single seam: it adds `format=json`, goes
through `_http_get` (never raises) and raises `SourceError` — `error_code`
kept on the exception — for a transport failure, a non-list body or an error
row. Endpoints used: `marketplace_catalog` (one row per app, carrying its
`release_id`), `marketplace_categories`, `marketplace_collections` (the home
shelves), `marketplace_search`, `marketplace_app_detail` (readme, files,
releases) and `marketplace_release` (the file list with sha256 plus a
presigned tar.gz `package_url`). `client_version` is `fused_render.__version__`.

**State dir** is ``~/.fused-render/marketplace/`` (FUSED_RENDER_HOME honoured)
— UNNESTED by branch ref, exactly like community.STATE_DIR: the catalog and
asset cache describe a remote service, not this checkout, so parallel branches
share one copy. What IS per-setup is the workspace the apps install into
(`seed.fused_dir()`), read per call.

  catalog.json                the last snapshot + fetched_at/checked_at/stale
  assets/<release_id>/<slug>/  bytes served by /api/marketplace/asset

**Snapshot** = {source: "udf-api", api, catalog_version, generated_at,
apps: {slug: catalog row}, categories: [..], collections: [..]} — one build
makes three API calls. A catalog.json written by the earlier GitHub-backed
code (no `source == "udf-api"`) is treated as no cache.

**Cache policy.** A catalog younger than 10 minutes (by `checked_at`, the last
ATTEMPT — so an offline machine backs off too instead of re-trying the network
on every page view, and a crashing build likewise) is served as-is unless
`refresh=1`; a `refresh=1` within 60 s of the last attempt is served the cache
too. A refresh that fails serves the old cache with `stale: true` and a
warning; with no cache at all the answer is `{status: "error", message}`.
Only GET /catalog (`get_snapshot`) rebuilds an OLD cache; assets, detail,
install and installed (`cached_snapshot`) take any cache regardless of age,
without the build lock, and build only when there is none — a rebuild never
holds up an <img>.

**Assets.** The catalog's icon/preview URLs are raw.githubusercontent.com
URLs pinned to the app's commit; they are served through our own proxy
(`/api/marketplace/asset/<slug>/<rel>?v=<release_id[:12]>`) so the browser
never talks to GitHub and a readme's relative images resolve against the same
base. The proxy fetches only under `https://raw.githubusercontent.com/<owner>/
<repo>/<commit>/<slug>/` derived from the snapshot row, and only for a path
that is the row's own icon or preview or one the app's detail lists (the
detail row is memoised per (slug, release_id) in `_DETAIL_MEMO`, so the page
and the proxy share one API call).

**Install / update** run as background jobs (one per slug, idempotent while
running; finished jobs linger 60 s for the UI's success state). `_stage` asks
`marketplace_release` for the snapshot's `release_id`, refuses a revoked or
moved-on release, streams the presigned tar.gz to a temp file (size and
sha256 verified), extracts it with a hand-rolled safe walk (regular files
only; no absolute, `..`, link or device members) and checks the extracted set
equals the release's `files` list path-for-path, byte-for-byte (sha256+size).
Files are staged in a dot-dir inside `app/` (same filesystem, so a new
install is one rename and never flashes half-written in the explorer), then:
  new     → rename into `app/<slug>`
  update  → merged over the copy (appfile._merge_tree — the hardened walk the
            `.fused` installer uses): app files replace their counterparts;
            `.venv`, `.fused`, `.git` and every file the app does not track
            (the user's data) stay. Files the OLD stamp tracked that the new
            version dropped are removed only when their bytes still match the
            old stamp (the author's deletion, not the user's edit).
The stamp `<app>/.fused/marketplace.json` = {slug, version, release_id,
commit, api, installed_at, source, files{path: sha256}} is what "installed"
means: the catalog finds installs by globbing `app/*/.fused/
marketplace.json`, reports `modified` when a stamped file's bytes changed, and
`update_available` when the catalog's release_id differs from the stamp's (a
stamp from before releases, holding only a tree sha, therefore reads as
updatable). A HUSK (a folder holding only `.fused/`, what a keep-data
uninstall leaves) is not installed, but an install into it merges like an
update, so its data is reused. An app whose paths differ only by case is
refused (they would collide on macOS).
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
import tarfile
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlencode

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://udf.ai/fc_1g1McHwVfq5S6gNL43FxBp"
RAW_BASE = "https://raw.githubusercontent.com"
SOURCE = "udf-api"
#: How many "new and updated" apps the home shelf asks the API for.
NEW_AND_UPDATED_LIMIT = 12

#: A cached catalog younger than this (since the last attempt) is served as-is.
CACHE_TTL_S = 10 * 60
#: A forced `refresh=1` within this long of the last attempt serves the cache
#: (a reload-hammering user must not hammer the API).
REFRESH_MIN_INTERVAL_S = 60
#: Finished jobs stay in /jobs this long, so the UI can show "done"/"error".
JOB_TTL_S = 60
#: Install staging dirs older than this are swept (an interrupted install
#: cannot clean up its own).
STAGING_TTL_S = 24 * 3600

HTTP_TIMEOUT_S = 20
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_README_BYTES = 512 * 1024
#: Hard ceiling on a release package, whatever the API claims it weighs.
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024

STAMP_NAME = "marketplace.json"
#: Never written, never deleted by an install or update — the same set the
#: `.fused` installer keeps (appfile._INSTALL_KEEP): the user's venv, the
#: app's own data dir, the copy's repo.
KEEP = frozenset({".venv", ".fused", ".git"})

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: A release id becomes a cache directory name: no separators, no dots-only.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SOURCE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/tree/([0-9a-f]{40,64})/([a-z0-9-]+)/?")


class SourceError(Exception):
    """The API could not answer; the message is user-facing. ``error_code`` is
    the API's own code (e.g. ``app_not_found``) when it sent one."""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class InstallError(Exception):
    """An install/update failed; the message is shown on the job verbatim."""


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


def install_root() -> str:
    """``<workspace>/app`` — where Marketplace apps install: the same tag
    folder a `.fused` file installs into (one place for every installed app,
    the two stamps `appfile.json`/`marketplace.json` tell them apart)."""
    from fused_render.appfile import install_dir

    return install_dir()


def api_base() -> str:
    """The API's base URL (no trailing slash); `FUSED_RENDER_MARKETPLACE_API`
    overrides the constant, read per call."""
    return (os.environ.get("FUSED_RENDER_MARKETPLACE_API") or API_BASE).strip().rstrip("/")


def is_slug(slug) -> bool:
    # The shape the community repo's CI enforces; also what keeps a crafted
    # slug from ever forming a path outside app/ or the cache.
    return isinstance(slug, str) and bool(_SLUG_RE.fullmatch(slug))


def is_sha(v) -> bool:
    """A full git object id (sha1 or sha256)."""
    return isinstance(v, str) and bool(_SHA_RE.fullmatch(v))


def _safe_rel(path) -> bool:
    """A file path we are willing to write or serve: relative, forward
    slashes, no empty/`.`/`..` segment, no NUL or backslash."""
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
        return None, f"could not reach the marketplace ({e.__class__.__name__})"
    if r.status_code == 429:
        return None, "the marketplace is rate-limiting requests"
    if r.status_code == 404:
        return None, "not found (404)"
    if r.status_code >= 400:
        return None, f"the marketplace answered {r.status_code}"
    return r, None


def _udf_get(endpoint: str, *, timeout: float = HTTP_TIMEOUT_S, **params) -> list[dict]:
    """GET `<api>/<endpoint>?format=json&...` → the list of row dicts.
    `timeout` (seconds) bounds the request; it is never sent as a parameter.
    Params that are None are dropped (an empty string is kept: the release
    endpoint reads ``release_id=`` as "latest"). Raises SourceError on a
    transport failure, a body that is not a list of objects, or an error row
    (HTTP 200, one row with `error_code`)."""
    from fused_render import __version__

    query = {k: v for k, v in params.items() if v is not None}
    query["format"] = "json"
    url = f"{api_base()}/{endpoint}?{urlencode(query)}"
    r, err = _http_get(url, headers={"Accept": "application/json",
                                     "User-Agent": f"fused-render/{__version__}"},
                       timeout=timeout)
    if err:
        raise SourceError(err)
    try:
        rows = r.json()
    except ValueError:
        raise SourceError("the marketplace answered something unexpected")
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        raise SourceError("the marketplace answered something unexpected")
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
            "collections": _group_collections(cols, apps)}


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
    return {r["release_id"] for r in snap.get("apps", {}).values()
            if isinstance(r.get("release_id"), str)}


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
    snap["fetched_at"] = _utc_now()
    snap["checked_at"] = now
    snap["stale"] = False
    snap["warning"] = None
    try:
        _write_cache(snap)
    except OSError:
        logger.warning("marketplace: could not write %s", catalog_path(), exc_info=True)
    if (not cached or snap.get("catalog_version") is None
            or cached.get("catalog_version") != snap.get("catalog_version")):
        _prune_asset_cache(_release_ids(snap))
    return snap, None


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
    if cached is not None:
        if not _CATALOG_LOCK.acquire(blocking=False):
            return cached, None  # a rebuild is in flight; it will land shortly
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
    return os.path.join(app_dir, ".fused", STAMP_NAME)


def read_stamp(app_dir: str) -> dict | None:
    try:
        with open(stamp_path(app_dir), encoding="utf-8") as f:
            stamp = json.load(f)
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


#: (path, mtime_ns, size) → sha256, so `modified` on every catalog read only
#: re-hashes files that actually changed.
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


def installed_apps() -> dict[str, dict]:
    """slug → {path, stamp} for every `app/*/.fused/marketplace.json`
    whose folder still holds the app. A keep-data uninstall
    (routers/app_uninstall._strip_app_files) removes the app's files but
    keeps `.fused/` — and the stamp with it — so a husk is NOT installed;
    it stays an install TARGET (install_app merges into it, reusing the
    data)."""
    root = install_root()
    out: dict[str, dict] = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        if name.startswith("."):
            continue
        app_dir = os.path.join(root, name)
        stamp = read_stamp(app_dir)
        if stamp is None or _is_husk(app_dir):
            continue
        slug = stamp.get("slug") if is_slug(stamp.get("slug")) else name
        out.setdefault(slug, {"path": app_dir, "stamp": stamp})
    return out


def _installed_info(rec: dict | None) -> dict | None:
    if not rec:
        return None
    stamp = rec["stamp"]
    return {
        "path": rec["path"].replace(os.sep, "/"),
        "version": stamp.get("version"),
        "release_id": stamp.get("release_id"),
        "installed_at": stamp.get("installed_at"),
        "modified": _modified(rec["path"], stamp),
    }


# ---------------------------------------------------------------------------
# AppEntry
# ---------------------------------------------------------------------------

def _str_list(v) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _opt_str(v) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _author(v) -> dict:
    if isinstance(v, str):
        return {"name": v, "github": None}
    if isinstance(v, dict):
        return {"name": _opt_str(v.get("name")), "github": _opt_str(v.get("github"))}
    return {"name": None, "github": None}


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _asset_base(slug: str, row: dict) -> str | None:
    """`https://raw.githubusercontent.com/<owner>/<repo>/<commit>/<slug>/` —
    the only place the asset proxy ever fetches from — read off the row's
    `source_url` (or, failing that, its icon/preview URL). None when the row
    names no GitHub folder for this slug."""
    m = _SOURCE_URL_RE.fullmatch(row.get("source_url") or "")
    if m and m.group(3) == slug:
        return f"{RAW_BASE}/{m.group(1)}/{m.group(2)}/{slug}/"
    for key in ("icon_url", "preview_url"):
        m = re.match(rf"({re.escape(RAW_BASE)}/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9a-f]{{40,64}}/{re.escape(slug)}/)",
                     row.get(key) or "")
        if m:
            return m.group(1)
    return None


def _asset_version(row: dict) -> str | None:
    """The `?v=` cache key catalog asset URLs carry: the release id,
    shortened (a new release is new content, so a new URL)."""
    rid = row.get("release_id")
    return rid[:12] if isinstance(rid, str) and _ID_RE.fullmatch(rid) else None


def _asset_rel(slug: str, row: dict, url) -> str | None:
    """The path of a row's raw GitHub icon/preview `url` under the app's own
    base; None when the row has none or it is not under that base."""
    base = _asset_base(slug, row)
    if not base or not isinstance(url, str) or not url.startswith(base):
        return None
    rel = unquote(url[len(base):].split("?", 1)[0].split("#", 1)[0])
    return rel if _safe_rel(rel) else None


def _proxied(slug: str, row: dict, url) -> str | None:
    """Our /api/marketplace/asset URL for a row's raw GitHub icon/preview URL;
    None when the row has none or it is not under the app's own base."""
    rel = _asset_rel(slug, row, url)
    if rel is None:
        return None
    ver = _asset_version(row)
    return f"/api/marketplace/asset/{slug}/{quote(rel, safe='/')}" + (f"?v={ver}" if ver else "")


def app_entry(slug: str, row: dict, installed: dict | None) -> dict:
    """One AppEntry (the contract's shape) out of an API catalog row."""
    inst = _installed_info(installed)
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
        "release_id": _opt_str(row.get("release_id")),
        "commit": _opt_str(row.get("commit")),
        "published_at": _opt_str(row.get("published_at")),
        "updated_at": _opt_str(row.get("updated_at")),
        "source_url": _opt_str(row.get("source_url")),
        "featured": row.get("featured") is True,
        "installed": inst,
        "update_available": bool(inst and inst.get("release_id") != row.get("release_id")),
        "compatible": _compatible(row.get("min_fused_render")),
    }


def catalog(refresh: bool = False) -> dict:
    """GET /api/marketplace/catalog's payload."""
    snap, error = get_snapshot(refresh=refresh)
    if snap is None:
        return {"status": "error", "message": error, "apps": [], "categories": [],
                "collections": []}
    installed = installed_apps()
    apps = snap["apps"]
    entries = [app_entry(slug, row, installed.get(slug)) for slug, row in sorted(apps.items())]
    collections = [{"id": c.get("id"), "kind": c.get("kind"), "title": c.get("title") or "",
                    "slugs": [s for s in c.get("slugs") or [] if s in apps]}
                   for c in snap.get("collections") or [] if isinstance(c, dict)]
    return {
        "status": "ok",
        "source": snap.get("source"),
        "api": snap.get("api"),
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
    AppEntry carrying its `score` and `highlights`. Installed stamps are
    merged in like the catalog's. SourceError → `{status: "error", message}`."""
    from fused_render import __version__

    try:
        rows = _udf_get("marketplace_search", q=q or "", category=category or None,
                        cursor=cursor or None, limit=limit, client_version=__version__)
    except SourceError as exc:
        return {"status": "error", "message": str(exc)}
    installed = installed_apps()
    snap, _err = cached_snapshot()
    catalog_apps = (snap or {}).get("apps") or {}
    apps = []
    for row in rows:
        slug = row.get("slug")
        if not is_slug(slug):
            continue
        # The catalog's row underneath: a search row without `release_id`
        # must not read an installed current app as updatable.
        entry = app_entry(slug, {**(catalog_apps.get(slug) or {}), **row}, installed.get(slug))
        entry["score"] = row.get("score") if isinstance(row.get("score"), (int, float)) else 0
        entry["highlights"] = [{"field": str(h.get("field") or ""), "snippet": str(h.get("snippet") or "")}
                               for h in row.get("highlights") or [] if isinstance(h, dict)]
        apps.append(entry)
    nxt = next((r.get("next_cursor") for r in rows if r.get("next_cursor")), None)
    return {"status": "ok", "apps": apps, "next_cursor": nxt if isinstance(nxt, str) else None}


def _local_entry(slug: str, rec: dict) -> dict:
    """An AppEntry for an install the catalog no longer lists, from what the
    stamp and the copy's own metadata.json know."""
    stamp = rec["stamp"]
    try:
        with open(os.path.join(rec["path"], "metadata.json"), "rb") as f:
            meta = _parse_metadata(f.read()) or {}
    except OSError:
        meta = {}
    paths = [rel for rel in sorted(stamp.get("files") or {}) if _safe_rel(rel)]
    size = 0
    for rel in paths:
        try:
            size += os.path.getsize(os.path.join(rec["path"], *rel.split("/")))
        except OSError:
            pass
    # Whatever the copy's metadata declares, minus anything that would point
    # at the catalog: there is no catalog record to serve assets from, nothing
    # to update to.
    row = {**{k: v for k, v in meta.items() if k not in ("icon_url", "preview_url", "source_url",
                                                         "featured", "release_id", "commit")},
           "size_bytes": size, "file_count": len(paths)}
    entry = app_entry(slug, row, rec)
    entry.update(update_available=False, featured=False)
    if not meta.get("version") and stamp.get("version"):
        entry["version"] = stamp["version"]
    return entry


def installed_list() -> dict:
    """GET /api/marketplace/installed — every stamped install, catalog-backed
    where the catalog (cached; never forces a network refresh) still has it."""
    snap, _error = cached_snapshot()
    apps = (snap or {}).get("apps") or {}
    out = []
    for slug, rec in sorted(installed_apps().items()):
        if slug in apps:
            out.append(app_entry(slug, apps[slug], rec))
        else:
            out.append(_local_entry(slug, rec))
    return {"apps": out}


# ---------------------------------------------------------------------------
# acknowledgements
# ---------------------------------------------------------------------------
#
# ``<state_dir>/acks.json`` = ``{"python": ["<slug>", ...]}``: the apps whose
# "this app runs Python on your machine" notice the user has accepted, so the
# install flow asks once per app, not once per install. It was the browser's
# localStorage (``fused-render:marketplace:python-ack``) until D1377: storage
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
        raise ValueError("not a marketplace slug")
    with _ACKS_LOCK:
        slugs = python_acks()
        if slug in slugs:
            return slugs
        slugs = sorted({*slugs, slug})
        os.makedirs(state_dir(), exist_ok=True)
        # Unique per writer, like the catalog cache: other worktrees' servers
        # share this state dir (it is not nested by branch).
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
    does not list it). Memoised under the snapshot row's (slug, release_id),
    and only while the answer is that very release. Raises SourceError."""
    key = (slug, snap_row.get("release_id") if snap_row else None)
    if snap_row is not None:
        hit = _memo_get(key)
        if hit is not None:
            return hit
    from fused_render import __version__

    rows = _udf_get("marketplace_app_detail", timeout=DETAIL_TIMEOUT_S,
                    slug=slug, client_version=__version__)
    row = next((r for r in rows if r.get("slug") == slug), None)
    if row is not None and snap_row is not None and row.get("release_id") == key[1]:
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
    release's commit), or None."""
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
    if version is None or base is None:
        return None  # the release id is a cache directory name; never trust it unshaped
    rid = row["release_id"]
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


def _installed_detail(slug: str, rec: dict) -> dict:
    """Installed but gone from the catalog: describe the local copy."""
    entry = _local_entry(slug, rec)
    readme = ""
    for name in ("readme.md", "README.md", "Readme.md"):
        try:
            with open(os.path.join(rec["path"], name), "rb") as f:
                readme = f.read(MAX_README_BYTES).decode("utf-8", "replace")
            break
        except OSError:
            continue
    files = [{"path": p, "size": 0} for p in sorted(rec["stamp"].get("files") or {})]
    return {"status": "ok", "app": entry, "readme": readme, "files": files,
            "readme_base": f"/api/marketplace/asset/{slug}/", "releases": []}


def app_detail(slug: str) -> dict | None:
    """GET /api/marketplace/app/{slug}'s payload, or None (→ 404)."""
    if not is_slug(slug):
        return None
    installed = installed_apps()
    snap, _error = cached_snapshot()
    snap_row = ((snap or {}).get("apps") or {}).get(slug)
    try:
        row = _detail_row(slug, snap_row)
    except SourceError as exc:
        if exc.error_code != "app_not_found" and snap_row is not None:
            # The API is unreachable, but the cached catalog knows the app:
            # the card still renders; only the readme and file list are lost.
            return {"status": "ok", "app": app_entry(slug, snap_row, installed.get(slug)),
                    "readme": "", "files": [], "readme_base": f"/api/marketplace/asset/{slug}/",
                    "releases": []}
        row = None
    if row is not None:
        files = [{"path": f["path"], "size": _int(f.get("size"))}
                 for f in row.get("files") or []
                 if isinstance(f, dict) and isinstance(f.get("path"), str)]
        releases = [{"release_id": r.get("release_id"), "version": r.get("version"),
                     "changelog": r.get("changelog") or "", "published_at": r.get("published_at"),
                     "revoked": bool(r.get("revoked"))}
                    for r in row.get("releases") or [] if isinstance(r, dict)]
        readme = row.get("readme_markdown")
        return {"status": "ok", "app": app_entry(slug, {**(snap_row or {}), **row}, installed.get(slug)),
                "readme": readme[:MAX_README_BYTES] if isinstance(readme, str) else "",
                "files": files, "readme_base": f"/api/marketplace/asset/{slug}/",
                "releases": releases}
    if slug in installed:
        return _installed_detail(slug, installed[slug])
    return None


# ---------------------------------------------------------------------------
# install / update jobs
# ---------------------------------------------------------------------------

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


def start_install(slug: str) -> dict:
    """Start installing (or updating, when a stamped copy exists) `slug` in
    the background; answers the job. Idempotent while one is running.
    Raises InstallError for a request that cannot start (bad/unknown slug)."""
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
    if slug not in snap["apps"]:
        raise InstallError(f"no app named {slug!r} in the catalog")
    dest = os.path.join(install_root(), slug)
    action = "update" if read_stamp(dest) and not _is_husk(dest) else "install"
    # An estimate (the unpacked size); install_app swaps in the package's size.
    total = _int(snap["apps"][slug].get("size_bytes"))
    with _JOBS_LOCK:
        job = _JOBS.get(slug)
        if job and job["state"] in _ACTIVE:
            return dict(job)
        job = {"slug": slug, "action": action, "state": "queued", "progress": 0.0,
               "bytes_done": 0, "bytes_total": total, "message": "Waiting to start…",
               "path": None, "error": None}
        _JOBS[slug] = job
        _JOB_FINISHED.pop(slug, None)
        out = dict(job)
    threading.Thread(target=_run_job, args=(slug, snap), daemon=True,
                     name=f"marketplace-{action}-{slug}").start()
    return out


def _is_husk(app_dir: str) -> bool:
    """A folder holding nothing but `.fused/` (what a keep-data uninstall
    leaves): the place the app's data waits for the next install."""
    try:
        names = [n for n in os.listdir(app_dir) if n != ".DS_Store"]
    except OSError:
        return False
    return names == [".fused"]


def _sweep_staging(root: str) -> None:
    try:
        names = os.listdir(root)
    except OSError:
        return
    cutoff = time.time() - STAGING_TTL_S
    for n in names:
        if not n.startswith(".marketplace-"):
            continue
        p = os.path.join(root, n)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue


def _kept(rel: str) -> bool:
    """Whether `rel` lies under a KEEP name — case-insensitively, since on a
    case-insensitive filesystem (macOS by default) `.FUSED/x` IS `.fused/x`."""
    return rel.split("/", 1)[0].lower() in KEEP


def _wanted(files: list[dict]) -> list[dict]:
    """The files an install writes: safe paths, nothing under a KEEP name."""
    return [f for f in files if _safe_rel(f["path"]) and not _kept(f["path"])]


def _case_collisions(files: list[dict]) -> list[str]:
    """Paths (or folder prefixes) of the list that differ only by case —
    `README.md` + `readme.md`, `Lib/a.js` + `lib/b.js`. On a case-insensitive
    filesystem they land on ONE file/folder, so one silently overwrites the
    other and the stamp's hashes stop describing the disk."""
    seen: dict[str, str] = {}
    clashes: set[str] = set()
    for f in files:
        segs = f["path"].split("/")
        for i in range(1, len(segs) + 1):
            name = "/".join(segs[:i])
            prev = seen.setdefault(name.casefold(), name)
            if prev != name:
                clashes.add(f"{prev} / {name}")
    return sorted(clashes)


def _refuse_case_collisions(files: list[dict]) -> None:
    clashes = _case_collisions(files)
    if clashes:
        raise InstallError("this app has files whose names differ only by upper/lower case "
                           f"({clashes[0]}), which cannot be installed safely")


def _rebuild_after_drift() -> None:
    """The release the user's page named is gone or moved on: rebuild the
    catalog now, past the refresh throttle, so the "refresh" the error asks for
    shows the new release. A failed rebuild changes nothing (the caller's
    InstallError still raises)."""
    try:
        get_snapshot(force=True)
    except Exception:  # noqa: BLE001
        logger.debug("marketplace: catalog rebuild after a release change failed", exc_info=True)


def _fetch_release(slug: str, release_id: str | None) -> dict:
    """The `marketplace_release` row for `release_id` (the one the user chose
    from the catalog), validated: not revoked, the release asked for, with a
    well-formed file list and package. Raises InstallError."""
    try:
        rows = _udf_get("marketplace_release", slug=slug, release_id=release_id or "")
    except SourceError as exc:
        raise InstallError(str(exc))
    rel = rows[0] if rows else None
    if not isinstance(rel, dict):
        raise InstallError("the marketplace returned no release for this app")
    if rel.get("revoked"):
        _rebuild_after_drift()
        raise InstallError("This release was withdrawn — refresh the Marketplace")
    if release_id and rel.get("release_id") != release_id:
        _rebuild_after_drift()
        raise InstallError("the marketplace changed since this page loaded — "
                           "refresh and try again")
    files = rel.get("files")
    if not isinstance(files, list) or not files or not all(
            isinstance(f, dict) and _safe_rel(f.get("path")) and isinstance(f.get("sha256"), str)
            and isinstance(f.get("size"), int) and f["size"] >= 0 for f in files):
        raise InstallError("the marketplace returned an unusable file list for this release")
    if len({f["path"] for f in files}) != len(files):
        raise InstallError("the release lists the same file twice")
    url, size, digest = rel.get("package_url"), rel.get("package_size"), rel.get("package_sha256")
    if (not isinstance(url, str) or not url.startswith("https://") or not isinstance(digest, str)
            or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES):
        raise InstallError("the marketplace returned an unusable package for this release")
    return rel


def _download_package(url: str, dest: str, progress, max_bytes: int) -> None:
    """Stream the presigned package at `url` to `dest`, calling
    `progress(n)` per chunk. Raises InstallError (also past `max_bytes`)."""
    got = 0
    try:
        with httpx.stream("GET", url, timeout=HTTP_TIMEOUT_S * 3, follow_redirects=True) as r:
            if r.status_code >= 400:
                raise InstallError(f"the package download failed ({r.status_code})")
            with open(dest, "wb") as out:
                for chunk in r.iter_bytes(256 * 1024):
                    got += len(chunk)
                    if got > max_bytes:
                        raise InstallError("the downloaded package is larger than announced")
                    out.write(chunk)
                    progress(len(chunk))
    except httpx.HTTPError as e:
        raise InstallError(f"could not download the package ({e.__class__.__name__})")


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_package(tar_path: str, payload: str, files: list[dict]) -> None:
    """Unpack the release package into `payload`, refusing anything but plain
    files and folders under safe relative paths (no absolute, `..`, symlink,
    hardlink, device or duplicate member), then require the extracted set to
    EQUAL the release's `files` — same paths, same size and sha256 each.
    Only `_wanted` files are written (nothing under a KEEP name). Hand-rolled
    rather than `extractall`: every member is vetted before a byte lands.
    Raises InstallError."""
    expected = {f["path"]: f for f in files}
    wanted = {f["path"] for f in _wanted(files)}
    seen: set[str] = set()
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            for m in tf:
                name = m.name[2:] if m.name.startswith("./") else m.name
                if m.isdir():
                    if name.rstrip("/") and not _safe_rel(name.rstrip("/")):
                        raise InstallError(f"the package holds an unsafe path ({m.name!r})")
                    continue
                if not m.isreg():
                    raise InstallError(f"the package holds something that is not a plain file ({m.name!r})")
                if not _safe_rel(name):
                    raise InstallError(f"the package holds an unsafe path ({m.name!r})")
                if name not in expected:
                    raise InstallError(f"the package holds a file the release does not list ({name})")
                if name in seen:
                    raise InstallError(f"the package holds {name} twice")
                seen.add(name)
                if m.size != expected[name]["size"]:
                    raise InstallError(f"{name} does not match the release (size)")
                src = tf.extractfile(m)
                if src is None:
                    raise InstallError(f"the package holds an unreadable member ({name})")
                h = hashlib.sha256()
                out = None
                if name in wanted:
                    dest = os.path.join(payload, *name.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    out = open(dest, "wb")
                try:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        h.update(chunk)
                        if out is not None:
                            out.write(chunk)
                finally:
                    if out is not None:
                        out.close()
                if h.hexdigest() != expected[name]["sha256"]:
                    raise InstallError(f"{name} does not match the release (checksum)")
    except (tarfile.TarError, EOFError, zlib.error, UnicodeError):
        raise InstallError("the downloaded package is not a valid archive")
    except OSError as exc:
        if exc.errno is None:  # gzip.BadGzipFile and friends; not the disk
            raise InstallError("the downloaded package is not a valid archive")
        raise  # a real disk error: install_app words it as a write failure
    missing = sorted(set(expected) - seen)
    if missing:
        raise InstallError(f"the package is missing {missing[0]} ({len(missing)} file(s))")


def _stage(release: dict, staging: str, payload: str, progress) -> list[dict]:
    """Download the release package, verify it, and unpack it into `payload`.
    Answers the `{path, sha256, size}` files actually staged."""
    pkg = os.path.join(staging, "package.tar.gz")
    _download_package(release["package_url"], pkg, progress, release["package_size"])
    try:
        if os.path.getsize(pkg) != release["package_size"]:
            raise InstallError("the downloaded package has the wrong size")
        if _file_sha256(pkg) != release["package_sha256"]:
            raise InstallError("the downloaded package failed its integrity check")
        _extract_package(pkg, payload, release["files"])
    finally:
        with contextlib.suppress(OSError):
            os.remove(pkg)
    return _wanted(release["files"])


def _prune_dropped(dest: str, old_stamp: dict | None, new_paths: set[str]) -> list[str]:
    """Files the OLD stamp tracked that the new version no longer ships,
    removed only while their bytes still match that stamp (the author deleted
    them; the user never touched them). Empty parent dirs go too."""
    if not old_stamp or not isinstance(old_stamp.get("files"), dict):
        return []
    pruned = []
    for rel, digest in sorted(old_stamp["files"].items()):
        if rel in new_paths or not _safe_rel(rel) or _kept(rel):
            continue
        p = os.path.join(dest, *rel.split("/"))
        try:
            if os.path.islink(p) or not os.path.isfile(p) or _sha256_file(p) != digest:
                continue
            os.remove(p)
            pruned.append(rel)
            d = os.path.dirname(p)
            while os.path.normcase(os.path.abspath(d)) != os.path.normcase(os.path.abspath(dest)):
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)
        except OSError:
            continue
    return pruned


def _write_stamp(dest: str, stamp: dict) -> None:
    os.makedirs(os.path.dirname(stamp_path(dest)), exist_ok=True)
    tmp = stamp_path(dest) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
        f.write("\n")
    os.replace(tmp, stamp_path(dest))


def install_app(slug: str, snap: dict, report=lambda **kw: None) -> dict:
    """Install or update `slug` from `snap` into `app/<slug>`,
    synchronously. `report(**job_fields)` receives progress. Answers
    {action, path, stamp, pruned}. Raises InstallError."""
    from fused_render import appfile

    root = install_root()
    os.makedirs(root, exist_ok=True)
    _sweep_staging(root)
    dest = os.path.join(root, slug)
    old_stamp = read_stamp(dest)
    exists = os.path.lexists(dest)
    if exists and (os.path.islink(dest) or not os.path.isdir(dest)):
        raise InstallError(f"{dest} exists and is not a folder — move it aside and try again")
    if exists and old_stamp is None and not _is_husk(dest):
        raise InstallError(f"{dest} already exists and was not installed from the Marketplace "
                           "— rename or move it, then try again")
    if old_stamp is not None and old_stamp.get("slug") not in (None, slug):
        raise InstallError(f"{dest} holds a different Marketplace app "
                           f"({old_stamp.get('slug')})")
    # A husk (keep-data uninstall) with our stamp is merged into like an
    # update — its `.fused/` data is reused — but to the user it is an install.
    action = "update" if old_stamp and not _is_husk(dest) else "install"
    row = snap["apps"][slug]
    report(state="downloading", action=action, progress=0.0, message="Downloading…")
    release = _fetch_release(slug, row.get("release_id"))
    _refuse_case_collisions(release["files"])
    total = release["package_size"]
    done = [0]
    lock = threading.Lock()

    def progress(n):
        with lock:
            done[0] += n
            report(bytes_done=done[0], progress=round(0.9 * min(done[0] / total, 1.0), 4))

    report(bytes_total=total, bytes_done=0)
    staging = tempfile.mkdtemp(dir=root, prefix=f".marketplace-{slug}-")
    payload = os.path.join(staging, "files")
    os.makedirs(payload)
    try:
        staged = _stage(release, staging, payload, progress)
        report(state="installing", progress=0.92,
               message="Updating…" if action == "update" else "Installing…")
        hashes = {f["path"]: f["sha256"] for f in staged}
        version = release.get("version") if isinstance(release.get("version"), str) else row.get("version")
        stamp = {
            "slug": slug,
            "version": version if isinstance(version, str) else None,
            "release_id": release.get("release_id"),
            "commit": release.get("commit") if isinstance(release.get("commit"), str) else row.get("commit"),
            "api": snap.get("api") or api_base(),
            "installed_at": _utc_now(),
            "source": SOURCE,
            "files": hashes,
        }
        if not exists:
            # The stamp rides in with the payload, so the folder appears
            # already owned: a crash after the rename can never leave app
            # files the Marketplace refuses to touch as "not installed by us".
            _write_stamp(payload, stamp)
            try:
                os.rename(payload, dest)
            except OSError:
                if not os.path.isdir(dest) or read_stamp(dest) is not None:
                    raise InstallError(f"could not place the app at {dest}")
                appfile._merge_tree(payload, dest, skip=KEEP)
            pruned = []
        else:
            # The hardened merge the `.fused` installer uses: replaces the
            # app's files, never enters a symlink, leaves everything it does
            # not name (the user's files) and the KEEP set alone.
            appfile._merge_tree(payload, dest, skip=KEEP)
            pruned = _prune_dropped(dest, old_stamp, set(hashes))
    except OSError as exc:
        raise InstallError(f"could not write the app ({exc.strerror or exc})")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    try:
        from fused_render import app_fused_dir

        app_fused_dir.ensure(dest)
    except Exception:  # noqa: BLE001 — scaffolding is a nicety, the stamp is not
        logger.debug("marketplace: .fused scaffold failed for %s", dest, exc_info=True)
    _write_stamp(dest, stamp)
    # The /apps hub's snapshot lags the disk by up to 20 s; never on the one
    # folder just installed (same reset the `.fused` installer does).
    try:
        from fused_render.server.routers import apps as apps_router

        apps_router._reset_snapshot()
    except Exception:  # noqa: BLE001
        logger.debug("marketplace: apps snapshot reset failed", exc_info=True)
    return {"action": action, "path": dest, "stamp": stamp, "pruned": pruned}


def _run_job(slug: str, snap: dict) -> None:
    def report(**fields):
        _update_job(slug, **fields)

    try:
        result = install_app(slug, snap, report)
        name = snap["apps"][slug].get("name") or slug
        verb = "Updated" if result["action"] == "update" else "Installed"
        with _JOBS_LOCK:
            total = (_JOBS.get(slug) or {}).get("bytes_total") or 0
        _update_job(slug, state="done", progress=1.0, bytes_done=total,
                    path=result["path"].replace(os.sep, "/"),
                    message=f"{verb} {name}")
    except InstallError as exc:
        _update_job(slug, state="error", error=str(exc), message=str(exc))
    except Exception as exc:  # noqa: BLE001 — a job must always finish
        logger.exception("marketplace: install of %s crashed", slug)
        msg = f"install failed ({exc.__class__.__name__}: {exc})"
        _update_job(slug, state="error", error=msg, message=msg)
