"""One-shot migration of the Fused workspace out of the iCloud-synced
``~/Documents`` tree: ``~/Documents/Fused`` -> ``~/Fused`` (D337).

Runs at the real process entry points (``cli._run_serve``,
``app._start_server_thread``) immediately BEFORE ``seed.ensure_fused_dir`` —
never from ``create_app``, so importing the server in a test cannot touch a
user's real directories (the same property ``meta_migration`` has). Order
matters: ``ensure_fused_dir`` creates the destination, and a destination that
exists is exactly what this refuses to migrate into.

Safety, all non-negotiable:

* **Move only into empty space.** The legacy dir must exist AND ``~/Fused``
  must not. Anything else logs and does nothing — never a merge, never a
  clobber.
* **Nothing is ever deleted.** The workspace is *renamed* (it holds git working
  trees; a copy-and-delete would churn every object and risk a partial tree),
  and so is the sidecar subtree.
* **``FUSED_RENDER_DIR`` wins, untouched.** A user who chose their own location
  is left alone entirely — migrating out from under them would be wrong, and
  the override keeps resolving exactly as it did.
* **Best-effort.** Any failure is logged and swallowed: the app starts.

Three things move, because the workspace's absolute path is written down in
three places:

a) the folder itself;
b) the **sidecar subtree** — per-file state keyed by the source file's absolute
   path (``shell/storage.sidecar_path``). This is the part that protects real
   data: sidecars hold ``comments`` (the annotate log, D101), ``docs`` version
   history, ``revertStash``, ``claudeSessions``, .... The mapping is a pure
   path transform, so one directory rename re-homes all of it and no file
   contents need patching;
c) **absolute paths written into the app's own state** — community install
   records, bookmark and recents view urls, scheduled-message targets (a
   stale one fails a 400 on an unattended run), and the menu-bar pin (the one
   of these that lives under Application Support, not ``home_dir()``).

Re-entrant: (b) and (c) are attempted on every startup, gated on their own
"old shape present, new shape absent" checks, so a process that died between
the folder move and the bookkeeping heals on the next start instead of leaving
orphaned state behind forever.
"""
import logging
import os
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from fused_render._branch import branch_dir
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import storage
from fused_render.shell.seed import fused_dir

logger = logging.getLogger(__name__)

# The pre-D337 default. A literal, not a call to anything: this is history, and
# it must keep naming the old location even as the default moves again.
LEGACY_FUSED_DIR = "~/Documents/Fused"

# Shell url prefixes whose remainder is the target's absolute path, mirroring
# bookmarks._VIEW_PREFIXES / recents._VIEW_PREFIXES.
_VIEW_PREFIXES = ("/explorer/view/", "/explorer/embed/", "/view/", "/embed/")

# The `file=` param of the `_bookmark` sentinel url, matched in place so the
# rest of the query keeps its exact original encoding.
_FILE_PARAM_RE = re.compile(r"(^|&)file=([^&]*)")


def legacy_dir() -> str:
    """Absolute ``~/Documents/Fused``. Path only — no I/O."""
    return os.path.abspath(os.path.expanduser(LEGACY_FUSED_DIR))


def run() -> None:
    """Migrate if and only if it is unambiguously safe to. Never raises."""
    try:
        _run()
    except Exception:  # pragma: no cover - defensive; startup must continue
        logger.exception("workspace migration failed; leaving everything as it was")


def _run() -> None:
    if os.environ.get("FUSED_RENDER_DIR"):
        # The user picked their own workspace. Nothing here applies to them.
        return
    src, dst = legacy_dir(), fused_dir()
    if src == dst:
        return
    if os.path.isdir(src):
        if os.path.exists(dst):
            logger.warning(
                "not migrating %s -> %s: the destination already exists. Both are "
                "left exactly as they are; move the contents by hand if you want "
                "them merged.", src, dst)
            return
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as exc:
            logger.warning("could not move %s to %s (%s); nothing was changed",
                           src, dst, exc)
            return
        logger.info("moved the Fused workspace %s -> %s (D337)", src, dst)
    if os.path.exists(src):
        # Still there, and not a directory we could move (a stray FILE at the
        # legacy path). Rewriting state to point at a destination nothing was
        # moved into would be worse than leaving it.
        return
    # Bookkeeping runs even when the folder move was a no-op: it is how a run
    # interrupted between the two steps heals.
    _move_sidecars(src, dst)
    _rewrite_state(src, dst)


# --------------------------------------------------------------- path remapping

def _remap(path: object, src: str, dst: str) -> str | None:
    """`path` re-rooted from `src` to `dst`, or None when it is not under
    `src`.

    `path` is typed `object`, not `str`, because that is what the callers
    actually have: every value here comes straight out of a user's JSON
    (`rec["path"]`, `entry["target"]`) and may be anything at all. The
    isinstance guard below is the load-bearing part — `run()` swallows
    exceptions, so a raise on one corrupt value would silently abandon the
    migration part-way and leave the state half-rewritten. Annotating `str`
    made a type checker call that guard unreachable, which is an invitation to
    delete it.

    The two sides arrive in different shapes and are compared in
    ``_view_url_codec.canonical_fs_path`` form so they can meet: `src`/`dst`
    come from ``os.path.abspath`` and are backslashed on Windows, while a path
    that came back out of a view url is forward-slashed on every platform.
    Comparing the raw strings matched nothing at all on Windows.

    The result takes its separator style from `path`, so a url value goes back
    canonical and an OS-native record keeps ``os.sep``. Canonicalization is a
    1:1 character substitution, which is what lets the tail be spliced by
    offset off the original string.
    """
    if not isinstance(path, str) or not path:
        return None
    csrc, cpath = canonical_fs_path(src), canonical_fs_path(path)
    if cpath != csrc and not cpath.startswith(csrc + "/"):
        return None
    return (canonical_fs_path(dst) if cpath == path else dst) + path[len(src):]


def _rooted(segments: list[str]) -> str:
    """Decoded url segments -> the absolute path they name, mirroring
    bookmarks._decode_fs_path (a Windows drive path is already absolute; a
    POSIX one needs its leading slash back)."""
    joined = "/".join(segments)
    if len(joined) == 2 and joined[0].isalpha() and joined[1] == ":":
        return joined + "/"
    if len(joined) >= 3 and joined[0].isalpha() and joined[1] == ":" and joined[2] == "/":
        return joined
    return "/" + joined


def _encode(path: str) -> str:
    """Absolute path -> url path segments, mirroring
    _view_url_codec.view_url_path — including its rule that ONLY a drive-letter
    path has its backslashes normalized. An unconditional replace here split a
    legal POSIX filename ("weird\\name.html") into two segments and wrote a
    bookmark for a path that does not exist."""
    return "/".join(quote(seg, safe="!*'()")
                    for seg in canonical_fs_path(path).lstrip("/").split("/") if seg)


def _remap_url(url: object, src: str, dst: str) -> str | None:
    """A shell view url re-rooted from `src` to `dst`, or None when it does not
    point into `src`. The query string is carried through untouched — it holds
    the page's params, and re-encoding it would rewrite bytes we do not own.

    `url` is `object` for the same reason `_remap`'s `path` is: it arrives
    from bookmarks.json / recents.json and is only a string if the file says
    so."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    for prefix in _VIEW_PREFIXES:
        if parts.path.startswith(prefix):
            rest = parts.path[len(prefix):]
            break
    else:
        return None
    segments = [unquote(s) for s in rest.split("/") if s]
    if not segments:
        return None
    if len(segments) == 1 and segments[0].startswith("_"):
        # A shell sentinel (`_prefs`, `_bookmark`, ...) names no path of its
        # own — but `_bookmark` carries one in `file=`.
        if segments[0] != "_bookmark":
            return None
        query = _remap_file_param(parts.query, src, dst)
        if query is None:
            return None
        return urlunsplit(("", "", parts.path, query, parts.fragment))
    remapped = _remap(_rooted(segments), src, dst)
    if remapped is None:
        return None
    return urlunsplit(("", "", prefix + _encode(remapped), parts.query, parts.fragment))


def _remap_file_param(query: str, src: str, dst: str) -> str | None:
    # `query` really is a str here (urlsplit's, empty when there is none) —
    # unlike this module's other inputs it never came out of untrusted JSON.
    m = _FILE_PARAM_RE.search(query)
    if not m:
        return None
    remapped = _remap(unquote(m.group(2)), src, dst)
    if remapped is None:
        return None
    return query[:m.start(2)] + quote(remapped, safe="") + query[m.end(2):]


# ------------------------------------------------------------------- sidecars

def _sidecar_root(path: str) -> str:
    """Where `path`'s sidecar subtree lives under home_dir(). Derived through
    storage._sidecar_subpath rather than string concatenation so drive-letter
    and UNC shapes stay correct."""
    parts = [p for p in storage._sidecar_subpath(os.path.abspath(path)).split("/") if p]
    return os.path.join(storage.home_dir(), "sidecar", *(parts or [""]))


def _move_sidecars(src: str, dst: str) -> None:
    """Re-home both halves of the workspace's sidecar state.

    Two moves, not one, because ``storage.sidecar_path`` appends ".json": the
    sidecar of the workspace DIRECTORY ITSELF is a *sibling* file of the
    subtree, not a child of it, so the subtree rename walks straight past it.
    It is real state — a directory listing is bookmarkable
    (``bookmarks._fs_path_from_url``), and its ``bookmarkHistory`` would be
    orphaned by the move.
    """
    old, new = _sidecar_root(src), _sidecar_root(dst)
    if old == new:
        return
    _rename_sidecar(old, new, "subtree")
    _rename_sidecar(old + ".json", new + ".json", "file for the workspace itself")


def _rename_sidecar(old: str, new: str, what: str) -> None:
    """Same safety rules as everything else here: only into empty space, never
    a delete, never a clobber."""
    if not os.path.exists(old):
        return
    if os.path.exists(new):
        logger.warning("not moving the sidecar %s %s -> %s: the destination "
                       "already exists; per-file state for the moved workspace "
                       "stays where it is", what, old, new)
        return
    try:
        os.makedirs(os.path.dirname(new), exist_ok=True)
        os.rename(old, new)
    except OSError as exc:
        logger.warning("could not move the sidecar %s %s -> %s (%s)",
                       what, old, new, exc)
        return
    logger.info("moved the sidecar %s %s -> %s", what, old, new)


# ---------------------------------------------------------------- state files

def _rewrite_state(src: str, dst: str) -> None:
    for label, fn in (("community installs", _rewrite_installs),
                      ("bookmarks", _rewrite_bookmarks),
                      ("recents", _rewrite_recents),
                      ("scheduled messages", _rewrite_schedule),
                      ("the menu-bar pin", _rewrite_pin)):
        try:
            fn(src, dst)
        except Exception:
            logger.exception("could not rewrite workspace paths in %s", label)


def _rewrite_installs(src: str, dst: str) -> None:
    """installs.json records each installed app's absolute folder."""
    from fused_render import community

    data = storage.read_json(community.INSTALLS_JSON)
    installs = data.get("installs") if isinstance(data, dict) else None
    if not isinstance(installs, dict):
        return
    changed = False
    for rec in installs.values():
        if not isinstance(rec, dict):
            continue
        remapped = _remap(rec.get("path"), src, dst)
        if remapped is not None:
            rec["path"] = remapped
            changed = True
    if changed:
        storage.write_json(community.INSTALLS_JSON, data)
        logger.info("rewrote workspace paths in %s", community.INSTALLS_JSON)


def _rewrite_bookmarks(src: str, dst: str) -> None:
    """The bookmark tree stores view urls; folders nest to any depth (D121)."""
    path = os.path.join(storage.home_dir(), "bookmarks.json")
    items = storage.read_json(path)
    if not isinstance(items, list):
        return
    changed = False

    def walk(entries: list) -> None:
        nonlocal changed
        for item in entries:
            if not isinstance(item, dict):
                continue
            children = item.get("children")
            if isinstance(children, list):
                walk(children)
            remapped = _remap_url(item.get("url"), src, dst)
            if remapped is not None:
                item["url"] = remapped
                changed = True

    walk(items)
    if changed:
        storage.write_json(path, items)
        logger.info("rewrote workspace paths in %s", path)


def _rewrite_recents(src: str, dst: str) -> None:
    path = os.path.join(storage.home_dir(), "recents.json")
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        remapped = _remap_url(entry.get("url"), src, dst)
        if remapped is not None:
            entry["url"] = remapped
            changed = True
    if changed:
        storage.write_json(path, data)
        logger.info("rewrote workspace paths in %s", path)


def _rewrite_schedule(src: str, dst: str) -> None:
    """Scheduled entries carry an absolute `target`; a stale one fails a 400 on
    an unattended run."""
    from fused_render import schedule

    path = schedule.store_path()
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        remapped = _remap(entry.get("target"), src, dst)
        if remapped is not None:
            entry["target"] = remapped
            changed = True
    if changed:
        storage.write_json(path, data)
        logger.info("rewrote workspace paths in %s", path)


def _app_support_dir() -> str:
    """``~/Library/Application Support/fused-render``, branch-nested.

    A deliberate duplicate of ``app.APP_SUPPORT_DIR`` rather than an import:
    ``app`` is the desktop menu-bar entry point (heavy, AppKit-adjacent startup
    state) and it already imports and calls this module, so importing back
    would be a cycle. Built through the same ``branch_dir`` on the same base so
    a ``FUSED_RENDER_BRANCH`` install operates on its OWN nested path, exactly
    as ``storage.home_dir()`` does. Expanded per call, not at import, so it
    follows HOME the way every other path here does.
    """
    return branch_dir(os.path.expanduser("~/Library/Application Support/fused-render"))


def _rewrite_pin(src: str, dst: str) -> None:
    """pin.json holds the menu-bar pin's one absolute path (D97, SPEC §25
    PV-7) — a stale one leaves the pinned view unopenable until the user
    re-pins. It is the only state file here that lives under Application
    Support instead of ``home_dir()``, which is how it was missed.

    Merged, never clobbered, for the same reason ``pin_store.save_pin``
    merges: ``size`` is a window preference, not a property of the pinned
    file. A missing, corrupt or wrong-shaped file is "nothing to do", matching
    ``pin_store``'s own "no pin, never raise".
    """
    from fused_render import pin_store

    path = os.path.join(_app_support_dir(), pin_store.PIN_FILENAME)
    data = storage.read_json(path)
    if not isinstance(data, dict):
        return
    remapped = _remap(data.get("path"), src, dst)
    if remapped is None:
        return
    data["path"] = remapped
    storage.write_json(path, data)
    logger.info("rewrote the workspace path in %s", path)
