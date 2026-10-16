"""Scratch "Try" copies (contract §9.3): the author runs a proposal in a
separate copy of the app, next to her own, then throws it away. Her own
copy is never touched — the Try copy is a separate clone.

WHERE THEY LIVE: `<workspace>/.fused-try/<owner>-<repo>-<N>/`, i.e. inside
the Fused workspace (`seed.fused_dir()`, ~/Fused) under ONE dotted folder.
Checked against every reader, because each of the four requirements is
answered by a different module:

- IT RENDERS. GET /render serves any readable path, dotted components
  included (server/routers/render.py has no hidden-folder rule), so the copy
  opens through the same `/explorer/view/<entry>` URL every app card uses
  (frontend appEntry.hrefFor → router.urlForFsPath). That URL is `url` below.
- THE APPS GRID NEVER SHOWS IT. The workspace walk skips every dotted name
  (app_listing._walk_apps: `name.startswith(".")`), so `/api/apps` and the
  hub never list it.
- OPENING IT REGISTERS NOTHING. GET /render's D301 block calls
  `record_app_open`; inside the workspace that only writes recents when
  `_app_folder_exists(rel)` passes, and it refuses a key starting with "."
  — so no recents row. (Outside the workspace, e.g. under `<home>/contrib/`,
  the same call would REGISTER the folder on the /apps hub
  (`registered_apps.record_open`) — which is why the copy is not kept there.)
- NOTHING COMMITS OR SYNCS IT. `app_git.app_dir_for` and the chat's
  `agent._app_dir_for` both return nothing for a path whose first
  workspace component is dotted, so no Claude turn auto-commits there; the
  clone has NO `origin` remote, so `git_upstream`'s open-time check and
  auto-sync skip it ("no-remote"); and its record is kind "scratch", which
  `engine.owns` never claims. The contrib routes refuse it too (they require
  `app_git.app_dir_for(path)`).

`app_hidden` is NOT used: it hides showcase subfolders from hub listings
and would add a row per Try for no gain — the dotted folder already keeps the
copy out of every listing.

The copy is a `git clone --no-hardlinks` of the author's repo (local, no
network; nothing shared on disk with her folder) checked out detached at the
proposal's head, which `inbox.review` fetched into `refs/fused/pr/<N>` of her
repo. Removed on accept, decline, and when the proposal is closed on GitHub
(`inbox.list` sweeps copies whose proposal is no longer open).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from urllib.parse import quote

logger = logging.getLogger(__name__)

TRY_DIRNAME = ".fused-try"
_CLONE_TIMEOUT_S = 300.0
_lock = threading.Lock()


def _gu():
    from fused_render import git_upstream

    return git_upstream


def _run(root, *args, timeout=60.0):
    return _gu()._run(root, *args, timeout=timeout)


def try_root() -> str:
    from fused_render.shell.seed import fused_dir

    return os.path.join(fused_dir(), TRY_DIRNAME)


def _repo_of(app_dir: str) -> str | None:
    from fused_render.contrib import store

    record = store.get(app_dir) or {}
    repo = record.get("repo")
    return repo if isinstance(repo, str) and "/" in repo else None


def path_for(repo: str, number: int) -> str:
    owner, _, name = repo.partition("/")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{owner}-{name}").strip(".-") or "app"
    return os.path.join(try_root(), f"{slug}-{int(number)}")


def url_for(path: str) -> str:
    """The `/explorer/view/<entry>` URL the frontend opens — the same codec
    as frontend `urlForFsPath` (each segment encodeURIComponent'd)."""
    from fused_render.app_listing import app_entry

    try:
        entry = app_entry(path)
    except OSError:
        entry = None
    target = entry if entry and os.path.isabs(entry) else (
        os.path.join(path, entry) if entry else path)
    segs = [quote(s, safe="!~*'()") for s in target.strip("/").split("/") if s]
    return "/explorer/view/" + "/".join(segs)


def _head(path: str) -> str | None:
    result = _run(path, "rev-parse", "HEAD")
    return _gu()._out(result) if _gu()._ok(result) else None


def status(app_dir: str, number: int) -> dict | None:
    """`{"status": "ready", "path", "url", "head_sha"}` when a Try copy of
    proposal `number` exists, else None. No network."""
    repo = _repo_of(app_dir)
    if repo is None:
        return None
    path = path_for(repo, number)
    if not os.path.isdir(os.path.join(path, ".git")):
        return None
    return {"status": "ready", "path": path, "url": url_for(path),
            "head_sha": _head(path)}


def create(app_dir: str, number: int, *, head_sha: str | None = None) -> dict:
    """Make (or reuse) the Try copy of proposal `number`; `{"path", "url"}`.
    `head_sha` is the reviewed head: an existing copy at another head is
    rebuilt. Raises `jobs.PlainError` with a plain sentence on failure."""
    from fused_render.contrib import store
    from fused_render.contrib.jobs import PlainError

    repo = _repo_of(app_dir)
    if repo is None:
        raise PlainError("This app has no proposals to try.")
    ref = f"refs/fused/pr/{int(number)}"
    want = _gu()._out(_run(app_dir, "rev-parse", "--verify", "-q", ref + "^{commit}"))
    if not want:
        raise PlainError("Claude hasn't finished reading this proposal yet. "
                         "Try again in a moment.")
    if head_sha and want != head_sha:
        raise PlainError("This proposal changed since Claude read it. "
                         "Wait for the new review, then try it.")
    path = path_for(repo, number)
    with _lock:
        if os.path.isdir(os.path.join(path, ".git")) and _head(path) == want:
            return {"path": path, "url": url_for(path)}
        _remove_path(path)
        os.makedirs(try_root(), exist_ok=True)
        ok = _clone(app_dir, path, ref)
        if not ok:
            _remove_path(path)
            raise PlainError("Couldn't make a copy to try. Try again.")
        record = store.new_record("scratch", repo=repo, classified_by="try")
        record["try_of"] = {"app": os.path.abspath(app_dir), "number": int(number),
                            "head_sha": want}
        store.put(path, record)
    return {"path": path, "url": url_for(path)}


def _clone(app_dir: str, path: str, ref: str) -> bool:
    gu = _gu()
    parent = os.path.dirname(path)
    # Cloning READS the author's repo; nothing in it is written, so no slot.
    steps = [
        (parent, ("clone", "--no-hardlinks", "--no-checkout", "-q",
                  os.path.abspath(app_dir), path)),
        (path, ("fetch", "-q", "--no-tags", os.path.abspath(app_dir),
                f"+{ref}:refs/fused/try")),
        (path, ("checkout", "-q", "--detach", "refs/fused/try")),
        (path, ("remote", "remove", "origin")),
    ]
    for root, args in steps:
        result = gu._run(root, *args, timeout=_CLONE_TIMEOUT_S)
        if not gu._ok(result):
            logger.warning("try copy: git %s failed: %s", args[0],
                           gu._text(result) if result else "did not run")
            return False
    return True


def _remove_path(path: str) -> None:
    root = try_root()
    ap = os.path.abspath(path)
    # Never delete anything that is not a direct child of the Try folder.
    if os.path.dirname(ap) != root or not os.path.lexists(ap):
        return
    shutil.rmtree(ap, ignore_errors=True)
    try:
        from fused_render.contrib import store

        store.drop(ap)
    except Exception:  # noqa: BLE001 — a stale scratch record self-heals
        logger.exception("try copy: could not drop record for %s", ap)
    try:
        from fused_render import app_state_remove

        app_state_remove.prune_stores(ap)
    except Exception:  # noqa: BLE001
        logger.exception("try copy: could not prune stores for %s", ap)


def remove(app_dir: str, number: int) -> dict:
    """Throw the Try copy away. `{"ok": True}` whether or not one existed."""
    repo = _repo_of(app_dir)
    if repo is not None:
        with _lock:
            _remove_path(path_for(repo, number))
    return {"ok": True}


def sweep(app_dir: str, open_numbers) -> int:
    """Remove every Try copy of `app_dir`'s repo whose proposal is not in
    `open_numbers` (closed or merged on GitHub). Returns how many went."""
    repo = _repo_of(app_dir)
    root = try_root()
    if repo is None or not os.path.isdir(root):
        return 0
    keep = {int(n) for n in open_numbers}
    prefix = os.path.basename(path_for(repo, 0))[:-1]  # "<slug>-"
    gone = 0
    for name in os.listdir(root):
        if not name.startswith(prefix):
            continue
        tail = name[len(prefix):]
        if tail.isdigit() and int(tail) not in keep:
            with _lock:
                _remove_path(os.path.join(root, name))
            gone += 1
    return gone
