"""Backend for the Marketplace (the shell's /marketplace pages) — TEMPORARY.

A browsable catalog of the community apps in
github.com/fusedio/fused-render-community-apps, with one-click install into
the user's workspace (``<workspace>/community/<slug>``) and in-place update.
A real backend (semantic search, server-side catalog) replaces this; until
then it is deliberately SELF-CONTAINED — this module plus the thin router in
server/routers/marketplace.py, a pref (`shell/prefs.marketplace_enabled`) and
the shell routes — so deleting it is deleting two files and a few hooks.

It is NOT community.py (the /apps hub's Showcase tab). That one clones the
repo INTO the workspace as the user's editable tree and never syncs it again;
this one keeps its own private mirror/cache under the state dir and copies one
app at a time out of it, stamping what it wrote so a later update can tell the
author's files from the user's.

**Two sources behind one interface** (`_build_snapshot`), both producing the
same "snapshot" — {source, repo, branch, commit, apps: {slug: {meta, files,
tree_sha}}}:

  github-api  (default) — `GET /repos/{o}/{r}/commits/{branch}` for the head
              sha, `GET /git/trees/{sha}?recursive=1` for every path, size and
              per-folder tree sha in one call, then each app's metadata.json
              from raw.githubusercontent.com pinned AT that sha (parallel).
              Honours GITHUB_TOKEN (sent to api.github.com only). Unauthenticated
              GitHub allows 60 req/h/IP; a catalog costs 2 of them and is cached
              for 10 minutes, so ordinary browsing never gets near it.
  git-mirror  (fallback when the API errors — 403, 429, offline — or forced by
              FUSED_RENDER_MARKETPLACE_SOURCE=git) — a shallow single-branch
              clone at <state>/mirror, refreshed with `fetch --depth 1` +
              `reset --hard FETCH_HEAD`. The tree shas it reports are git's own
              (`ls-tree HEAD`), i.e. the SAME values the API reports for the
              same commit, so an install made through one source compares
              cleanly against a catalog read through the other.

The repo is `FUSED_RENDER_MARKETPLACE_REPO` (``owner/repo`` on GitHub, or any
git URL/path — the latter is git-mirror only, which is how the tests point it
at a local bare repo) and `FUSED_RENDER_MARKETPLACE_BRANCH` (default main).
Both are read per call, never frozen at import.

**State dir** is ``~/.fused-render/marketplace/`` (FUSED_RENDER_HOME honoured)
— UNNESTED by branch ref, exactly like community.STATE_DIR: the catalog,
mirror and asset cache describe a remote repo, not this checkout, so parallel
branches share one copy instead of each cloning their own. What IS per-setup is
the workspace the apps install into (`seed.fused_dir()`), read per call.

  catalog.json             the last snapshot + fetched_at/checked_at/stale
  mirror/                  the git-mirror clone (only once that source ran)
  assets/<commit>/<slug>/  bytes served by /api/marketplace/asset, per commit

**Cache policy.** A catalog younger than 10 minutes (by `checked_at`, the last
ATTEMPT — so an offline machine backs off too instead of re-trying the network
on every page view) is served as-is unless `refresh=1`. A refresh that fails on
both sources serves the old cache with `stale: true` and a warning; with no
cache at all the answer is `{status: "error", message}`.

**Install / update** run as background jobs (one per slug, idempotent while
running; finished jobs linger 60 s for the UI's success state). Files are
staged in a dot-dir inside `community/` (same filesystem, so a new install is
one rename and never flashes half-written in the explorer), then:
  new     → rename into `community/<slug>`
  update  → merged over the copy (appfile._merge_tree — the hardened walk the
            `.fused` installer uses): app files replace their counterparts;
            `.venv`, `.fused`, `.git` and every file the app does not track
            (the user's data) stay. Files the OLD stamp tracked that the new
            version dropped are removed only when their bytes still match the
            old stamp (the author's deletion, not the user's edit).
The stamp `<app>/.fused/marketplace.json` = {slug, version, tree_sha, commit,
repo, installed_at, source, files{path: sha256}} is what "installed" means:
the catalog finds installs by globbing `community/*/.fused/marketplace.json`,
reports `modified` when a stamped file's bytes changed, and `update_available`
when the catalog's tree sha differs from the stamp's.

Every git call: an ABSOLUTE git (`git_tools.git_bin()`), `-C` instead of
`cwd=`, `close_fds=False` — the posix_spawn rule (see community.py and
tests/test_git_posix_spawn.py) — plus GIT_TERMINAL_PROMPT=0 and a bounded
timeout. Every HTTP call goes through `_http_get`, which never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

DEFAULT_REPO = "fusedio/fused-render-community-apps"
DEFAULT_BRANCH = "main"
API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

SOURCE_API = "github-api"
SOURCE_GIT = "git-mirror"

#: A cached catalog younger than this (since the last attempt) is served as-is.
CACHE_TTL_S = 10 * 60
#: Finished jobs stay in /jobs this long, so the UI can show "done"/"error".
JOB_TTL_S = 60
#: Install staging dirs older than this are swept (an interrupted install
#: cannot clean up its own).
STAGING_TTL_S = 24 * 3600
#: Parallel raw downloads (metadata during a catalog build, files during an
#: install).
WORKERS = 8

HTTP_TIMEOUT_S = 20
GIT_TIMEOUT_S = 60
CLONE_TIMEOUT_S = 240
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_README_BYTES = 512 * 1024

STAMP_NAME = "marketplace.json"
COMMUNITY_DIRNAME = "community"
#: Never written, never deleted by an install or update — the same set the
#: `.fused` installer keeps (appfile._INSTALL_KEEP): the user's venv, the
#: app's own data dir, the copy's repo.
KEEP = frozenset({".venv", ".fused", ".git"})

#: Category chip order; anything else follows alphabetically.
CATEGORY_ORDER = ("starters", "local-ai", "productivity", "geospatial")
CATEGORY_LABELS = {
    "starters": "Starters",
    "local-ai": "Local AI",
    "productivity": "Productivity",
    "geospatial": "Geospatial",
}
#: Featured when no app's metadata declares `featured` (missing ones skipped).
DEFAULT_FEATURED = ("local-chat", "overture-explorer", "local-transcription",
                    "hexagon-lab", "midtown-mayhem")

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_GH_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_GH_URL_RE = re.compile(r"(?:https?://|git@)github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class SourceError(Exception):
    """A source could not produce a snapshot; the message is user-facing."""


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


def mirror_dir() -> str:
    return os.path.join(state_dir(), "mirror")


def catalog_path() -> str:
    return os.path.join(state_dir(), "catalog.json")


def community_dir() -> str:
    """``<workspace>/community`` — where Marketplace apps install."""
    from fused_render.shell.seed import fused_dir

    return os.path.join(fused_dir(), COMMUNITY_DIRNAME)


def repo_config() -> dict:
    """The configured repo: ``{repo, gh, clone_url, branch}``. ``gh`` is the
    GitHub ``owner/repo`` when there is one (the API source and the
    `source_url` links need it), else None — a local path or non-GitHub URL,
    which only the git-mirror source can read."""
    raw = (os.environ.get("FUSED_RENDER_MARKETPLACE_REPO") or DEFAULT_REPO).strip()
    branch = (os.environ.get("FUSED_RENDER_MARKETPLACE_BRANCH") or DEFAULT_BRANCH).strip()
    if _GH_SLUG_RE.fullmatch(raw) and not os.path.exists(raw):
        gh = raw
        clone_url = f"https://github.com/{gh}.git"
    else:
        m = _GH_URL_RE.match(raw)
        gh = m.group(1) if m else None
        clone_url = raw
    return {"repo": gh or raw, "gh": gh, "clone_url": clone_url, "branch": branch}


def forced_source() -> str | None:
    v = (os.environ.get("FUSED_RENDER_MARKETPLACE_SOURCE") or "").strip().lower()
    if v in ("git", SOURCE_GIT):
        return SOURCE_GIT
    if v in ("api", SOURCE_API):
        return SOURCE_API
    return None


def is_slug(slug) -> bool:
    # The shape the community repo's CI enforces; also what keeps a crafted
    # slug from ever forming a path outside community/ or the cache.
    return isinstance(slug, str) and bool(_SLUG_RE.fullmatch(slug))


def _safe_rel(path) -> bool:
    """A repo-relative file path we are willing to write or serve: relative,
    forward slashes, no empty/`.`/`..` segment, no NUL or backslash."""
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
# HTTP (never raises)
# ---------------------------------------------------------------------------

def _api_headers() -> dict:
    from fused_render import __version__

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"fused-render/{__version__}",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get(url: str, *, headers: dict | None = None, client: httpx.Client | None = None,
              timeout: float = HTTP_TIMEOUT_S) -> tuple[httpx.Response | None, str | None]:
    """One GET: (response, error). Never raises — like hub_models._get, a
    refused or unreachable GitHub is a sentence on the page (and a reason to
    fall back to the git mirror), not a 500 from this server."""
    try:
        if client is not None:
            r = client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        else:
            r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        return None, f"could not reach GitHub ({e.__class__.__name__})"
    if r.status_code == 429 or (r.status_code == 403
                                and r.headers.get("x-ratelimit-remaining") == "0"):
        return None, "GitHub API rate-limited"
    if r.status_code in (401, 403):
        return None, f"GitHub API refused the request ({r.status_code})"
    if r.status_code == 404:
        return None, "not found on GitHub (404)"
    if r.status_code >= 400:
        return None, f"GitHub answered {r.status_code}"
    return r, None


# ---------------------------------------------------------------------------
# git (absolute binary, -C not cwd=, close_fds=False — the posix_spawn rule)
# ---------------------------------------------------------------------------

def _git(*args, cwd: str | None = None, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run git, returning bytes stdout/stderr. FileNotFoundError and timeouts
    become SourceError (user-facing)."""
    from fused_render.shell import git_tools

    where = ["-C", cwd] if cwd is not None else []
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        # argv inlined (not a local) so tests/test_git_posix_spawn.py's sweep
        # can see this is git and check the posix_spawn clauses.
        return subprocess.run([git_tools.git_bin(), *where, *args], capture_output=True,
                              timeout=timeout, env=env, close_fds=False)
    except FileNotFoundError:
        raise SourceError("git is not installed (or not on PATH)")
    except subprocess.TimeoutExpired:
        raise SourceError("git timed out talking to GitHub")


def _git_out(*args, cwd: str | None = None, timeout: float = GIT_TIMEOUT_S) -> str:
    r = _git(*args, cwd=cwd, timeout=timeout)
    if r.returncode != 0:
        lines = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SourceError(f"git {args[0]} failed: {lines[-1] if lines else f'exit {r.returncode}'}")
    return r.stdout.decode("utf-8", "replace")


#: Serializes every touch of the mirror's git state within this process
#: (a catalog refresh resetting the tree while an install copies out of it).
_MIRROR_LOCK = threading.RLock()


def _ensure_mirror(*, fetch: bool) -> tuple[str, str | None]:
    """Make sure the mirror exists (shallow clone on first use) and, when
    ``fetch``, bring it to the branch tip. Answers (head sha, warning) — a
    failed FETCH over an existing mirror is not fatal: the old checkout is
    still a catalog, reported with a warning (and `stale`). Caller holds
    _MIRROR_LOCK."""
    cfg = repo_config()
    mirror = mirror_dir()
    warning = None
    if not os.path.isdir(os.path.join(mirror, ".git")):
        os.makedirs(state_dir(), exist_ok=True)
        staging = tempfile.mkdtemp(dir=state_dir(), prefix=".mirror-clone-")
        try:
            r = _git("clone", "--depth", "1", "--single-branch", "--no-tags",
                     "--branch", cfg["branch"], "--", cfg["clone_url"],
                     os.path.join(staging, "mirror"), timeout=CLONE_TIMEOUT_S)
            if r.returncode != 0:
                lines = r.stderr.decode("utf-8", "replace").strip().splitlines()
                raise SourceError("could not clone the community apps repo: "
                                  f"{lines[-1] if lines else 'clone failed'}")
            # A leftover non-repo `mirror` (interrupted earlier run) is ours.
            shutil.rmtree(mirror, ignore_errors=True)
            os.rename(os.path.join(staging, "mirror"), mirror)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    elif fetch:
        try:
            # The configured repo may have changed since the clone.
            _git_out("remote", "set-url", "origin", cfg["clone_url"], cwd=mirror)
            _git_out("fetch", "--depth", "1", "--no-tags", "origin", cfg["branch"],
                     cwd=mirror, timeout=CLONE_TIMEOUT_S)
            _git_out("reset", "--hard", "-q", "FETCH_HEAD", cwd=mirror)
            _git_out("clean", "-fdqx", cwd=mirror)
        except SourceError as exc:
            warning = f"could not update the git mirror ({exc})"
    head = _git_out("rev-parse", "HEAD", cwd=mirror).strip()
    return head, warning


def _ls_tree(ref: str, *paths: str, recursive: bool = False) -> list[tuple[str, str, int | None, str]]:
    """`git ls-tree -l -z` over the mirror: [(type, sha, size, path)]."""
    args = ["ls-tree", "-l", "-z"] + (["-r"] if recursive else []) + [ref]
    if paths:
        args += ["--", *paths]
    out = _git_out(*args, cwd=mirror_dir())
    rows = []
    for rec in out.split("\x00"):
        if not rec or "\t" not in rec:
            continue
        head, path = rec.split("\t", 1)
        parts = head.split()
        if len(parts) < 4:
            continue
        _mode, typ, sha, size = parts[:4]
        rows.append((typ, sha, int(size) if size.isdigit() else None, path))
    return rows


# ---------------------------------------------------------------------------
# sources → snapshot
# ---------------------------------------------------------------------------

def _parse_metadata(raw: bytes | str) -> dict | None:
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return meta if isinstance(meta, dict) else None


def _api_snapshot(cfg: dict) -> dict:
    gh = cfg["gh"]
    if not gh:
        raise SourceError("the configured repo is not on GitHub")
    headers = _api_headers()
    r, err = _http_get(f"{API_BASE}/repos/{gh}/commits/{cfg['branch']}", headers=headers)
    if err:
        raise SourceError(err)
    try:
        commit = r.json()["sha"]
        r, err = _http_get(f"{API_BASE}/repos/{gh}/git/trees/{commit}?recursive=1",
                           headers=headers)
        if err:
            raise SourceError(err)
        tree = r.json()
    except (ValueError, KeyError, TypeError):
        raise SourceError("GitHub answered something unexpected")
    if tree.get("truncated"):
        raise SourceError("GitHub truncated the repo listing")
    tops: dict[str, str] = {}
    files: dict[str, list] = {}
    for e in tree.get("tree") or []:
        path = e.get("path") or ""
        if e.get("type") == "tree" and "/" not in path and is_slug(path):
            tops[path] = e.get("sha")
        elif e.get("type") == "blob" and "/" in path:
            top, rel = path.split("/", 1)
            if is_slug(top) and _safe_rel(rel):
                files.setdefault(top, []).append({"path": rel, "size": int(e.get("size") or 0)})
    slugs = sorted(s for s in tops
                   if any(f["path"] == "metadata.json" for f in files.get(s, [])))

    def fetch_meta(slug):
        url = f"{RAW_BASE}/{gh}/{commit}/{slug}/metadata.json"
        resp, error = _http_get(url, client=client)
        return slug, (None if error else _parse_metadata(resp.content)), error

    apps = {}
    with httpx.Client() as client, ThreadPoolExecutor(WORKERS) as pool:
        for slug, meta, error in pool.map(fetch_meta, slugs):
            if error and not error.startswith("not found"):
                # A network failure mid-build is a failed source, not a
                # silently shorter catalog.
                raise SourceError(f"could not read {slug}/metadata.json: {error}")
            if meta is None:
                continue  # broken metadata.json: not an app
            apps[slug] = {"meta": meta, "tree_sha": tops[slug],
                          "files": sorted(files[slug], key=lambda f: f["path"])}
    return {"source": SOURCE_API, "commit": commit, "apps": apps}


def _git_snapshot_locked(head: str) -> dict:
    tops = {path: sha for typ, sha, _size, path in _ls_tree("HEAD")
            if typ == "tree" and is_slug(path)}
    files: dict[str, list] = {}
    for typ, _sha, size, path in _ls_tree("HEAD", recursive=True):
        if typ != "blob" or "/" not in path:
            continue
        top, rel = path.split("/", 1)
        if top in tops and _safe_rel(rel):
            files.setdefault(top, []).append({"path": rel, "size": size or 0})
    apps = {}
    for slug in sorted(tops):
        if not any(f["path"] == "metadata.json" for f in files.get(slug, [])):
            continue
        try:
            with open(os.path.join(mirror_dir(), slug, "metadata.json"), "rb") as f:
                meta = _parse_metadata(f.read())
        except OSError:
            meta = None
        if meta is None:
            continue
        apps[slug] = {"meta": meta, "tree_sha": tops[slug],
                      "files": sorted(files[slug], key=lambda f: f["path"])}
    return {"source": SOURCE_GIT, "commit": head, "apps": apps}


def _git_snapshot(cfg: dict) -> dict:
    with _MIRROR_LOCK:
        head, warning = _ensure_mirror(fetch=True)
        snap = _git_snapshot_locked(head)
    if warning:
        snap["warning"] = warning
        snap["stale"] = True
    return snap


def _build_snapshot() -> dict:
    """A fresh snapshot from the preferred source, falling back to the git
    mirror. Raises SourceError when neither can produce one."""
    cfg = repo_config()
    forced = forced_source()
    warning = None
    if forced != SOURCE_GIT and cfg["gh"]:
        try:
            snap = _api_snapshot(cfg)
            snap.setdefault("warning", None)
            return {**snap, **_cfg_fields(cfg)}
        except SourceError as exc:
            if forced == SOURCE_API:
                raise
            warning = f"{str(exc)[:1].upper()}{str(exc)[1:]} — using git mirror"
            logger.info("marketplace: github-api source failed (%s); trying git mirror", exc)
    snap = _git_snapshot(cfg)
    snap["warning"] = "; ".join(w for w in (warning, snap.get("warning")) if w) or None
    return {**snap, **_cfg_fields(cfg)}


def _cfg_fields(cfg: dict) -> dict:
    return {"repo": cfg["repo"], "branch": cfg["branch"], "clone_url": cfg["clone_url"]}


# ---------------------------------------------------------------------------
# catalog cache
# ---------------------------------------------------------------------------

_CATALOG_LOCK = threading.Lock()


def _read_cache() -> dict | None:
    try:
        with open(catalog_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        return None
    cfg = repo_config()
    if data.get("clone_url") != cfg["clone_url"] or data.get("branch") != cfg["branch"]:
        return None  # cached for a different repo/branch
    return data


def _write_cache(snap: dict) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    tmp = catalog_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f)
    os.replace(tmp, catalog_path())


def _prune_asset_cache(keep_commit: str | None) -> None:
    root = os.path.join(state_dir(), "assets")
    try:
        names = os.listdir(root)
    except OSError:
        return
    for n in names:
        if n != keep_commit:
            shutil.rmtree(os.path.join(root, n), ignore_errors=True)


def get_snapshot(refresh: bool = False) -> tuple[dict | None, str | None]:
    """The catalog snapshot (cached or freshly built) and, when there is none
    at all, the error message. Never raises."""
    with _CATALOG_LOCK:
        cached = _read_cache()
        now = time.time()
        if cached and not refresh and now - float(cached.get("checked_at") or 0) < CACHE_TTL_S:
            return cached, None
        try:
            snap = _build_snapshot()
        except SourceError as exc:
            msg = str(exc)
            logger.warning("marketplace: catalog refresh failed: %s", msg)
            if cached:
                cached["stale"] = True
                cached["checked_at"] = now
                cached["warning"] = (f"Could not refresh the catalog ({msg}) — showing apps "
                                     f"cached at {cached.get('fetched_at')}")
                try:
                    _write_cache(cached)
                except OSError:
                    pass
                return cached, None
            return None, f"Could not load the community catalog: {msg}"
        except Exception as exc:  # noqa: BLE001 — a bug must not take the page down
            logger.exception("marketplace: catalog build crashed")
            if cached:
                cached["stale"] = True
                return cached, None
            return None, f"Could not load the community catalog: {exc.__class__.__name__}"
        snap["fetched_at"] = _utc_now()
        snap["checked_at"] = now
        snap["stale"] = bool(snap.get("stale"))
        try:
            _write_cache(snap)
        except OSError:
            logger.warning("marketplace: could not write %s", catalog_path(), exc_info=True)
        if not cached or cached.get("commit") != snap.get("commit"):
            _prune_asset_cache(snap.get("commit"))
        return snap, None


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
    """slug → {path, stamp} for every `community/*/.fused/marketplace.json`."""
    root = community_dir()
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
        if stamp is None:
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
        "tree_sha": stamp.get("tree_sha"),
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


def _pick(paths: set[str], *names: str) -> str | None:
    for n in names:
        if n in paths:
            return n
    return None


def _featured_mode(snap: dict) -> bool:
    """True when some app's metadata declares `featured` itself."""
    return any(isinstance(a.get("meta", {}).get("featured"), bool)
               for a in snap.get("apps", {}).values())


def app_entry(slug: str, app: dict, snap: dict, installed: dict | None,
              declared_featured: bool | None = None) -> dict:
    """One AppEntry (the contract's shape) out of a snapshot app record."""
    meta = app.get("meta") or {}
    files = app.get("files") or []
    paths = {f["path"] for f in files}
    icon = _pick(paths, "icon.svg", "icon.png")
    preview = _pick(paths, "preview.png", "preview.jpg", "preview.webp")
    requires_python = meta.get("requires_python")
    if not isinstance(requires_python, bool):
        # Not declared: a pyproject or any top-level .py says it runs Python.
        requires_python = any(p == "pyproject.toml" or (p.endswith(".py") and "/" not in p)
                              for p in paths)
    if declared_featured is None:
        declared_featured = _featured_mode(snap)
    featured = (meta.get("featured") is True) if declared_featured else slug in DEFAULT_FEATURED
    gh = repo_config()["gh"] if snap.get("repo") == repo_config()["repo"] else None
    branch = snap.get("branch") or DEFAULT_BRANCH
    inst = _installed_info(installed)
    tree_sha = app.get("tree_sha")
    return {
        "slug": slug,
        "name": _opt_str(meta.get("name")) or slug,
        "description": meta.get("description") if isinstance(meta.get("description"), str) else "",
        "author": _author(meta.get("author")),
        "tags": _str_list(meta.get("tags")),
        "category": _opt_str(meta.get("category")) or "other",
        "version": _opt_str(meta.get("version")) or "0.0.0",
        "requires_python": requires_python,
        "min_fused_render": _opt_str(meta.get("min_fused_render")),
        "ai_capabilities": _str_list(meta.get("ai_capabilities")),
        "ai_models": _str_list(meta.get("ai_models")),
        "ai_model_param": _opt_str(meta.get("ai_model_param")),
        "icon_url": f"/api/marketplace/asset/{slug}/{icon}" if icon else None,
        "preview_url": f"/api/marketplace/asset/{slug}/{preview}" if preview else None,
        "size_bytes": sum(int(f.get("size") or 0) for f in files),
        "file_count": len(files),
        "tree_sha": tree_sha,
        "source_url": f"https://github.com/{gh}/tree/{branch}/{slug}" if gh else None,
        "featured": featured,
        "installed": inst,
        "update_available": bool(inst and tree_sha and inst.get("tree_sha") != tree_sha),
        "compatible": _compatible(meta.get("min_fused_render")),
    }


def _categories(entries: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["category"]] = counts.get(e["category"], 0) + 1
    ids = list(CATEGORY_ORDER) + sorted(c for c in counts if c not in CATEGORY_ORDER)
    return [{"id": c, "label": CATEGORY_LABELS.get(c) or c.replace("-", " ").title(),
             "count": counts.get(c, 0)} for c in ids]


def catalog(refresh: bool = False) -> dict:
    """GET /api/marketplace/catalog's payload."""
    snap, error = get_snapshot(refresh=refresh)
    if snap is None:
        return {"status": "error", "message": error, "apps": [], "categories": []}
    installed = installed_apps()
    declared = _featured_mode(snap)
    entries = [app_entry(slug, app, snap, installed.get(slug), declared)
               for slug, app in sorted(snap["apps"].items())]
    return {
        "status": "ok",
        "source": snap.get("source"),
        "repo": snap.get("repo"),
        "commit": snap.get("commit"),
        "fetched_at": snap.get("fetched_at"),
        "stale": bool(snap.get("stale")),
        "warning": snap.get("warning") or None,
        "categories": _categories(entries),
        "apps": entries,
    }


def _local_entry(slug: str, rec: dict) -> dict:
    """An AppEntry for an install the catalog no longer lists, from what the
    stamp and the copy's own metadata.json know."""
    stamp = rec["stamp"]
    try:
        with open(os.path.join(rec["path"], "metadata.json"), "rb") as f:
            meta = _parse_metadata(f.read()) or {}
    except OSError:
        meta = {}
    files = []
    for rel in sorted((stamp.get("files") or {})):
        if not _safe_rel(rel):
            continue
        try:
            files.append({"path": rel, "size": os.path.getsize(os.path.join(rec["path"], *rel.split("/")))})
        except OSError:
            files.append({"path": rel, "size": 0})
    entry = app_entry(slug, {"meta": meta, "files": files, "tree_sha": stamp.get("tree_sha")},
                      {"apps": {}}, rec, declared_featured=True)
    # No catalog record → nothing to serve assets from, nothing to update to.
    entry.update(icon_url=None, preview_url=None, source_url=None, update_available=False,
                 featured=False)
    if not meta.get("version") and stamp.get("version"):
        entry["version"] = stamp["version"]
    return entry


def installed_list() -> dict:
    """GET /api/marketplace/installed — every stamped install, catalog-backed
    where the catalog (cached; never forces a network refresh) still has it."""
    snap, _error = get_snapshot(refresh=False)
    apps = (snap or {}).get("apps") or {}
    declared = _featured_mode(snap) if snap else False
    out = []
    for slug, rec in sorted(installed_apps().items()):
        if slug in apps:
            out.append(app_entry(slug, apps[slug], snap, rec, declared))
        else:
            out.append(_local_entry(slug, rec))
    return {"apps": out}


# ---------------------------------------------------------------------------
# assets + detail
# ---------------------------------------------------------------------------

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


def _fetch_asset_bytes(snap: dict, slug: str, rel: str) -> bytes | None:
    """The bytes of `<slug>/<rel>` at the snapshot's commit, from the
    snapshot's source (falling back to the mirror's checkout)."""
    commit = snap.get("commit")
    cfg = repo_config()
    if snap.get("source") == SOURCE_API and cfg["gh"]:
        r, err = _http_get(f"{RAW_BASE}/{cfg['gh']}/{commit}/{slug}/{rel}")
        if not err and r is not None:
            return r.content
    mirror = mirror_dir()
    if not os.path.isdir(os.path.join(mirror, ".git")):
        return None
    with _MIRROR_LOCK:
        try:
            r = _git("cat-file", "blob", f"{commit}:{slug}/{rel}", cwd=mirror)
            if r.returncode == 0:
                return r.stdout
        except SourceError:
            pass
        # The commit is gone from the shallow mirror: the checkout is the
        # nearest thing (a preview a commit newer, never a wrong app).
        p = os.path.join(mirror, slug, *rel.split("/"))
        try:
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as f:
                    return f.read(MAX_ASSET_BYTES + 1)
        except OSError:
            return None
    return None


def get_asset(slug: str, rel: str) -> tuple[bytes, str] | None:
    """(bytes, content type) for one file of a catalog app, or None (→ 404):
    unknown slug, a path not in that app's file list (which also refuses
    every traversal shape), too large, or unfetchable."""
    if not is_slug(slug) or not _safe_rel(rel):
        return None
    snap, _err = get_snapshot(refresh=False)
    if not snap:
        return None
    app = snap["apps"].get(slug)
    if not app:
        return None
    entry = next((f for f in app.get("files") or [] if f["path"] == rel), None)
    if entry is None or int(entry.get("size") or 0) > MAX_ASSET_BYTES:
        return None
    commit = snap.get("commit") or "unknown"
    cache_root = os.path.join(state_dir(), "assets", commit, slug)
    cached = os.path.join(cache_root, *rel.split("/"))
    try:
        with open(cached, "rb") as f:
            return f.read(), _content_type(rel)
    except OSError:
        pass
    data = _fetch_asset_bytes(snap, slug, rel)
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
    return data, _content_type(rel)


def app_detail(slug: str) -> dict | None:
    """GET /api/marketplace/app/{slug}'s payload, or None (→ 404)."""
    if not is_slug(slug):
        return None
    snap, error = get_snapshot(refresh=False)
    installed = installed_apps()
    if snap and slug in snap["apps"]:
        app = snap["apps"][slug]
        entry = app_entry(slug, app, snap, installed.get(slug))
        files = [{"path": f["path"], "size": f["size"]} for f in app.get("files") or []]
        readme_rel = next((f["path"] for f in files
                           if f["path"].lower() in ("readme.md", "readme.markdown", "readme")), None)
        readme = ""
        if readme_rel:
            got = get_asset(slug, readme_rel)
            if got:
                readme = got[0][:MAX_README_BYTES].decode("utf-8", "replace")
        return {"status": "ok", "app": entry, "readme": readme, "files": files,
                "readme_base": f"/api/marketplace/asset/{slug}/"}
    if slug in installed:
        # Installed but gone from the catalog: describe the local copy.
        rec = installed[slug]
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
                "readme_base": f"/api/marketplace/asset/{slug}/"}
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
    snap, error = get_snapshot(refresh=False)
    if snap is None:
        raise InstallError(error or "the catalog is unavailable")
    if slug not in snap["apps"]:
        raise InstallError(f"no app named {slug!r} in the catalog")
    action = "update" if read_stamp(os.path.join(community_dir(), slug)) else "install"
    total = sum(int(f.get("size") or 0) for f in snap["apps"][slug].get("files") or [])
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


def _wanted(files: list[dict]) -> list[dict]:
    """The files an install writes: safe paths, nothing under a KEEP name."""
    return [f for f in files if _safe_rel(f["path"]) and f["path"].split("/", 1)[0] not in KEEP]


def _download_api(snap: dict, slug: str, files: list[dict], payload: str, progress) -> None:
    gh = repo_config()["gh"]
    commit = snap["commit"]

    def one(f):
        url = f"{RAW_BASE}/{gh}/{commit}/{slug}/{f['path']}"
        dest = os.path.join(payload, *f["path"].split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with client.stream("GET", url, timeout=HTTP_TIMEOUT_S * 3,
                               follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise InstallError(f"GitHub answered {r.status_code} for {f['path']}")
                with open(dest, "wb") as out:
                    for chunk in r.iter_bytes(256 * 1024):
                        out.write(chunk)
                        progress(len(chunk))
        except httpx.HTTPError as e:
            raise InstallError(f"could not download {f['path']} ({e.__class__.__name__})")

    with httpx.Client() as client, ThreadPoolExecutor(WORKERS) as pool:
        for fut in [pool.submit(one, f) for f in files]:
            fut.result()


def _copy_from_mirror(slug: str, payload: str, progress) -> dict:
    """Copy the app out of the mirror's checkout (caller holds _MIRROR_LOCK);
    answers the {commit, tree_sha, files} actually copied."""
    mirror = mirror_dir()
    head = _git_out("rev-parse", "HEAD", cwd=mirror).strip()
    tree_sha = _git_out("rev-parse", f"HEAD:{slug}", cwd=mirror).strip()
    files = []
    for typ, _sha, size, path in _ls_tree("HEAD", f"{slug}/", recursive=True):
        if typ == "blob" and path.startswith(slug + "/"):
            files.append({"path": path[len(slug) + 1:], "size": size or 0})
    files = _wanted(files)
    for f in files:
        src = os.path.join(mirror, slug, *f["path"].split("/"))
        dest = os.path.join(payload, *f["path"].split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.islink(src):
            continue  # symlinks are not something an app ships
        shutil.copyfile(src, dest)
        progress(f["size"])
    return {"commit": head, "tree_sha": tree_sha, "files": files}


def _stage(snap: dict, slug: str, payload: str, progress) -> dict:
    """Fill `payload` with the app's files; answers {commit, tree_sha,
    files, source} describing exactly what was staged."""
    app = snap["apps"][slug]
    files = _wanted(app.get("files") or [])
    if snap.get("source") == SOURCE_API and repo_config()["gh"]:
        try:
            _download_api(snap, slug, files, payload, progress)
            return {"commit": snap["commit"], "tree_sha": app["tree_sha"], "files": files,
                    "source": SOURCE_API}
        except InstallError as exc:
            logger.info("marketplace: raw download of %s failed (%s); using git mirror", slug, exc)
            shutil.rmtree(payload, ignore_errors=True)
            os.makedirs(payload)
            progress(None)  # reset the byte count
    with _MIRROR_LOCK:
        try:
            # Fetch when the mirror is behind the catalog this install was
            # chosen from (or does not exist yet).
            behind = True
            if os.path.isdir(os.path.join(mirror_dir(), ".git")):
                head = _git_out("rev-parse", "HEAD", cwd=mirror_dir()).strip()
                behind = head != snap.get("commit")
            _ensure_mirror(fetch=behind)
            r = _git("cat-file", "-e", f"HEAD:{slug}", cwd=mirror_dir())
            if r.returncode != 0:
                raise InstallError(f"{slug} is no longer in the community repo")
            got = _copy_from_mirror(slug, payload, progress)
        except SourceError as exc:
            raise InstallError(str(exc))
    return {**got, "source": SOURCE_GIT}


def _prune_dropped(dest: str, old_stamp: dict | None, new_paths: set[str]) -> list[str]:
    """Files the OLD stamp tracked that the new version no longer ships,
    removed only while their bytes still match that stamp (the author deleted
    them; the user never touched them). Empty parent dirs go too."""
    if not old_stamp or not isinstance(old_stamp.get("files"), dict):
        return []
    pruned = []
    for rel, digest in sorted(old_stamp["files"].items()):
        if rel in new_paths or not _safe_rel(rel) or rel.split("/", 1)[0] in KEEP:
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
    """Install or update `slug` from `snap` into `community/<slug>`,
    synchronously. `report(**job_fields)` receives progress. Answers
    {action, path, stamp, pruned}. Raises InstallError."""
    from fused_render import appfile

    root = community_dir()
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
    action = "update" if old_stamp else "install"
    meta = snap["apps"][slug].get("meta") or {}
    total = sum(int(f.get("size") or 0) for f in _wanted(snap["apps"][slug].get("files") or []))
    done = [0]
    lock = threading.Lock()

    def progress(n):
        with lock:
            done[0] = 0 if n is None else done[0] + n
            frac = min(done[0] / total, 1.0) if total else 0.0
            report(bytes_done=done[0], progress=round(0.9 * frac, 4))

    report(state="downloading", action=action, bytes_total=total, progress=0.0,
           message="Downloading…")
    staging = tempfile.mkdtemp(dir=root, prefix=f".marketplace-{slug}-")
    payload = os.path.join(staging, "files")
    os.makedirs(payload)
    try:
        staged = _stage(snap, slug, payload, progress)
        report(state="installing", progress=0.92,
               message="Updating…" if action == "update" else "Installing…")
        hashes = {}
        for f in staged["files"]:
            p = os.path.join(payload, *f["path"].split("/"))
            if os.path.isfile(p):
                hashes[f["path"]] = _sha256_file(p)
        if not exists:
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
    stamp = {
        "slug": slug,
        "version": meta.get("version") if isinstance(meta.get("version"), str) else None,
        "tree_sha": staged["tree_sha"],
        "commit": staged["commit"],
        "repo": snap.get("repo"),
        "installed_at": _utc_now(),
        "source": staged["source"],
        "files": hashes,
    }
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
        name = (snap["apps"][slug].get("meta") or {}).get("name") or slug
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
