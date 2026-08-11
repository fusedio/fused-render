"""GET /api/local-models (+ /status) — what the Hugging Face cache holds on
this machine, for the sidebar's "Local models" page.

The cache is a *shared* directory: anything that speaks `huggingface_hub`
(transformers, sentence-transformers, diffusers, a template a user pasted in,
the `hf` CLI) downloads into the same tree, and nothing ever tells the user
what accumulated there or how much disk it is now worth. This endpoint reads
that tree — it never downloads, deletes or evicts anything.

The layout it reads is `huggingface_hub`'s own (CACHE_STRUCTURE in their
docs)::

    <hub cache>/
      models--openai--whisper-small/
        refs/main                 -> a commit sha
        blobs/<sha>               the real bytes
        snapshots/<commit>/…      symlinks back into blobs/
      datasets--squad/
      spaces--user--demo/
      .locks/ version.txt         bookkeeping, skipped

Two consequences drive `_scan_repo`:

* **Size is measured with `lstat`, symlinks skipped.** A snapshot entry points
  at a blob in the *same* repo, so following it would count every file twice
  per revision — a two-revision repo would report triple its real footprint.
  Hardlinks (the same blob shared by two entries, and what Windows falls back
  to when it cannot symlink) are de-duplicated by `(st_dev, st_ino)` for the
  same reason. What is left is bytes actually on disk, which is the number the
  page exists to show.
* **The newest mtime includes the symlinks**, unlike the size — and excludes
  directories. A blob is written once and never touched again, but
  materialising a revision creates its snapshot links, so their mtimes are what
  "last pulled a revision of this repo" actually looks like on disk. Directory
  mtimes are left out because they also move on *deletion*, which would report
  a repo someone just emptied as freshly used.

Repo ids are decoded the way `huggingface_hub` encodes them — kind prefix,
then the id with `/` written as `--` (`models--openai--whisper-small` ->
`openai/whisper-small`). A directory whose name carries no known kind prefix
is not a repo folder and is skipped, which is also what keeps `.locks/`,
`version.txt` and half-written `tmp*` dirs out of the list.

Read-only, so no D3 `X-Fused` guard (same posture as
routers/claude_sessions.py).
"""
import os
import stat
from dataclasses import dataclass

from fastapi import APIRouter

from fused_render._view_url_codec import canonical_fs_path

router = APIRouter()

# Directory-name prefix -> the kind reported to the UI. This is also the
# allowlist: a hub-cache entry that starts with none of these is not a repo.
_KIND_PREFIXES = {"models--": "model", "datasets--": "dataset", "spaces--": "space"}


def hf_home() -> str:
    """The Hugging Face home dir — `HF_HOME`, else `$XDG_CACHE_HOME/huggingface`,
    else `~/.cache/huggingface` (huggingface_hub's own resolution order)."""
    env = os.environ.get("HF_HOME")
    if env:
        return os.path.expanduser(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = os.path.expanduser(xdg) if xdg else os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "huggingface")


def hub_cache_dir() -> str:
    """Where repo folders live: `HF_HUB_CACHE`, else the deprecated-but-still-honored
    `HUGGINGFACE_HUB_CACHE`, else `<hf_home>/hub`.

    Resolved per call rather than at import: the answer is whatever the process
    environment says right now, and a module constant would freeze one machine's
    answer into the module (and force every test to patch a private).
    """
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        env = os.environ.get(var)
        if env:
            return os.path.expanduser(env)
    return os.path.join(hf_home(), "hub")


@dataclass
class _RepoScan:
    """One repo folder's on-disk footprint (see the module docstring for why
    size and mtime treat symlinks differently)."""

    size: int
    files: int
    mtime: float


def _scan_repo(root: str) -> _RepoScan:
    size = 0
    files = 0
    newest = 0.0
    # Only consulted for multiply-linked files — the common case (one link) never
    # touches the set, so a 30k-blob cache doesn't pay for a 30k-entry dict.
    seen: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            # A repo folder being written by a live download, or one we can't
            # read: report what we could see rather than failing the page.
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                # Directory mtimes are excluded from `newest` deliberately: a
                # dir's mtime moves whenever an entry is added OR removed, so an
                # emptied repo would still report "just now".
                stack.append(entry.path)
                continue
            if st.st_mtime > newest:
                newest = st.st_mtime
            if stat.S_ISLNK(st.st_mode):
                continue  # points back into this repo's blobs/ — already counted
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            size += st.st_size
            files += 1
    return _RepoScan(size=size, files=files, mtime=newest)


def _revisions(repo_dir: str) -> int:
    try:
        return sum(1 for e in os.scandir(os.path.join(repo_dir, "snapshots")) if e.is_dir())
    except OSError:
        return 0


def _refs(repo_dir: str) -> list[str]:
    """Branch/tag names under refs/ (`main`, a release tag, …). The commit shas
    they hold are deliberately not read: the page names revisions, it doesn't
    resolve them."""
    refs_dir = os.path.join(repo_dir, "refs")
    names: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(refs_dir):
        rel = os.path.relpath(dirpath, refs_dir)
        for name in filenames:
            names.append(name if rel == "." else f"{rel}/{name}".replace(os.sep, "/"))
    names.sort()
    return names


def _repo(cache_dir: str, dirname: str, kind: str) -> dict:
    repo_dir = os.path.join(cache_dir, dirname)
    scan = _scan_repo(repo_dir)
    return {
        # "models--openai--whisper-small" -> "openai/whisper-small". A bare
        # repo id (no org) has one segment and comes back unchanged.
        "id": "/".join(dirname.split("--")[1:]),
        "kind": kind,
        # Canonicalized like every other fs path the frontend gets, so it can
        # go straight to navigate(path, {isDir: true}).
        "path": canonical_fs_path(repo_dir),
        "size": scan.size,
        "files": scan.files,
        "mtime": scan.mtime or None,
        "revisions": _revisions(repo_dir),
        "refs": _refs(repo_dir),
    }


@router.get("/api/local-models/status")
def api_local_models_status():
    """Cheap availability probe for the sidebar entry — one isdir(), no walk.

    False on a machine that has never pulled from the Hub, which is what keeps
    the row out of that sidebar; the page itself still answers (with an empty
    state) if the URL is opened directly.
    """
    cache_dir = hub_cache_dir()
    return {"available": os.path.isdir(cache_dir), "cacheDir": canonical_fs_path(cache_dir)}


@router.get("/api/local-models")
def api_local_models():
    """Every repo in the hub cache, biggest first.

    Sync `def` on purpose: this walks a tree that can hold tens of thousands of
    blobs, so FastAPI runs it in the threadpool instead of stalling the event
    loop for every other request the page fires.
    """
    cache_dir = hub_cache_dir()
    repos: list[dict] = []
    try:
        entries = list(os.scandir(cache_dir))
    except OSError:
        entries = []
    for entry in entries:
        # Symlinks ARE followed here, unlike inside a repo: a repo folder
        # symlinked in from another disk (how people move a 40GB model off the
        # boot volume) is a real cached repo, and its files still measure
        # correctly since the walk lstats what it finds on the other side. A
        # broken link answers False and drops out.
        if not entry.is_dir():
            continue
        kind = next(
            (k for prefix, k in _KIND_PREFIXES.items() if entry.name.startswith(prefix)), None
        )
        if kind is None:
            continue  # .locks/, tmp dirs, anything that isn't a repo folder
        repos.append(_repo(cache_dir, entry.name, kind))
    # Biggest first: the page's job is "what is this costing me", and a name
    # sort buries the 8GB checkpoint among forty 2MB tokenizer repos.
    repos.sort(key=lambda r: (-r["size"], r["id"]))
    return {
        "cacheDir": canonical_fs_path(cache_dir),
        "hfHome": canonical_fs_path(hf_home()),
        "exists": os.path.isdir(cache_dir),
        "totalSize": sum(r["size"] for r in repos),
        "repos": repos,
    }
