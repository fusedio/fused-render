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
  mirror.lock              flock'd around every mirror clone/fetch/reset/copy
                           (the state dir is shared across worktree servers)
  assets/<commit>/<slug>/  bytes served by /api/marketplace/asset, per commit

**Cache policy.** A catalog younger than 10 minutes (by `checked_at`, the last
ATTEMPT — so an offline machine backs off too instead of re-trying the network
on every page view, and a crashing build likewise) is served as-is unless
`refresh=1`; a `refresh=1` within 60 s of the last attempt is served the cache
too. A refresh that fails on both sources serves the old cache with
`stale: true` and a warning; with no cache at all the answer is
`{status: "error", message}`. Only GET /catalog (`get_snapshot`) rebuilds an
OLD cache; assets, detail, install and installed (`cached_snapshot`) take any
cache regardless of age, without the build lock, and build only when there is
none — a rebuild never holds up an <img>.

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
when the catalog's tree sha differs from the stamp's. A HUSK (a folder holding
only `.fused/`, what a keep-data uninstall leaves) is not installed, but an
install into it merges like an update, so its data is reused. A mirror install
copies the blobs of the catalog's own commit out of the object store (never the
working tree, which may have moved on). An app whose paths differ only by case
is refused (they would collide on macOS).

Every git call: an ABSOLUTE git (`git_tools.git_bin()`), `-C` instead of
`cwd=`, `close_fds=False` — the posix_spawn rule (see community.py and
tests/test_git_posix_spawn.py) — plus GIT_TERMINAL_PROMPT=0 and a bounded
timeout. Every HTTP call goes through `_http_get`, which never raises.
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
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

try:  # POSIX only — community.py's `_cache_lock` makes the same assumption.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

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
#: A forced `refresh=1` within this long of the last attempt serves the cache
#: (a reload-hammering user must not burn the 60 req/h API budget).
REFRESH_MIN_INTERVAL_S = 60
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
#: How long a mirror operation waits for another server process (a parallel
#: worktree sharing the state dir) to finish its own clone/fetch.
MIRROR_LOCK_WAIT_S = CLONE_TIMEOUT_S + 60
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
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
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


def is_sha(v) -> bool:
    """A full git object id (sha1 or sha256) — the only thing a commit may be
    before it becomes a cache directory name or a git argument."""
    return isinstance(v, str) and bool(_SHA_RE.fullmatch(v))


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
#: Reentrant; the OUTERMOST holder also takes `_mirror_lock`'s file lock.
_MIRROR_LOCK = threading.RLock()
_MIRROR_DEPTH = [0]


def _flock_acquire(fd: int, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


@contextlib.contextmanager
def _mirror_lock(wait_s: float = MIRROR_LOCK_WAIT_S):
    """Hold the mirror: `_MIRROR_LOCK` within this process plus an OS
    advisory lock on `<state>/mirror.lock` across processes — the state dir
    is shared by every worktree's server, so a per-process lock alone lets
    one server's `reset --hard` run under another's copy (the same reason
    community.py has `_cache_lock`). Raises SourceError when another process
    holds it for longer than ``wait_s``."""
    if not _MIRROR_LOCK.acquire(timeout=wait_s):
        raise SourceError("the community apps mirror is busy — try again in a moment")
    fd = None
    try:
        if _MIRROR_DEPTH[0] == 0 and fcntl is not None:
            os.makedirs(state_dir(), exist_ok=True)
            fd = os.open(os.path.join(state_dir(), "mirror.lock"), os.O_CREAT | os.O_RDWR, 0o644)
            if not _flock_acquire(fd, wait_s):
                os.close(fd)
                fd = None
                raise SourceError("the community apps mirror is busy (another fused-render "
                                  "is updating it) — try again in a moment")
        _MIRROR_DEPTH[0] += 1
        try:
            yield
        finally:
            _MIRROR_DEPTH[0] -= 1
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        _MIRROR_LOCK.release()


def _ensure_mirror(*, fetch: bool) -> tuple[str, str | None]:
    """Make sure the mirror exists (shallow clone on first use) and, when
    ``fetch``, bring it to the branch tip. Answers (head sha, warning) — a
    failed FETCH over an existing mirror is not fatal: the old checkout is
    still a catalog, reported with a warning (and `stale`). Caller holds
    `_mirror_lock()`."""
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


def _ls_tree(ref: str, *paths: str, recursive: bool = False,
             skip_symlinks: bool = False) -> list[tuple[str, str, int | None, str]]:
    """`git ls-tree -l -z` over the mirror: [(type, sha, size, path)]
    (``skip_symlinks`` drops mode-120000 entries: not something an app
    ships, and never something an install writes)."""
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
        mode, typ, sha, size = parts[:4]
        if skip_symlinks and mode == "120000":
            continue
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
        if not is_sha(commit):
            raise SourceError("GitHub answered something unexpected")
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
        url = _raw_url(gh, commit, slug, "metadata.json")
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
    with _mirror_lock():
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

#: Held only while a catalog is being BUILT. Never waited on while a cache
#: exists: readers (assets, detail, install, installed) read catalog.json
#: without it, and a /catalog request that finds a rebuild already running
#: serves the cache instead of queueing behind it — a slow build (20 s HTTP,
#: up to 240 s of git) must not soak up FastAPI's threadpool.
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
    # Unique per writer: other worktrees' servers share this state dir.
    tmp = f"{catalog_path()}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, catalog_path())
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _prune_asset_cache(keep_commit: str | None) -> None:
    root = os.path.join(state_dir(), "assets")
    try:
        names = os.listdir(root)
    except OSError:
        return
    for n in names:
        if n != keep_commit:
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
    snap["stale"] = bool(snap.get("stale"))
    try:
        _write_cache(snap)
    except OSError:
        logger.warning("marketplace: could not write %s", catalog_path(), exc_info=True)
    if not cached or cached.get("commit") != snap.get("commit"):
        _prune_asset_cache(snap.get("commit"))
    return snap, None


def get_snapshot(refresh: bool = False) -> tuple[dict | None, str | None]:
    """The catalog snapshot for GET /catalog — the ONLY caller allowed to
    rebuild a cache because it is old: rebuilt when older than CACHE_TTL_S,
    or on ``refresh`` when the last attempt is over REFRESH_MIN_INTERVAL_S
    old. While another request is already rebuilding, the existing cache is
    answered at once rather than waiting. Answers (snapshot, None) or, when
    there is no cache at all and the build failed, (None, message). Never
    raises. Everything else uses `cached_snapshot()`."""
    cached = _read_cache()
    if _fresh_enough(cached, refresh, time.time()):
        return cached, None
    if cached is not None:
        if not _CATALOG_LOCK.acquire(blocking=False):
            return cached, None  # a rebuild is in flight; it will land shortly
    else:
        _CATALOG_LOCK.acquire()  # nothing to serve without a build anyway
    try:
        # Re-read: the build we may have waited behind just stored one.
        cached = _read_cache()
        if _fresh_enough(cached, refresh, time.time()):
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
    """slug → {path, stamp} for every `community/*/.fused/marketplace.json`
    whose folder still holds the app. A keep-data uninstall
    (routers/app_uninstall._strip_app_files) removes the app's files but
    keeps `.fused/` — and the stamp with it — so a husk is NOT installed;
    it stays an install TARGET (install_app merges into it, reusing the
    data)."""
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
    ver = f"?v={_asset_version(tree_sha)}" if _asset_version(tree_sha) else ""
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
        # `?v=` (the app folder's tree sha) keys the browser cache to the
        # content, so a catalog that moved on never shows last hour's icon.
        "icon_url": f"/api/marketplace/asset/{slug}/{icon}{ver}" if icon else None,
        "preview_url": f"/api/marketplace/asset/{slug}/{preview}{ver}" if preview else None,
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
    snap, _error = cached_snapshot()
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


def _raw_url(gh: str, commit: str, slug: str, rel: str) -> str:
    """raw.githubusercontent.com URL of `<slug>/<rel>` at `commit`, the path
    percent-encoded (a `#`, `?`, space or `%` in a file name is a path
    character there, not URL syntax)."""
    return f"{RAW_BASE}/{gh}/{commit}/{quote(f'{slug}/{rel}', safe='/')}"


def _fetch_asset_bytes(snap: dict, slug: str, rel: str) -> bytes | None:
    """The bytes of `<slug>/<rel>` at the snapshot's commit, from the
    snapshot's source (falling back to the mirror's checkout)."""
    commit = snap.get("commit")
    if not is_sha(commit):
        return None
    cfg = repo_config()
    if snap.get("source") == SOURCE_API and cfg["gh"]:
        r, err = _http_get(_raw_url(cfg["gh"], commit, slug, rel))
        if not err and r is not None:
            return r.content
    mirror = mirror_dir()
    if not os.path.isdir(os.path.join(mirror, ".git")):
        return None
    # Reading an object needs no lock (git's object store is safe to read
    # while a fetch writes it) — so an <img> never waits on a mirror fetch.
    try:
        r = _git("cat-file", "blob", f"{commit}:{slug}/{rel}", cwd=mirror)
        if r.returncode == 0:
            return r.stdout
    except SourceError:
        pass
    # The commit is gone from the shallow mirror: the checkout is the nearest
    # thing (a preview a commit newer, never a wrong app). The checkout DOES
    # change under a reset, so this read holds the mirror — briefly: busy
    # means no preview, not a stalled request.
    p = os.path.join(mirror, slug, *rel.split("/"))
    try:
        with _mirror_lock(wait_s=1.0):
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as f:
                    return f.read(MAX_ASSET_BYTES + 1)
    except (OSError, SourceError):
        return None
    return None


def _asset_version(tree_sha) -> str | None:
    """The `?v=` cache key catalog asset URLs carry: the app folder's tree
    sha (it changes whenever any file in the folder does), shortened."""
    return tree_sha[:12] if isinstance(tree_sha, str) and tree_sha else None


def asset_version(slug: str) -> str | None:
    """The `?v=` value this slug's asset URLs carry right now (cache only,
    never a rebuild). The asset route grants long browser caching only to a
    URL whose `v` equals it — an arbitrary `?v=1` in a readme link proves
    nothing about the bytes behind it."""
    snap, _err = cached_snapshot()
    app = (snap or {}).get("apps", {}).get(slug) if is_slug(slug) else None
    return _asset_version(app.get("tree_sha")) if app else None


def get_asset(slug: str, rel: str) -> tuple[bytes, str] | None:
    """(bytes, content type) for one file of a catalog app, or None (→ 404):
    unknown slug, a path not in that app's file list (which also refuses
    every traversal shape), too large, or unfetchable."""
    if not is_slug(slug) or not _safe_rel(rel):
        return None
    snap, _err = cached_snapshot()
    if not snap:
        return None
    app = snap["apps"].get(slug)
    if not app:
        return None
    entry = next((f for f in app.get("files") or [] if f["path"] == rel), None)
    if entry is None or int(entry.get("size") or 0) > MAX_ASSET_BYTES:
        return None
    commit = snap.get("commit")
    if not is_sha(commit):
        return None  # never a path segment, never a git argument
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
    snap, error = cached_snapshot()
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
    snap, error = cached_snapshot()
    if snap is None:
        raise InstallError(error or "the catalog is unavailable")
    if slug not in snap["apps"]:
        raise InstallError(f"no app named {slug!r} in the catalog")
    dest = os.path.join(community_dir(), slug)
    action = "update" if read_stamp(dest) and not _is_husk(dest) else "install"
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


def _download_api(snap: dict, slug: str, files: list[dict], payload: str, progress) -> None:
    gh = repo_config()["gh"]
    commit = snap["commit"]

    def one(f):
        url = _raw_url(gh, commit, slug, f["path"])
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


def _git_has_commit(sha) -> bool:
    if not is_sha(sha):
        return False
    try:
        return _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=mirror_dir()).returncode == 0
    except SourceError:
        return False


def _write_blobs(shas: dict[str, str], payload: str, progress) -> None:
    """Write each blob (rel → sha) out of the mirror's object store into
    `payload`, through ONE `git cat-file --batch` whose output streams to a
    file (bounded memory, exact bytes — no checkout, no .gitattributes
    filters). Caller holds `_mirror_lock()`."""
    from fused_render.shell import git_tools

    if not shas:
        return
    order = sorted(shas)
    batch = os.path.join(os.path.dirname(payload), "blobs.batch")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        with open(batch, "wb") as out:
            # argv inlined for tests/test_git_posix_spawn.py's sweep.
            r = subprocess.run([git_tools.git_bin(), "-C", mirror_dir(), "cat-file", "--batch"],
                               input="".join(f"{shas[rel]}\n" for rel in order).encode(),
                               stdout=out, stderr=subprocess.PIPE, timeout=CLONE_TIMEOUT_S,
                               env=env, close_fds=False)
    except FileNotFoundError:
        raise SourceError("git is not installed (or not on PATH)")
    except subprocess.TimeoutExpired:
        raise SourceError("git timed out reading the community apps mirror")
    if r.returncode != 0:
        raise SourceError("git cat-file failed reading the community apps mirror")
    with open(batch, "rb") as src:
        for rel in order:
            header = src.readline().decode("ascii", "replace").split()
            if len(header) != 3 or header[0] != shas[rel] or header[1] != "blob":
                raise SourceError(f"the community apps mirror is missing {rel}")
            left = int(header[2])
            dest = os.path.join(payload, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as out:
                while left:
                    chunk = src.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise SourceError(f"the community apps mirror cut {rel} short")
                    out.write(chunk)
                    left -= len(chunk)
                    progress(len(chunk))
            src.read(1)  # the record's trailing newline
    with contextlib.suppress(OSError):
        os.remove(batch)


def _copy_from_mirror(slug: str, ref: str, payload: str, progress) -> dict:
    """Copy the app AS OF `ref` (the catalog's commit when the mirror has it)
    out of the mirror's object store — never its working tree, which may be
    a different commit than the one the user chose from. Caller holds
    `_mirror_lock()`; answers the {commit, tree_sha, files} actually
    copied."""
    mirror = mirror_dir()
    commit = _git_out("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=mirror).strip()
    tree_sha = _git_out("rev-parse", f"{commit}:{slug}", cwd=mirror).strip()
    files, shas = [], {}
    for typ, sha, size, path in _ls_tree(commit, f"{slug}/", recursive=True, skip_symlinks=True):
        if typ == "blob" and path.startswith(slug + "/"):
            rel = path[len(slug) + 1:]
            files.append({"path": rel, "size": size or 0})
            shas[rel] = sha
    _refuse_case_collisions(files)
    files = _wanted(files)
    _write_blobs({f["path"]: shas[f["path"]] for f in files}, payload, progress)
    return {"commit": commit, "tree_sha": tree_sha, "files": files}


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
    try:
        with _mirror_lock():
            want = snap.get("commit")
            mirror_ok = os.path.isdir(os.path.join(mirror_dir(), ".git"))
            if not mirror_ok or not _git_has_commit(want):
                # Clone, or bring the mirror to the branch tip (the catalog is
                # usually the tip, so that is where its commit turns up).
                _ensure_mirror(fetch=True)
            if is_sha(want) and not _git_has_commit(want):
                # Older than the tip (the branch moved on): ask for it by sha.
                with contextlib.suppress(SourceError):
                    _git("fetch", "--depth", "1", "--no-tags", "origin", want,
                         cwd=mirror_dir(), timeout=CLONE_TIMEOUT_S)
            # Fail closed: installing whatever the tip holds now would put
            # code on disk that differs from the version, readme and trust
            # notes the user just read.
            if not _git_has_commit(want):
                raise InstallError("the community repo changed since this page loaded — "
                                   "refresh the Marketplace and try again")
            ref = want
            r = _git("cat-file", "-e", f"{ref}:{slug}", cwd=mirror_dir())
            if r.returncode != 0:
                raise InstallError(f"{slug} is no longer in the community repo")
            got = _copy_from_mirror(slug, ref, payload, progress)
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
    # A husk (keep-data uninstall) with our stamp is merged into like an
    # update — its `.fused/` data is reused — but to the user it is an install.
    action = "update" if old_stamp and not _is_husk(dest) else "install"
    meta = snap["apps"][slug].get("meta") or {}
    _refuse_case_collisions(snap["apps"][slug].get("files") or [])
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
