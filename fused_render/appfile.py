"""Single-file app export/open: the ``.fused`` app file (SPEC §43, D384-D386).

A ``.fused`` file is one double-clickable artifact holding a whole fused app —
the folder the /apps hub lists (its marker-carrying entry page, every other
page, the ``.py`` data files, assets, ``pyproject.toml``). Export walks the app
FOLDER (the app's own unit of identity, D301) rather than reusing
``export.py``'s per-page static scan: the scan exists to build a minimal
*hosted* bundle from one page and misses computed paths and sibling pages,
while a ``.fused`` targets the opposite trade — carry everything the author
put in the folder, because the thing that opens it is a full fused-render
runtime, not a hosting layer.

Physically the file is a zip: ``manifest.json`` at the root plus a single
``files/`` payload dir mirroring the app folder (same payload-dir shape as
bundle v2, docs/bundle-v2-design.md, so the two artifact families read alike).
The manifest records the format tag and the entry page's payload-relative
path, resolved at *export* time by the one shared entry rule
(``app_listing.app_entry``) so exporter and hub can never disagree about which
page an app opens on.

Opening is extract-then-render, never render-from-archive: the payload lands
in a content-addressed cache dir under ``~/.fused-render/appfiles/`` (keyed by
the file's sha256, so re-opening the same file re-uses the extract and a
changed file gets a fresh dir), every extracted file is chmod'd read-only
(0o444 — the same bit the archive-member preview uses, RO-7, which makes
``fused.writeFile`` refuse with the existing ``readonly`` error rather than
needing a new enforcement surface), and the browser lands on the entry page in
**embed mode** (chrome-free: no sidebar, no editor, no Claude — the app as it
is, nothing else). Extraction rides the one hardened unzip implementation
(``zip_import``): a ``.fused`` that arrived by mail is exactly as untrusted as
an uploaded template pack.

The trust boundary is the confirm page (``static/openfused.html``, served by
GET /openfused — same posture as the deep-link clone confirm, D110): a
double-click alone never executes anything; only the explicit confirm click
fires the X-Fused-guarded POST /api/appfile/open, and the page says plainly
that the app can run Python on this machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile

from fused_render import app_listing
from fused_render.zip_import import ZipRejected, ZipTooLarge, extract_to_staging, sweep_stale_staging

# The payload dir inside the zip, mirroring the app folder — the same
# single-payload-dir shape as bundle v2's `files/` (docs/bundle-v2-design.md).
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

# Directory/file names never exported. Dotted names (`.git`, `.claude`,
# `.venv`, `.DS_Store`, `.env` — which may hold secrets) are dropped by the
# hidden-name rule; these are the non-dotted machinery dirs on top of it.
_SKIP_DIRS = frozenset({"node_modules", "__pycache__"})

# CLAUDE.md is the app's AUTHORING contract (scaffolded by app_starter for the
# agent that edits the app); a .fused is the app as a user-facing artifact with
# no editing surface behind it, so the instructions file stays home.
_SKIP_FILES = frozenset({"CLAUDE.md"})

# fused.ai() is deliberately ALLOWED in a .fused, unlike the hosted exporter
# (RH-11): a hosted page has no runtime behind it, but an opened .fused runs
# inside the recipient's full local fused-render, where /api/ai exists. A
# recipient without the claude CLI or a resident local model gets the API's
# own graceful `ai_unavailable` rejection, which pages are already written to
# handle (the authoring skill's error table). "No claude" in this artifact's
# contract means no EDITING surface — embed mode strips that — not no AI.
# (Owner call, D387, reversing the D384 stance.)

# Stale-extract sweep: an interrupted open cannot clean its staging dir.
_STAGING_TTL_SECONDS = 24 * 3600


class AppFileError(Exception):
    """User-correctable failure exporting or opening a ``.fused`` file; the
    message goes verbatim into the route's 400 body."""


def appfiles_root() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "appfiles")


def _iter_app_files(app_dir: str):
    """Yield (abs_path, folder-relative forward-slash path) for every exported
    file: symlinks skipped (a link's target is outside the author's folder or
    reachable inside it), hidden names and machinery dirs dropped."""
    for dirpath, dirnames, filenames in os.walk(app_dir, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in _SKIP_DIRS
            and not os.path.islink(os.path.join(dirpath, d))
        )
        rel_dir = os.path.relpath(dirpath, app_dir)
        for fname in sorted(filenames):
            if fname.startswith(".") or fname in _SKIP_FILES:
                continue
            full = os.path.join(dirpath, fname)
            if os.path.islink(full):
                continue
            rel = fname if rel_dir == "." else f"{rel_dir}/{fname}".replace(os.sep, "/")
            yield full, rel


def default_file_name(app_dir: str) -> str:
    return os.path.basename(os.path.abspath(app_dir)) + ".fused"


def export_app_file(app_dir: str, out_path: str) -> dict:
    """Write the app folder at ``app_dir`` as a ``.fused`` file at ``out_path``.

    Returns the manifest written into the zip. Raises :class:`AppFileError`
    on anything user-correctable: not an app folder (no page carries the
    marker), or a folder over budget.
    Non-destructive: refuses an existing ``out_path``.
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
    members = list(_iter_app_files(app_dir))
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

    manifest = {
        "fused_app_file": 1,
        "root": PAYLOAD_DIR,
        "name": os.path.basename(app_dir),
        "entry": entry_rel,
    }
    # Build beside the destination, one rename in — out_path is only ever
    # absent or the complete file (same posture as export.py's staged bundle).
    parent = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fused-appfile-", suffix=".zip", dir=parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            for full, rel in members:
                zf.write(full, arcname=f"{PAYLOAD_DIR}/{rel}")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return manifest


def read_manifest(fused_path: str) -> dict:
    """The validated manifest of the ``.fused`` file at ``fused_path`` —
    read-only (the confirm page's preview), nothing extracted."""
    try:
        with zipfile.ZipFile(fused_path) as zf:
            raw = zf.read(MANIFEST_NAME)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise AppFileError(f"not a readable .fused file: {exc}")
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise AppFileError(f"invalid manifest.json in .fused file: {exc}")
    if not isinstance(manifest, dict) or manifest.get("fused_app_file") != 1:
        raise AppFileError("not a fused app file (manifest carries no fused_app_file: 1)")
    entry = manifest.get("entry")
    if not isinstance(entry, str) or not entry or os.path.isabs(entry) or ".." in entry.split("/"):
        raise AppFileError(f"invalid entry path in manifest: {entry!r}")
    return manifest


def _file_key(fused_path: str, name: str) -> str:
    """Cache dir name: slug of the app name + content hash. Content-addressed
    so re-opening the same bytes re-uses the extract and an edited/re-exported
    file lands in a fresh dir instead of mixing with the old one."""
    h = hashlib.sha256()
    with open(fused_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"
    return f"{slug}-{h.hexdigest()[:16]}"


def _make_read_only(root: str) -> None:
    # Files 0o444 (RO-7's bit: stat reports writable=false, /api/fs/write
    # refuses, writeFile surfaces err.type "readonly"). Directories keep their
    # mode: traversal must work, and a read-only PROJECT FOLDER is already a
    # solved case for env installs (D376's manifest-only mirror).
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            try:
                os.chmod(os.path.join(dirpath, fname), 0o444)
            except OSError:
                continue


def open_app_file(fused_path: str) -> dict:
    """Extract the ``.fused`` file into the content-addressed cache (re-using
    a prior extract of the same bytes) and return
    ``{"dir", "entry", "name", "reused"}`` with absolute paths.

    The extracted entry page must still carry the fused-app marker — the
    manifest names the entry, but the marker is what the /apps hub and the
    render-time open-recording key on (D301), so a payload whose page lost it
    is refused rather than opened as a non-app.
    """
    fused_path = os.path.abspath(fused_path)
    if not os.path.isfile(fused_path):
        raise AppFileError(f"no such file: {fused_path}")
    manifest = read_manifest(fused_path)
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else "app"
    root = appfiles_root()
    # Staging lives in its OWN subdir, and only that subdir is swept: the root
    # holds the long-lived content-addressed extracts, whose mtime is their
    # extract time and never advances — sweeping the root would rmtree a
    # perfectly live app 24h after it was opened (hub card gone, open tab's
    # calls failing mid-session). Extracts are evicted only by a re-open of
    # changed bytes rebuilding its own key, never by age.
    staging_root = os.path.join(root, ".staging")
    os.makedirs(staging_root, exist_ok=True)
    sweep_stale_staging(staging_root, _STAGING_TTL_SECONDS)

    dest = os.path.join(root, _file_key(fused_path, name))
    entry_rel = manifest["entry"]
    entry_abs = os.path.join(dest, *entry_rel.split("/"))
    if os.path.isdir(dest):
        if os.path.isfile(entry_abs) and app_listing.has_fused_meta(entry_abs):
            return {"dir": dest, "entry": entry_abs, "name": name, "reused": True}
        # A half-extracted or manually-damaged cache dir: rebuild it. Files
        # are 0o444, so lift the bit before removing.
        shutil.rmtree(dest, ignore_errors=True)
        if os.path.isdir(dest):
            _lift_read_only(dest)
            shutil.rmtree(dest, ignore_errors=True)

    staging = tempfile.mkdtemp(prefix="open-", dir=staging_root)
    try:
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
        payload = os.path.join(staging, PAYLOAD_DIR)
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
        _make_read_only(payload)
        try:
            os.rename(payload, dest)
        except OSError:
            # A concurrent open of the same file won the rename — same bytes,
            # same content key, so the winner's extract is ours too.
            if not (os.path.isfile(entry_abs) and app_listing.has_fused_meta(entry_abs)):
                raise AppFileError(f"could not place the extracted app at {dest}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"dir": dest, "entry": entry_abs, "name": name, "reused": False}


def _lift_read_only(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            try:
                os.chmod(os.path.join(dirpath, fname), 0o644)
            except OSError:
                continue
