"""Single-file app export/open: the ``.fused`` app file (SPEC §43, D385-D387).

A ``.fused`` file is one double-clickable artifact holding a whole fused app —
the folder the /apps hub lists (its marker-carrying entry page, every other
page, the ``.py`` data files, assets, ``pyproject.toml``). Export walks the app
FOLDER (the app's own unit of identity, D301) rather than reusing
``export.py``'s per-page static scan: the scan exists to build a minimal
*hosted* bundle from one page and misses computed paths and sibling pages,
while a ``.fused`` targets the opposite trade — carry everything the author
put in the folder, because the thing that opens it is a full fused-render
runtime, not a hosting layer.

Physically the file is an OPAQUE CONTAINER (``appfile_container``: magic
``FUSEDAPP``, versioned header, deflated JSON index, per-file deflate streams)
— NOT a zip. v1 was a zip with its extension renamed, and mail scanners
classify by bytes, not names: Gmail saw ``PK``, walked the members, found the
``.html`` and ``.py`` and flagged the attachment as suspicious. An unknown
binary is not an archive to any scanner, which is the whole reason for the
format; the zip layout survives ONLY on the read side so every v1 file already
exported and indexed (D396) keeps opening. The index records the format tag,
the app name, every member's path/size/hash, and the entry page's
payload-relative path, resolved at *export* time by the one shared entry rule
(``app_listing.app_entry``) so exporter and hub can never disagree about which
page an app opens on.

Opening is INSTALLING, never running from the file (owner, 2026-10-13): a
``.fused`` renders at its own ``/explorer/view|embed/<path>`` URL through the
``fusedapp`` template, which is an install page — the file's icon, name,
export date, README and member list, and one button. Install unpacks the
payload through the hardened extractor into ``<workspace>/app/<slug>`` as an
ordinary editable app folder heading its own git repo: the ONE working copy
of the app. A later file for the same app (its ``app_id``, D884) UPDATES
that copy: a newer export over an unchanged copy goes through quietly; a
copy the user edited since install, an older export, or the same one are
warned about and still allowed behind a second click. ``<app>/.fused/`` —
the app's own data and state (D548) — is never touched by any of it; the
install stamp ``.fused/appfile.json`` is how the next file is told apart as
an update. See the install section below.

There is no cache of extracts any more (the ``appfiles/`` dir and the shared
``fused_data/<app_id>`` state of the run-in-place era are retired; an install
adopts the state and deletes the extracts of the app it installs), and
nothing a ``.fused`` carries runs until the user installs it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone

from fused_render import app_id as app_identity
from fused_render import app_listing
from fused_render import appfile_container as container
from fused_render.zip_import import ZipRejected, ZipTooLarge, extract_to_staging

logger = logging.getLogger(__name__)

# The payload dir a v1 zip carries, and the staging subdir both formats
# extract into — the same single-payload-dir shape as bundle v2's `files/`
# (docs/bundle-v2-design.md).
PAYLOAD_DIR = "files"
MANIFEST_NAME = "manifest.json"

# Export-side budget. An app folder is authored content, not a data lake; a
# folder over these bounds is not going to open acceptably from a zip either,
# and the caps keep a stray huge artifact (a model checkpoint dropped in the
# folder) from silently becoming a multi-GB "app file".
MAX_EXPORT_ENTRIES = 4000
MAX_EXPORT_TOTAL_BYTES = 512 * 1024 * 1024

# Open-side budget, enforced by zip_import on bytes actually decompressed
# (declared sizes in an archive are attacker-controlled). Total is looser than
# the export cap so a file exported at the limit still opens.
MAX_OPEN_ENTRIES = 5000
MAX_OPEN_ENTRY_BYTES = 512 * 1024 * 1024
MAX_OPEN_TOTAL_BYTES = 1024 * 1024 * 1024

# Cap on manifest.json's decompressed size (read before the capped extractor
# runs). A real manifest is a few hundred bytes; 256 KiB is generous.
_MANIFEST_CAP_BYTES = 256 * 1024

# Cap on the payload's preview.png, both directions (a capture injected at
# export time and the member `read_preview` streams to a card). A card
# thumbnail is ~1280x800; 8 MiB is generous for any real screenshot.
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Directory/file names never exported. Dotted names (`.git`, `.claude`,
# `.venv`, `.DS_Store`, `.env` — which may hold secrets) are dropped by the
# hidden-name rule; these are the non-dotted machinery dirs on top of it.
_SKIP_DIRS = frozenset({"node_modules", "__pycache__"})

# CLAUDE.md is the app's AUTHORING contract (scaffolded by app_starter for the
# agent that edits the app); a .fused is the app as a user-facing artifact with
# no editing surface behind it, so the instructions file stays home.
_SKIP_FILES = frozenset({"CLAUDE.md"})

# fused.ai.text() is deliberately ALLOWED in a .fused, unlike the hosted exporter
# (RH-11): a hosted page has no runtime behind it, but an opened .fused runs
# inside the recipient's full local fused-render, where /api/ai exists. A
# recipient without the claude CLI or a resident local model gets the API's
# own graceful `ai_unavailable` rejection, which pages are already written to
# handle (the authoring skill's error table). "No claude" in this artifact's
# contract means no EDITING surface — embed mode strips that — not no AI.
# (Owner call, D388, reversing the D385 stance.)

class AppFileError(Exception):
    """User-correctable failure exporting or opening a ``.fused`` file; the
    message goes verbatim into the route's 400 body."""


class InstallNeedsConfirm(AppFileError):
    """An install that would lose the user's work (a warn state, AF-14) and
    was not ``force``d. Carries the fresh probe so the route can answer 409
    with it and the page can show the confirm — whether the state was known
    before the write or appeared during it."""

    def __init__(self, target: dict):
        super().__init__(f"install state {target['state']!r} needs confirmation")
        self.target = target


def appfiles_root() -> str:
    """Where the run-in-place era extracted app files (retired). Installs
    retire what sits there for the app they install; `registered_apps` still
    refuses paths under it."""
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "appfiles")


class _IgnoreRoots:
    """The gitignore view of one export walk — which `git check-ignore`
    oracle decides each directory, resolved the way the explorer's walk and
    the index filter resolve it (server/gitignore.py is the one authority):

    * app folder inside a git work tree (its own repo, OR a subfolder of a
      bigger repo whose `.gitignore` sits at the toplevel) -> one oracle at
      the toplevel; git cascades every nested `.gitignore`, `.git/info/exclude`
      and the global excludesfile from there by itself.
    * no repo, but a `.gitignore` in the app folder or ANY ancestor -> one
      oracle grafted (empty GIT_DIR) at the nearest such ancestor, which again
      cascades the `.gitignore` files between it and the app.
    * a nested `.git` (or, with no repo in scope, a nested `.gitignore`) met
      during the walk re-roots below it, as the explorer walk does.
    * no git, nothing found -> no filtering. git MISSING fails open too; git BREAKING mid-walk does not
      (see `ignored`) — a half-filtered artifact is worse than no export.
    """

    def __init__(self, app_dir: str):
        from fused_render.server.gitignore import _IgnoreOracle, _repo_toplevel
        self._Oracle = _IgnoreOracle
        self._oracles: dict[str, object] = {}
        # dirpath -> (oracle root or None, inside a real repo). Resolved for
        # every directory the walk enters FROM ITS PARENT's entry, so a
        # nested repo's oracle scopes to that subtree only: os.walk is
        # depth-first, and a single mutable "current root" would leak the
        # nested repo onto the siblings walked after it (they would then
        # fail the `..` guard below and ship parent-ignored files).
        self._by_dir: dict[str, tuple[str | None, bool]] = {}
        root: str | None = None
        in_repo = False
        top = _repo_toplevel(app_dir)
        if top is not None:
            root, in_repo = top, True
        else:
            d = app_dir
            while True:
                if os.path.isfile(os.path.join(d, ".gitignore")):
                    root = d
                    break
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        self._by_dir[app_dir] = (root, in_repo)

    def enter(self, dirpath: str, names: set[str]) -> None:
        """Decide `dirpath`'s oracle: the parent's, unless this dir starts a
        nested repo (or, with no repo in scope, a nested `.gitignore`)."""
        parent = self._by_dir.get(dirpath) or self._by_dir.get(
            os.path.dirname(dirpath), (None, False))
        root, in_repo = parent
        if ".git" in names and dirpath != root and self._is_repo(dirpath):
            root, in_repo = dirpath, True
        elif not in_repo and root is None and ".gitignore" in names:
            root = dirpath
        self._by_dir[dirpath] = (root, in_repo)

    def _is_repo(self, dirpath: str) -> bool:
        """Whether a `.git` name at `dirpath` is a repository GIT accepts.

        The NAME is not the fact. A `.git` directory with a HEAD git rejects
        (a stub, a half-copied checkout, a folder someone named that) made
        this re-root there and start an oracle git answers with "not a git
        repository" — every query then read as the oracle breaking, and the
        export refused a folder whose `.git` was going to be dropped by the
        hidden-name rule anyway. So git is asked the same question the walk's
        entry asks (`_repo_toplevel`, memoized), and a `.git` git will not
        own leaves the parent's oracle in force: fail OPEN on the marker, as
        an unreadable one always did, not closed on the export."""
        from fused_render.server.gitignore import _repo_toplevel

        top = _repo_toplevel(dirpath)
        if top is None:
            return False
        try:
            return os.path.realpath(top) == os.path.realpath(dirpath)
        except OSError:
            return False

    def ignored(self, dirpath: str, names: list[str]) -> set[str]:
        """Subset of `names` (children of `dirpath`) git ignores."""
        root, _ = self._by_dir.get(dirpath, (None, False))
        if root is None or not names:
            return set()
        oracle = self._oracles.get(root)
        if oracle is None:
            oracle = self._oracles[root] = self._Oracle(root)
        rel = os.path.relpath(os.path.realpath(dirpath), os.path.realpath(root))
        if rel.startswith(".."):
            # Two spellings of the same place that realpath could not
            # reconcile: fail OPEN for this dir rather than mis-query git.
            return set()
        prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        queries = [prefix + n for n in names]
        hits = oracle.ignored(queries)
        if oracle.broken:
            raise AppFileError(
                "git check-ignore stopped answering while reading the app "
                "folder's gitignore rules; retry the export"
            )
        return {n for n, q in zip(names, queries) if q in hits}

    def close(self) -> None:
        for o in self._oracles.values():
            o.close()
        self._oracles.clear()


def _iter_app_files(app_dir: str):
    """Yield (abs_path, folder-relative forward-slash path) for every exported
    file: symlinks skipped (a link's target is outside the author's folder or
    reachable inside it), hidden names and machinery dirs dropped, and
    anything the folder's gitignore rules ignore (see `_IgnoreRoots`) left
    home — a build `dist/`, a `.venv`, data the author never committed.

    git's own view is what's honored: a TRACKED file that happens to match a
    pattern is not ignored, so it ships; the no-repo graft has an empty index
    and applies patterns unconditionally."""
    roots = _IgnoreRoots(app_dir)
    try:
        for dirpath, dirnames, filenames in os.walk(app_dir, followlinks=False):
            roots.enter(dirpath, set(dirnames) | set(filenames))
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".")
                and d not in _SKIP_DIRS
                and not os.path.islink(os.path.join(dirpath, d))
            )
            files = sorted(
                f for f in filenames
                if not f.startswith(".") and f not in _SKIP_FILES
                and not os.path.islink(os.path.join(dirpath, f))
            )
            drop = roots.ignored(dirpath, dirnames + files)
            if drop:
                dirnames[:] = [d for d in dirnames if d not in drop]
                files = [f for f in files if f not in drop]
            rel_dir = os.path.relpath(dirpath, app_dir)
            for fname in files:
                full = os.path.join(dirpath, fname)
                rel = fname if rel_dir == "." else f"{rel_dir}/{fname}".replace(os.sep, "/")
                yield full, rel
    finally:
        roots.close()


def default_file_name(app_dir: str) -> str:
    return os.path.basename(os.path.abspath(app_dir)) + ".fused"


def export_app_file(app_dir: str, out_path: str,
                    preview_bytes: bytes | None = None) -> dict:
    """Write the app folder at ``app_dir`` as a ``.fused`` file at ``out_path``.

    Returns the manifest written into the zip. Raises :class:`AppFileError`
    on anything user-correctable: not an app folder (no page carries the
    marker), or a folder over budget.
    Non-destructive: refuses an existing ``out_path``.

    ``preview_bytes`` is an optional caller-captured card screenshot (D396):
    it becomes the payload's ``preview.png`` ONLY when the folder has no
    authored one — an author's chosen still always wins over a capture. It
    must be a real PNG under the preview cap; anything else raises rather
    than baking a broken thumbnail into the artifact forever. That strictness
    is for a caller that MEANT to supply a still: the export ROUTE, whose
    bytes come off a best-effort screen grab, drops an over-cap capture before
    it gets here, because a failed grab must cost the thumbnail and not the
    export (routers/appfile.api_appfile_export_with_preview).
    """
    app_dir = os.path.abspath(app_dir)
    if not os.path.isdir(app_dir):
        raise AppFileError(f"no such folder: {app_dir}")
    try:
        entry = app_listing.app_entry(app_dir)
    except OSError as exc:
        raise AppFileError(f"cannot read {app_dir}: {exc}")
    if entry is None:
        raise AppFileError(
            f"{app_dir} is not a fused app: no page in it carries "
            '<meta name="fused-app">, so a .fused file would have nothing to open'
        )
    if os.path.exists(out_path):
        raise AppFileError(f"refusing to overwrite existing file: {out_path}")

    entry_rel = os.path.relpath(entry, app_dir).replace(os.sep, "/")
    # The app's stable identity (app_id.py) is minted at creation (routers/
    # apps, inside the boilerplate commit); an app from before that — or one
    # whose creation could not stamp it — gets it minted on its FIRST export
    # and written into the entry page itself — before the members are walked and
    # sized, since stamping changes the entry's bytes. The manifest copies
    # whatever the page carries, so `app_id` in a `.fused` means the tag is
    # in its entry too; when the page cannot take the tag (a read-only extract
    # of an older `.fused`, no <head> anchor, a tree with a git remote that a
    # write would leave dirty) the export goes out with no `app_id` rather
    # than an id the next export would mint afresh.
    app_id = app_identity.app_id(entry)
    if app_id is None and not _has_remote(app_dir):
        app_id = app_identity.ensure(entry, os.path.basename(app_dir))
    members = list(_iter_app_files(app_dir))
    if not any(rel == entry_rel for _, rel in members):
        raise AppFileError(
            f"the app's entry page {entry_rel} is excluded by a gitignore rule "
            "(in the app folder, a parent folder, or the repository), so the "
            ".fused would have nothing to open; un-ignore it and export again"
        )
    if len(members) > MAX_EXPORT_ENTRIES:
        raise AppFileError(
            f"app folder has too many files to export ({len(members)} > {MAX_EXPORT_ENTRIES})"
        )
    total = 0
    for full, rel in members:
        try:
            total += os.path.getsize(full)
        except OSError:
            continue
    if total > MAX_EXPORT_TOTAL_BYTES:
        raise AppFileError(
            f"app folder is too large to export ({total} bytes > {MAX_EXPORT_TOTAL_BYTES})"
        )

    if preview_bytes is not None:
        if len(preview_bytes) > MAX_PREVIEW_BYTES:
            raise AppFileError(
                f"preview image is too large ({len(preview_bytes)} bytes > "
                f"{MAX_PREVIEW_BYTES})")
        if not preview_bytes.startswith(_PNG_MAGIC):
            raise AppFileError("preview image is not a PNG")
        if any(rel == app_listing.PREVIEW_IMAGE_NAME for _, rel in members):
            preview_bytes = None  # the authored still wins

    payload: list[tuple[str, str | bytes]] = []
    if preview_bytes is not None:
        payload.append((app_listing.PREVIEW_IMAGE_NAME, preview_bytes))
    payload.extend((rel, full) for full, rel in members)
    # Build beside the destination, one rename in — out_path is only ever
    # absent or the complete file (same posture as export.py's staged bundle).
    parent = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fused-appfile-", dir=parent)
    os.close(fd)
    try:
        try:
            index: dict = {"name": os.path.basename(app_dir), "entry": entry_rel,
                           "exported_at": _utc_now()}
            if app_id is not None:
                index["app_id"] = app_id
            manifest = container.write(tmp, index, payload)
        except container.ContainerError as exc:
            raise AppFileError(str(exc))
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return manifest


def _has_remote(app_dir: str) -> bool:
    """Whether ``app_dir``'s owning repo has a remote — the discriminator
    `meta_migration._has_remote` uses for "externally synced, hands off":
    stamping a tracked file there leaves the checkout dirty and breaks its
    ``--ff-only`` pull. Managed app repos and the shared `local` repo never
    have one. A failing `git` reads as "has one" — when git cannot answer,
    the safe direction is not writing."""
    from fused_render import app_git

    scope = app_git._repo_scope(app_dir)
    if scope is None:
        return False
    repo_dir, _spec = scope
    try:
        r = app_git._git(repo_dir, "remote")
    except Exception:
        # `_git` may raise TimeoutExpired / OSError (missing or hung git);
        # a git that cannot answer must not fail the export — same "hands
        # off" reading as a non-zero exit.
        return True
    return r.returncode != 0 or bool((r.stdout or "").strip())


def _utc_now() -> str:
    """The export stamp: UTC, ISO-8601, seconds, ``Z`` suffix — one obvious
    shape for a consumer to read or parse (``2026-09-14T10:05:12Z``)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def exported_at(manifest: dict) -> str | None:
    """The manifest's ``exported_at`` if it is a plausible stamp, else None.

    Optional on the read side: v1 zips and v2 files exported before the field
    existed have none. The value comes out of an untrusted file, so it is
    forwarded only as a short string — never parsed here, never raised on."""
    v = manifest.get("exported_at")
    return v if isinstance(v, str) and 0 < len(v) <= 64 else None


def app_id_of(manifest: dict) -> str | None:
    """The manifest's ``app_id`` when it is a well-formed id, else None —
    files exported before the id existed have none, and the value comes out
    of an untrusted file, so it is validated and never used as a path."""
    v = manifest.get("app_id")
    return v if app_identity.is_valid(v) else None


def _entry_problem(entry: object) -> bool:
    # `\\` is rejected outright rather than normalized: the exporter always
    # writes forward slashes, and on Windows a backslash inside a "component"
    # would act as a separator after os.path.join — `..\\..\\x` style entries
    # would escape the extract dir while passing a `/`-split ".." check.
    return (
        not isinstance(entry, str)
        or not entry
        or "\\" in entry
        or os.path.isabs(entry)
        or ".." in entry.split("/")
    )


def read_manifest(fused_path: str) -> dict:
    """The validated manifest of the ``.fused`` file at ``fused_path`` —
    read-only, nothing extracted. A v2 container answers its index (which
    carries ``files``); a v1 zip answers its ``manifest.json``. Both carry
    ``fused_app_file``, ``name`` and a validated ``entry``."""
    if container.is_container(fused_path):
        try:
            index = container.read_index(
                fused_path,
                max_entries=MAX_OPEN_ENTRIES,
                max_entry_bytes=MAX_OPEN_ENTRY_BYTES,
                max_total_bytes=MAX_OPEN_TOTAL_BYTES,
            )
        except container.ContainerError as exc:
            raise AppFileError(str(exc))
        entry = index.get("entry")
        if _entry_problem(entry):
            raise AppFileError(f"invalid entry path in manifest: {entry!r}")
        return index
    return _read_zip_manifest(fused_path)


def _read_zip_manifest(fused_path: str) -> dict:
    """v1: the zip's ``manifest.json``. Kept so every .fused exported before
    the container format keeps opening."""
    try:
        with zipfile.ZipFile(fused_path) as zf:
            # Bounded read, never ZipFile.read(): this runs BEFORE the capped
            # extractor, and a zip's declared sizes are attacker-controlled —
            # an unbounded read of a crafted manifest member is a memory bomb.
            with zf.open(MANIFEST_NAME) as f:
                raw = f.read(_MANIFEST_CAP_BYTES + 1)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise AppFileError(f"not a readable .fused file: {exc}")
    if len(raw) > _MANIFEST_CAP_BYTES:
        raise AppFileError(
            f"manifest.json is too large (> {_MANIFEST_CAP_BYTES} bytes) — not a fused app file"
        )
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise AppFileError(f"invalid manifest.json in .fused file: {exc}")
    if not isinstance(manifest, dict) or manifest.get("fused_app_file") != 1:
        raise AppFileError("not a fused app file (manifest carries no fused_app_file: 1)")
    entry = manifest.get("entry")
    if _entry_problem(entry):
        raise AppFileError(f"invalid entry path in manifest: {entry!r}")
    return manifest


def read_preview(fused_path: str) -> bytes | None:
    """The payload's ``preview.png`` bytes, or None when the file ships
    without one — read-only, single member, nothing extracted (the same
    posture as :func:`read_manifest`; a card thumbnail must never trigger
    extraction). Raises :class:`AppFileError` for an unreadable/invalid
    ``.fused``; a member that is over the cap or not a PNG answers None —
    for a THUMBNAIL, "broken still" and "no still" earn the same fallback."""
    manifest = read_manifest(fused_path)
    if manifest["fused_app_file"] == container.VERSION:
        try:
            raw = container.read_member(
                fused_path, manifest, app_listing.PREVIEW_IMAGE_NAME, MAX_PREVIEW_BYTES)
        except container.ContainerError as exc:
            raise AppFileError(str(exc))
        if raw is None or len(raw) > MAX_PREVIEW_BYTES or not raw.startswith(_PNG_MAGIC):
            return None
        return raw
    root = manifest.get("root") or PAYLOAD_DIR
    if not isinstance(root, str) or "\\" in root or ".." in root.split("/"):
        raise AppFileError(f"invalid root path in manifest: {root!r}")
    member = f"{root}/{app_listing.PREVIEW_IMAGE_NAME}"
    try:
        with zipfile.ZipFile(fused_path) as zf:
            try:
                with zf.open(member) as f:
                    # Bounded read, like the manifest's: declared sizes in an
                    # archive are attacker-controlled.
                    raw = f.read(MAX_PREVIEW_BYTES + 1)
            except KeyError:
                return None
    except (OSError, zipfile.BadZipFile) as exc:
        raise AppFileError(f"not a readable .fused file: {exc}")
    if len(raw) > MAX_PREVIEW_BYTES or not raw.startswith(_PNG_MAGIC):
        return None
    return raw


def _slug(name: str, fallback: str = "app") -> str:
    """A manifest ``name`` reduced to one path-safe segment.

    The name comes out of an attacker-controlled file, so this is the ONLY
    thing that may turn it into a filesystem path: the character class admits
    no separator, no dot and no drive letter, which is what makes ``"../evil"``
    and ``"C:\\evil"`` collapse to ordinary segments instead of escaping the
    directory they are joined to. Shared by the install destination and the
    legacy extract-cache key so the two cannot drift on what an app is called.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or fallback


def _legacy_file_key(fused_path: str, name: str) -> str:
    """The content-addressed extract dir name the run-in-place era used
    (``<slug>-<sha256[:16]>`` under ``appfiles_root()``). Kept only so an
    install can find and retire the extract — and its local ``.fused`` state
    — that an earlier open of THIS file left behind."""
    h = hashlib.sha256()
    with open(fused_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return f"{_slug(name)}-{h.hexdigest()[:16]}"


def shared_dot_fused_dir(app_id: str) -> str:
    """Where the run-in-place era kept an app id's ``.fused`` state:
    ``<home>/fused_data/<app_id>`` (AF-13, retired). An install moves it into
    the installed copy. ``app_id`` must have passed ``app_identity.is_valid``
    — the regex admits nothing but ``[a-z0-9-]``, so it is safe as a path
    segment."""
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "fused_data", app_id)


def _lift_read_only(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            try:
                os.chmod(os.path.join(dirpath, fname), 0o644)
            except OSError:
                continue


# ------------------------------------------------------------------ install
#
# A `.fused` is an INSTALLER, not an app: nothing runs from the file or from a
# cache of it. Opening one shows the install page (templates/fusedapp), and
# the one working copy of the app lives at `<workspace>/app/<slug>` — where
# new apps are created and where the `local/` ones were moved (#1592). The
# copy is an ordinary app folder: editable, heading its own git repo, with
# the app's own `.fused/` state beside its files.
#
# The copy carries an INSTALL STAMP, `<app>/.fused/appfile.json`: the file's
# `exported_at`, when it was installed, where from, and the sha256 of every
# file the installer wrote (the container index already records them). The
# stamp is what tells a later file apart as an update, a downgrade or the
# same version, and whether the copy DIVERGED — any stamped file whose bytes
# no longer hash to the stamp. Files the user added are not divergence.
#
# `.fused/` is never written, listed or deleted by an install or update: the
# merge skips it, the prune touches only stamped paths (an export never
# carries a dotted name), and the stamp itself lives there because it is
# state about this copy, like `meta.json`.

#: The install stamp's name inside `<app>/.fused/`.
STAMP_NAME = "appfile.json"

#: What `install_target` answers for `state`. The page's primary button
#: follows it; every state but `new` and `update` needs `force` on the POST.
STATE_NEW = "new"              # no copy: install
STATE_UPDATE = "update"        # newer file, copy unchanged since install
STATE_DIVERGED = "diverged"    # newer file, copy edited since install
STATE_SAME = "same"            # this exact version is installed
STATE_DOWNGRADE = "downgrade"  # the file is older than the installed one
STATE_UNKNOWN = "unknown"      # no stamp (a clone, a hand copy) or no dates
STATE_CLASH = "clash"          # app/<slug> holds a DIFFERENT app
WARN_STATES = frozenset({STATE_DIVERGED, STATE_SAME, STATE_DOWNGRADE, STATE_UNKNOWN})

#: Divergence hashes the stamped files on every probe; an app can be large.
#: Past this many bytes the probe answers "unknown" instead of stalling.
MAX_DIVERGENCE_BYTES = 256 * 1024 * 1024

#: Top-level names an update never touches in the installed copy. `.venv` is
#: the user's environment (never in a payload), `.fused` is the app's own
#: data/state dir (D548) — losing it is losing the user's data — and `.git`
#: is the copy's repo. The export drops every dotted name, so a payload
#: cannot carry these; the set is the guarantee spelled out.
_INSTALL_KEEP = frozenset({".venv", ".fused", ".git"})

#: Install staging dirs (`app/.install-<slug>-*`) older than this are swept:
#: an interrupted install cannot clean its own.
_INSTALL_STAGING_TTL_S = 24 * 3600

README_NAMES = ("readme.md", "readme.markdown", "readme", "readme.txt")
ICON_NAMES = ("icon.svg", "icon.png")
MAX_README_BYTES = 200_000
MAX_ICON_BYTES = 2 * 1024 * 1024


def install_dir() -> str:
    """The workspace tag folder an app file installs into: ``<workspace>/app``."""
    from fused_render.shell.seed import fused_dir

    return os.path.join(fused_dir(), "app")


def stamp_path(app_dir: str) -> str:
    from fused_render import app_fused_dir

    return os.path.join(app_fused_dir.dot_fused(app_dir), STAMP_NAME)


def read_stamp(app_dir: str) -> dict | None:
    """The install stamp, or None when there is none to read (absent,
    unreadable, malformed, not an object) — a copy that was not installed by
    this code, which the probe reports as ``unknown``."""
    try:
        with open(stamp_path(app_dir), encoding="utf-8") as f:
            stamp = json.load(f)
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


def write_stamp(app_dir: str, manifest: dict, source: str) -> dict:
    """Record what was just written: the file's stamp, the per-file hashes
    out of its index (v1 zips carry none, so the stamp has no `files` and a
    later probe reads the copy as ``unknown`` for divergence), the source."""
    from fused_render import app_fused_dir

    files = {}
    for f in manifest.get("files") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str):
            files[f["path"]] = f["sha256"]
    stamp = {
        "version": 1,
        "exported_at": exported_at(manifest),
        "installed_at": _utc_now(),
        "source": os.path.abspath(source),
        "app_id": app_id_of(manifest),
        "name": manifest.get("name") if isinstance(manifest.get("name"), str) else None,
        "files": files,
    }
    os.makedirs(app_fused_dir.dot_fused(app_dir), exist_ok=True)
    tmp = stamp_path(app_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
        f.write("\n")
    os.replace(tmp, stamp_path(app_dir))
    return stamp


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def divergence(app_dir: str, stamp: dict) -> list[str] | None:
    """The stamped files whose bytes on disk no longer match the stamp, or
    are gone. None when the stamp carries no hashes or the copy is over the
    hashing budget — "cannot tell", which the probe reports as ``unknown``."""
    files = stamp.get("files")
    if not isinstance(files, dict) or not files:
        return None
    changed: list[str] = []
    total = 0
    for rel in sorted(files):
        digest = files[rel]
        if container.path_problem(rel) or not isinstance(digest, str):
            continue
        p = os.path.join(app_dir, *rel.split("/"))
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                changed.append(rel)
                continue
            total += os.path.getsize(p)
            if total > MAX_DIVERGENCE_BYTES:
                return None
            if _sha256_file(p) != digest:
                changed.append(rel)
        except OSError:
            changed.append(rel)
    return changed


def workspace_app_with_id(app_id: str) -> str | None:
    """The workspace app folder whose entry page declares ``app_id``, or
    None — the whole walk (`app/`, a `local/` the migration skipped, any tag),
    so a copy the user renamed or moved is still this app. Never joins the
    id into a path."""
    from fused_render.shell.seed import fused_dir

    try:
        rows = app_listing.discover_workspace_apps(fused_dir())
    except Exception:  # noqa: BLE001 — an unwalkable workspace holds no copy we can name
        return None
    for row in rows:
        entry = row.get("entry_html")
        if not entry:
            continue
        try:
            if app_identity.app_id(entry) == app_id:
                return _workspace_spelling(row["path"])
        except OSError:
            continue
    return None


def _workspace_spelling(path: str) -> str:
    """``path`` (a listing row's REALPATH) respelled under ``fused_dir()`` as
    the user configured it. `app_git.app_dir_for` scopes commits by that
    spelling's string prefix, so a workspace reached through a symlink
    (macOS's `/var` → `/private/var`, a linked `~/Fused`) would otherwise
    make every install commit a silent no-op."""
    from fused_render.shell.seed import fused_dir

    root = fused_dir()
    real_root = os.path.realpath(root)
    rp = os.path.realpath(path)
    if rp == real_root:
        return root
    if rp.startswith(real_root.rstrip(os.sep) + os.sep):
        return os.path.join(root, os.path.relpath(rp, real_root))
    return path


def _installed_identity(app_dir: str) -> str | None:
    try:
        entry = app_listing.app_entry(app_dir)
    except OSError:
        return None
    return app_identity.app_id(entry) if entry else None


def _find_member(manifest: dict, names: tuple[str, ...]) -> str | None:
    """The first index path (top level only) whose lowercased name is in
    ``names``; None for a v1 zip, which lists no members."""
    for f in manifest.get("files") or []:
        rel = f.get("path") if isinstance(f, dict) else None
        if isinstance(rel, str) and "/" not in rel and rel.lower() in names:
            return rel
    return None


def read_readme(fused_path: str, manifest: dict | None = None) -> str | None:
    """The payload's README text (bounded), or None — read-only, one member,
    nothing extracted. v1 zips answer None."""
    manifest = manifest or read_manifest(fused_path)
    if manifest.get("fused_app_file") != container.VERSION:
        return None
    rel = _find_member(manifest, README_NAMES)
    if rel is None:
        return None
    try:
        raw = container.read_member(fused_path, manifest, rel, MAX_README_BYTES)
    except container.ContainerError as exc:
        raise AppFileError(str(exc))
    if raw is None:
        return None
    return raw[:MAX_README_BYTES].decode("utf-8", "replace")


def read_icon(fused_path: str, manifest: dict | None = None) -> tuple[str, bytes] | None:
    """``(mime, bytes)`` of the payload's icon, or None. Bounded single-member
    read like :func:`read_preview`; over the cap is "no icon"."""
    manifest = manifest or read_manifest(fused_path)
    if manifest.get("fused_app_file") != container.VERSION:
        return None
    rel = _find_member(manifest, ICON_NAMES)
    if rel is None:
        return None
    try:
        raw = container.read_member(fused_path, manifest, rel, MAX_ICON_BYTES)
    except container.ContainerError as exc:
        raise AppFileError(str(exc))
    if raw is None or len(raw) > MAX_ICON_BYTES:
        return None
    mime = "image/svg+xml" if rel.lower().endswith(".svg") else "image/png"
    return mime, raw


def _has_preview(fused_path: str, manifest: dict) -> bool:
    """Whether the page should ask `/api/appfile/preview`: the index says so
    for a v2 container; a v1 zip lists no members, so its `preview.png` is
    looked up the way that route reads it (one bounded member read)."""
    if manifest.get("fused_app_file") == container.VERSION:
        return _find_member(manifest, (app_listing.PREVIEW_IMAGE_NAME,)) is not None
    try:
        return read_preview(fused_path) is not None
    except AppFileError:
        return False


def install_target(fused_path: str, *, light: bool = False) -> dict:
    """What installing the ``.fused`` at ``fused_path`` would do — the install
    page's data and the POST's precondition. Read-only: one bounded manifest
    read, the workspace walk for the id, and (unless ``light``) the hash of
    the installed copy's stamped files.

    Identity: the file's ``app_id`` (D884) first — the workspace folder whose
    entry declares it is the installed copy, wherever it sits — else the
    slug of the manifest name (the file stem when empty) under ``app/``.
    ``light`` skips the divergence hashing (a card thumbnail, D396's rule:
    a peek never pays for the open)."""
    fused_path = os.path.abspath(fused_path)
    if not os.path.isfile(fused_path):
        raise AppFileError(f"no such file: {fused_path}")
    manifest = read_manifest(fused_path)
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else ""
    stem = os.path.splitext(os.path.basename(fused_path))[0]
    slug = _slug(name, fallback=_slug(stem))
    app_id = app_id_of(manifest)
    incoming_at = exported_at(manifest)

    dest = os.path.join(install_dir(), slug)
    found_by: str | None = None
    by_id = workspace_app_with_id(app_id) if app_id else None
    if by_id is not None:
        dest, found_by = by_id, "id"
    elif os.path.isdir(dest):
        found_by = "slug"
    installed = found_by is not None

    state = STATE_NEW
    changed: list[str] = []
    stamp: dict | None = None
    other_name: str | None = None
    if installed:
        stamp = read_stamp(dest)
        other = _installed_identity(dest) if found_by == "slug" else None
        if other and other != app_id:
            # The slug is taken by a folder declaring a different id — or any
            # id, when this file has none and so cannot prove it is the same
            # app: never overwrite it.
            state = STATE_CLASH
            other_name = os.path.basename(dest)
        else:
            stamped_at = stamp.get("exported_at") if stamp else None
            if not isinstance(stamped_at, str) or not stamped_at or not incoming_at:
                state = STATE_UNKNOWN
            elif incoming_at > stamped_at:
                if light:
                    state = STATE_UPDATE
                else:
                    diff = divergence(dest, stamp or {})
                    if diff is None:
                        state = STATE_UNKNOWN
                    elif diff:
                        state, changed = STATE_DIVERGED, diff
                    else:
                        state = STATE_UPDATE
            elif incoming_at == stamped_at:
                state = STATE_SAME
            else:
                state = STATE_DOWNGRADE

    files = []
    total = 0
    for f in manifest.get("files") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("size"), int):
            files.append({"path": f["path"], "size": f["size"]})
            total += f["size"]
    files.sort(key=lambda f: f["path"])

    entry_abs = os.path.join(dest, *manifest["entry"].split("/")) if installed else None
    out = {
        "name": name or stem,
        "slug": slug,
        "app_id": app_id,
        "path": dest.replace(os.sep, "/"),
        "installed": installed,
        "found_by": found_by,
        "state": state,
        "needs_force": state in WARN_STATES,
        "exported_at": incoming_at,
        "installed_exported_at": (stamp or {}).get("exported_at") if stamp else None,
        "installed_at": (stamp or {}).get("installed_at") if stamp else None,
        "changed": changed,
        "other_name": other_name,
        "entry": manifest["entry"],
        "entry_path": entry_abs.replace(os.sep, "/") if entry_abs and os.path.isfile(entry_abs) else None,
        "files": files,
        "bytes": total,
        "format": manifest.get("fused_app_file"),
        "has_icon": _find_member(manifest, ICON_NAMES) is not None,
        "has_preview": _has_preview(fused_path, manifest),
    }
    if not light:
        out["readme"] = read_readme(fused_path, manifest)
    return out


def _extract_payload(fused_path: str, manifest: dict, staging: str) -> str:
    """Unpack the file into ``<staging>/files`` through the one hardened
    extractor per format, check the entry is there and still carries the
    marker, lift the mode bits, answer the payload dir."""
    payload = os.path.join(staging, PAYLOAD_DIR)
    entry_rel = manifest["entry"]
    if manifest["fused_app_file"] == container.VERSION:
        try:
            container.extract(fused_path, manifest, payload)
        except container.ContainerError as exc:
            raise AppFileError(str(exc))
    else:
        try:
            with zipfile.ZipFile(fused_path) as zf:
                extract_to_staging(
                    zf,
                    staging,
                    max_entries=MAX_OPEN_ENTRIES,
                    max_entry_bytes=MAX_OPEN_ENTRY_BYTES,
                    max_total_bytes=MAX_OPEN_TOTAL_BYTES,
                )
        except (ZipRejected, ZipTooLarge, zipfile.BadZipFile) as exc:
            raise AppFileError(str(exc))
    if not os.path.isdir(payload):
        raise AppFileError(f"the .fused file has no {PAYLOAD_DIR}/ payload directory")
    staged_entry = os.path.join(payload, *entry_rel.split("/"))
    if not os.path.isfile(staged_entry):
        raise AppFileError(f"entry page {entry_rel!r} is missing from the .fused payload")
    if not app_listing.has_fused_meta(staged_entry):
        raise AppFileError(
            f"entry page {entry_rel!r} does not carry <meta name=\"fused-app\"> — "
            "not a fused app"
        )
    # An installed copy is the user's to edit: no 0444 from any extractor.
    _lift_read_only(payload)
    return payload


def _sweep_install_staging(root: str) -> None:
    """Remove `.install-*` dirs under ``root`` older than the TTL — never
    anything else: ``root`` is `app/`, where the user's apps live."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    cutoff = time.time() - _INSTALL_STAGING_TTL_S
    for n in names:
        if not n.startswith(".install-"):
            continue
        p = os.path.join(root, n)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue


def install_app_file(fused_path: str, *, force: bool = False) -> dict:
    """Install the ``.fused`` at ``fused_path`` into the workspace, or update
    the copy already there. Answers :func:`install_target`'s shape as it
    stands AFTER the write, plus ``action`` (``installed`` | ``updated``),
    ``entry`` (the copy's entry page, absolute), ``pruned`` (stamped files
    the author deleted that were removed) and ``adopted_state`` (the
    run-in-place state dir that was moved in, if any).

    A state in ``WARN_STATES`` (diverged, same, downgrade, unknown) refuses
    without ``force`` — the page shows the warning and asks again; ``clash``
    refuses always. ``.fused/`` is never touched (module comment).

    **New copy:** unpacked into a staging dir INSIDE `app/` (a same-filesystem
    rename claims it), the run-in-place era's state for this app adopted into
    the staging payload, then one rename, `.fused/` scaffolded, the stamp
    written, and `app_git.init_repo` so the copy heads its own repo like a
    newly created `app/` app.

    **Update / overwrite:** the payload is merged over the copy (`_merge_tree`:
    payload files replace their counterparts; `.venv`, `.fused`, `.git` and
    anything the payload does not name stay), then files the OLD stamp named
    that the new index does not — and whose bytes still match the old stamp,
    so they are the author's deletion and not the user's work — are removed,
    the stamp rewritten and one commit made. Not atomic (a merge over a live
    folder has no staging trick that avoids copying the `.venv`); a retry
    finishes the job.
    """
    target = install_target(fused_path)
    state = target["state"]
    if state == STATE_CLASH:
        raise AppFileError(
            f"{target['path']} already holds a different app ({target['other_name']}); "
            "rename or uninstall it first")
    if state in WARN_STATES and not force:
        raise InstallNeedsConfirm(target)
    manifest = read_manifest(fused_path)
    app_id = target["app_id"]
    root = install_dir()
    os.makedirs(root, exist_ok=True)
    _sweep_install_staging(root)
    dest = os.path.normpath(target["path"])
    staging = tempfile.mkdtemp(dir=root, prefix=f".install-{target['slug']}-")
    pruned: list[str] = []
    adopted: str | None = None
    try:
        payload = _extract_payload(fused_path, manifest, staging)
        if not target["installed"]:
            adopted = _adopt_legacy_state(payload, app_id, fused_path, manifest, final_dir=dest)
            try:
                os.rename(payload, dest)
            except OSError:
                if not os.path.isdir(dest):
                    raise AppFileError(f"could not place the installed app at {dest}")
                # Something claimed `dest` during the extract. Ask again who —
                # the FULL probe, divergence hashing included: a concurrent
                # install of the SAME app at this very path is the one
                # working copy and gets updated; anything else (another app's
                # folder, the app's copy appearing elsewhere, a copy that now
                # needs confirmation) is refused exactly as a first probe
                # would have refused it, and a warn state reaches the page as
                # the same 409 so it can show the confirm.
                again = install_target(fused_path)
                if (again["state"] == STATE_CLASH or not again["installed"]
                        or os.path.normcase(os.path.normpath(again["path"]))
                        != os.path.normcase(os.path.normpath(dest))):
                    raise AppFileError(
                        f"{dest} appeared during the install and is not this app's copy; "
                        "open the file again")
                if again["needs_force"] and not force:
                    raise InstallNeedsConfirm(again)
                # The state adopted into this staging goes with it when the
                # winner has none yet, so an adopted state is never lost to
                # the staging sweep below.
                from fused_render import app_fused_dir

                staged_dot = app_fused_dir.dot_fused(payload)
                if adopted and os.path.isdir(staged_dot) and not os.path.lexists(app_fused_dir.dot_fused(dest)):
                    shutil.move(staged_dot, app_fused_dir.dot_fused(dest))
                    _repoint_meta(dest, dest)
                _merge_tree(payload, dest, skip=_INSTALL_KEEP)
            action = "installed"
        else:
            old = read_stamp(dest)
            _merge_tree(payload, dest, skip=_INSTALL_KEEP)
            pruned = _prune_author_deleted(dest, old, manifest)
            adopted = _adopt_legacy_state(dest, app_id, fused_path, manifest, final_dir=dest)
            action = "updated"
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    from fused_render import app_fused_dir

    app_fused_dir.ensure(dest)
    write_stamp(dest, manifest, fused_path)
    _git_trail(dest, action, fused_path, exported_at(manifest))
    _retire_legacy_extracts(app_id, fused_path, manifest)
    entry = os.path.join(dest, *manifest["entry"].split("/"))
    after = install_target(fused_path, light=True)
    return {**after, "action": action, "entry": entry.replace(os.sep, "/"),
            "pruned": pruned, "adopted_state": adopted}


def _prune_author_deleted(dest: str, old_stamp: dict | None, manifest: dict) -> list[str]:
    """Remove files the OLD stamp named that the new index does not carry —
    only when their bytes still match the old stamp (the author deleted them
    between exports and the user never touched them). A v1 zip lists no
    members, so nothing is pruned against or for one."""
    if not old_stamp or not isinstance(old_stamp.get("files"), dict):
        return []
    if manifest.get("fused_app_file") != container.VERSION:
        return []
    new_paths = {f["path"] for f in manifest.get("files") or []
                 if isinstance(f, dict) and isinstance(f.get("path"), str)}
    pruned: list[str] = []
    for rel, digest in sorted(old_stamp["files"].items()):
        if rel in new_paths or container.path_problem(rel) or not isinstance(digest, str):
            continue
        if rel.split("/", 1)[0] in _INSTALL_KEEP:
            continue
        p = os.path.join(dest, *rel.split("/"))
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            if _sha256_file(p) != digest:
                continue  # the user's edit, not the author's deletion
            os.remove(p)
            pruned.append(rel)
            # Empty parents up to the app root go too; a folder the author
            # emptied is one they deleted.
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


def _adopt_legacy_state(app_dir: str, app_id: str | None, fused_path: str,
                        manifest: dict, *, final_dir: str) -> str | None:
    """Move the run-in-place era's ``.fused`` state for this app INTO the
    copy when the copy has none yet: the shared ``fused_data/<app_id>`` dir
    (AF-13) for a file with an id, else the ``.fused`` of the extract an
    earlier open of THIS file made. ``meta.json`` is repointed at
    ``final_dir`` (the witness would otherwise see the extract path, which
    the retire below deletes, and read a move). Answers the dir moved, or
    None. Best-effort: a state that cannot be moved stays where it is."""
    from fused_render import app_fused_dir

    dot = app_fused_dir.dot_fused(app_dir)
    if os.path.lexists(dot):
        return None
    candidates: list[str] = []
    if app_id:
        candidates.append(shared_dot_fused_dir(app_id))
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else "app"
    try:
        candidates.append(os.path.join(appfiles_root(), _legacy_file_key(fused_path, name), ".fused"))
    except OSError:
        pass
    for src in candidates:
        try:
            if not os.path.isdir(src) or os.path.islink(src):
                continue
            shutil.move(src, dot)
            _repoint_meta(app_dir, final_dir)
            return src
        except OSError:
            continue
    return None


def _repoint_meta(app_dir: str, final_dir: str) -> None:
    """Settle an adopted state dir's `meta.json` on the installed copy, the
    origin kept in `migrations` as evidence (`sessions: 0`, `installed`)."""
    from fused_render import app_fused_dir

    meta = app_fused_dir.read_meta(app_dir)
    if meta is None:
        return
    recorded = meta.get("app_dir")
    new = os.path.abspath(final_dir)
    if isinstance(recorded, str) and os.path.normcase(os.path.abspath(recorded)) == os.path.normcase(new):
        return
    migrations = meta.get("migrations")
    if not isinstance(migrations, list):
        migrations = []
    migrations.append({"from": recorded, "to": new, "at": _utc_now(),
                       "sessions": 0, "installed": True})
    meta.update(app_dir=new, migrations=migrations)
    tmp = app_fused_dir.meta_path(app_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    os.replace(tmp, app_fused_dir.meta_path(app_dir))


def _retire_legacy_extracts(app_id: str | None, fused_path: str, manifest: dict) -> None:
    """Delete the run-in-place extracts of this app under ``appfiles_root()``:
    the one for THIS file's bytes, and (with an id) every extract whose entry
    declares the id. Nothing runs from them any more. A `.fused` symlink in
    one is unlinked, never followed. Best-effort."""
    root = appfiles_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else "app"
    try:
        mine = _legacy_file_key(fused_path, name)
    except OSError:
        mine = None
    for n in names:
        if n.startswith("."):
            continue
        d = os.path.join(root, n)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        match = n == mine
        if not match and app_id:
            match = _installed_identity(d) == app_id
        if not match:
            continue
        dot = os.path.join(d, ".fused")
        try:
            if os.path.islink(dot):
                os.unlink(dot)
            _lift_read_only(d)
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _git_trail(dest: str, action: str, fused_path: str, stamp: str | None) -> None:
    """Version control: a fresh install heads its own repo like a newly
    created `app/` app (`init_repo`, one commit); an update lands one commit
    naming the file and its export stamp, so a diverged user can diff their
    work against the author's version afterwards. Best-effort — no git on
    the machine still gets a working app."""
    from fused_render import app_git

    label = os.path.basename(fused_path)
    try:
        if action == "installed":
            app_git.init_repo(dest, message=f"Installed from {label}")
        else:
            app_git.commit(dest, f"Updated from {label} ({stamp or 'no export date'})")
    except Exception:  # noqa: BLE001
        logger.debug("git trail skipped for %s", dest, exc_info=True)


def _merge_tree(src: str, dst: str, skip: frozenset[str] = frozenset()) -> None:
    """Lay ``src``'s files over ``dst`` at every depth, the payload's SHAPE
    winning wherever the two disagree: a file, symlink or anything else in the
    copy where the payload has a folder is removed and a real folder made; a
    real folder in the copy where the payload has a file is removed. Entries
    of ``dst`` the payload does not name are left alone. Our own walk rather
    than ``copytree(dirs_exist_ok=True)``: that one raises on a nested
    file-vs-folder clash (a partial merge no retry can finish) and ENTERS a
    directory symlink in the copy, writing the payload — and a trailing
    ``copystat`` — onto whatever it points at. ``skip`` names top-level
    entries of ``src`` never copied."""
    for name in sorted(os.listdir(src)):
        if name in skip:
            continue
        s_path = os.path.join(src, name)
        d_path = os.path.join(dst, name)
        if os.path.isdir(s_path) and not os.path.islink(s_path):
            # A symlink at d_path is unlinked, never entered — even one that
            # resolves to a directory — so nothing lands on its target.
            if os.path.islink(d_path) or (os.path.lexists(d_path) and not os.path.isdir(d_path)):
                os.unlink(d_path)
            os.makedirs(d_path, exist_ok=True)
            _merge_tree(s_path, d_path)
        elif os.path.isfile(s_path) and not os.path.islink(s_path):
            if os.path.isdir(d_path) and not os.path.islink(d_path):
                shutil.rmtree(d_path)
            _copy_over(s_path, d_path)
        # A symlink or special file in the payload is not something the
        # export produces (symlinks are skipped on the way out); ignored.


def _copy_over(src: str, dst: str) -> None:
    """Replace ``dst``'s bytes with ``src``'s, touching no mode but ``dst``'s
    own when it is read-only. A symlink at ``dst`` is unlinked rather than
    written THROUGH — the payload's file replaces the link, and whatever it
    pointed at is left alone."""
    if os.path.islink(dst):
        os.unlink(dst)
    elif os.path.isfile(dst) and not os.access(dst, os.W_OK):
        os.chmod(dst, 0o644)
    shutil.copyfile(src, dst)
