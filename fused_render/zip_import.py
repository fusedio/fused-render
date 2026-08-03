"""Hardened zip unpacking, shared by every feature that accepts an archive.

Extracted from ``templates_api`` (the template-pack import, SPEC §2.6) when app cloning
needed the same guarantees. It is deliberately **one** implementation: an archive arriving
from a URL the user pasted is no more trustworthy than one they uploaded, and a second
extractor written "just for clones" is how a hardened path and an unhardened one end up
side by side.

What it guards, and why each guard exists rather than being obvious:

- **zip-slip** — absolute paths, ``..`` segments, and backslash-separated names that
  normalize outside the staging root. Checked on every entry *before* anything is written,
  so a rejected archive never leaves a partial directory behind.
- **symlink entries** — refused outright. Extracting one lets a later entry (or the user's
  own later action) write through it to any path the process can reach.
- **entry count** — a bound on how many members an archive may declare, so a zip made of
  millions of tiny entries cannot exhaust inodes or wall-clock.
- **decompressed size, per entry and in total** — enforced on the bytes **actually
  written**, never on ``ZipInfo.file_size``. That field is attacker-controlled: a crafted
  archive can understate it, so trusting it is how a "50 MB cap" extracts 5 GB.
- **staging, then commit** — everything lands in a throwaway directory first, so a caller
  can inspect the result and abort without having touched the destination. Stale stages are
  swept opportunistically, since a crashed caller cannot clean up after itself.

The size/count limits are parameters rather than constants: the template-pack import and a
page clone have genuinely different budgets (a page bundle's ceiling is the serve path's
own clone cap), and a shared module should not force one to inherit the other's.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import time
import zipfile

#: Bounded read size during extraction. Chunked so the size caps are checked *while*
#: decompressing rather than after a whole entry is already in memory.
COPY_CHUNK = 64 * 1024


class ZipTooLarge(Exception):
    """A zip breached a size cap mid-extraction.

    Raised during the copy, not before it: the caps are enforced on bytes actually
    decompressed, because the declared sizes in the archive cannot be trusted.
    """


class ZipRejected(Exception):
    """A zip was refused before any bytes were written (unsafe entry, or too many)."""


def is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    """Does this entry describe a symlink? (Unix mode bits live in the high half of
    ``external_attr``.)"""
    return stat_mod.S_ISLNK(info.external_attr >> 16)


def reject_reason(info: zipfile.ZipInfo, staging_dir: str) -> str | None:
    """Why this entry is unsafe to extract, or ``None`` if it is safe.

    Guards zip-slip (absolute paths, ``..`` escapes, out-of-root targets) and symlink
    entries. The final containment check is deliberately redundant with the ``..`` scan:
    normalization catches shapes a per-segment scan does not (``a/./../..`` style
    sequences, or a name whose separators only become meaningful after normalizing).
    """
    name = info.filename
    if is_symlink_entry(info):
        return f"symlink entry not allowed: {name!r}"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or os.path.isabs(name):
        return f"absolute path not allowed: {name!r}"
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        return f"path escape ('..') not allowed: {name!r}"
    target = os.path.normpath(os.path.join(staging_dir, normalized))
    root = os.path.normpath(staging_dir)
    if target != root and not target.startswith(root + os.sep):
        return f"path escapes the staging directory: {name!r}"
    return None


def sweep_stale_staging(root: str, ttl_seconds: float) -> None:
    """Remove staging dirs older than ``ttl_seconds`` (opportunistic, best-effort).

    A caller that crashes between staging and commit cannot clean up after itself, so the
    next caller does it. Never raises: a sweep failure must not fail the operation that
    triggered it.
    """
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and (now - os.path.getmtime(path)) > ttl_seconds:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def validate_entries(
    infos: list[zipfile.ZipInfo], staging_dir: str, *, max_entries: int
) -> None:
    """Check every entry before writing any of them; raise :class:`ZipRejected` on the
    first unsafe one. Validating up front is what makes a rejected archive leave nothing
    behind."""
    if len(infos) > max_entries:
        raise ZipRejected(f"zip has too many entries ({len(infos)} > {max_entries})")
    for info in infos:
        reason = reject_reason(info, staging_dir)
        if reason is not None:
            raise ZipRejected(f"rejected zip: {reason}")


def extract_to_staging(
    zf: zipfile.ZipFile,
    staging_dir: str,
    *,
    max_entries: int,
    max_entry_bytes: int,
    max_total_bytes: int,
) -> int:
    """Validate, then extract ``zf`` into ``staging_dir``; return the total bytes written.

    Creates ``staging_dir``. On any failure the whole directory is removed, so the caller
    never inherits a half-extracted stage — and :class:`ZipTooLarge` / :class:`ZipRejected`
    carry a message fit to show a user.
    """
    infos = zf.infolist()
    validate_entries(infos, staging_dir, max_entries=max_entries)
    os.makedirs(staging_dir, exist_ok=True)
    total_written = 0
    try:
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            target = os.path.normpath(os.path.join(staging_dir, normalized))
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            entry_written = 0
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(COPY_CHUNK)
                    if not chunk:
                        break
                    entry_written += len(chunk)
                    total_written += len(chunk)
                    if entry_written > max_entry_bytes:
                        raise ZipTooLarge(
                            f"entry {info.filename!r} is too large "
                            f"(> {max_entry_bytes} bytes uncompressed)"
                        )
                    if total_written > max_total_bytes:
                        raise ZipTooLarge(
                            f"zip uncompressed size too large (> {max_total_bytes} bytes)"
                        )
                    dst.write(chunk)
    except (ZipTooLarge, OSError, zipfile.BadZipFile):
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return total_written


def _candidates(parent: str, name: str, limit: int, preferred: str | None = None):
    """The folder names to try, in order: `preferred` (a name already shown to the user),
    then ``name``, ``name-2``, ``name-3``… Single-sourced so the *prediction* and the
    *claim* walk the same sequence and cannot disagree about which name is next."""
    if preferred and preferred != name:
        yield os.path.join(parent, preferred)
    yield os.path.join(parent, name)
    for n in range(2, limit + 1):
        yield os.path.join(parent, f"{name}-{n}")


def move_into_new_dir(
    src: str, parent: str, name: str, *, preferred: str | None = None, limit: int = 200
) -> str:
    """Rename ``src`` to an unused folder under ``parent`` and return where it landed.

    ``os.rename``, deliberately, not ``shutil.move``: `move()` onto an **existing
    directory** moves the source *inside* it. So a destination that appeared between an
    ``exists()`` check and the move — a concurrent clone, or anything else writing to the
    workspace — would silently nest the payload one level deeper and still report success,
    leaving the page somewhere other than where the caller says it is. `rename` cannot nest:
    it fails (`ENOTEMPTY` on POSIX, `EEXIST`/`FileExistsError` on Windows), and this retries
    the next name instead of guessing.

    The one case POSIX `rename` *does* replace is an existing **empty** directory. That is
    accepted: an empty folder holds nothing to lose, and no clone has written into one yet.
    """
    last: OSError | None = None
    for candidate in _candidates(parent, name, limit, preferred):
        if os.path.exists(candidate):
            continue  # cheap skip; the rename below is what actually decides
        try:
            os.rename(src, candidate)
        except OSError as exc:
            # The destination was taken between the check and the rename (or is a file, or a
            # non-empty dir). Try the next name rather than nesting or overwriting.
            last = exc
            continue
        return candidate
    raise ZipRejected(
        f"could not find an unused folder name for {name!r} in {parent} "
        f"(tried {limit} variants{f'; last error: {last}' if last else ''}); "
        "rename or move the existing folders"
    )


def unique_dir(parent: str, name: str, *, limit: int = 200) -> str:
    """A path under ``parent`` that does not exist yet: ``name``, then ``name-2``, ``name-3``…

    A **prediction**, not a reservation — it creates nothing, so a caller that then writes
    there must handle the name having been taken in between (:func:`move_into_new_dir` does).

    Never overwrites and never merges into an existing directory — a clone landing on top
    of unrelated files is indistinguishable from data loss, and merging would leave a
    half-this half-that folder that neither runs nor re-exports.
    """
    for candidate in _candidates(parent, name, limit):
        if not os.path.exists(candidate):
            return candidate
    raise ZipRejected(
        f"could not find an unused folder name for {name!r} in {parent} "
        f"(tried {limit} variants); rename or move the existing folders"
    )
