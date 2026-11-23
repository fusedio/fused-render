"""The named files of a local SQL engine (SPEC §55.3).

Every file the engine's SQL can reach lives flat in the engine's `files/`
directory, under a name `NAME_RE` accepts. A file gets there three ways:

  - bytes sent to the engine: streamed to a temporary file in `io/` by the
    route, then `os.replace`d onto `files/<name>` (`register_bytes`);
  - a link to a saved result: a hard link to the result's body, so no bytes
    are copied and the name keeps the bytes it was given for as long as the
    engine lives, even after the store replaces or expires its entry
    (`register_link`);
  - SQL writing it (`COPY ... TO '<name>'`).

An existing `files/<name>` is never opened for writing here: it may be a
hard link to a store entry, so it is only ever replaced or removed. On POSIX
a link is made read-only (0444) for the same reason, so nothing writes the
store's bytes in place through it.

Plain file operations; no duckdb.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import stat

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HANDLE_RE = re.compile(r"^(udf|idx):([0-9a-z]{1,128})$")


class BadName(ValueError):
    """Not a valid file name or handle."""


class NotFound(LookupError):
    """No such file, or no such saved result."""


class Unsupported(Exception):
    """A handle kind that cannot be linked."""


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or name in (".", ".."):
        raise BadName("a file name is 1-128 of A-Z a-z 0-9 . _ - (not . or ..)")
    return name


def _target(files_dir: str, name: str) -> str:
    return os.path.join(files_dir, check_name(name))


def temp_path(io_dir: str) -> str:
    """A fresh path in `io/` for a body on its way in."""
    return os.path.join(io_dir, f"in-{secrets.token_hex(12)}.part")


def register_bytes(files_dir: str, name: str, src: str) -> None:
    """Move the file at `src` (in `io/`, same volume) onto `files/<name>`."""
    os.replace(src, _target(files_dir, name))


def register_link(files_dir: str, io_dir: str, name: str, handle: str) -> None:
    """Make `files/<name>` the body of the saved result `handle` names."""
    target = _target(files_dir, name)
    m = HANDLE_RE.match(handle) if isinstance(handle, str) else None
    if m is None:
        raise BadName("a handle is udf:<token> or idx:<token>")
    kind, token = m.group(1), m.group(2)
    if kind != "udf":
        raise Unsupported(f"{kind} handles cannot be linked yet")
    from fused_render.udf import store

    found = store.open_result(token)
    if found is None:
        raise NotFound("no such result (it may have expired)")
    body = found[0]
    tmp = temp_path(io_dir)
    try:
        try:
            os.link(body, tmp)
        except FileNotFoundError:
            raise NotFound("no such result (it may have expired)") from None
        except OSError as exc:
            # Another volume, or a filesystem without hard links: a copy.
            logger.debug("sql files: linking %s failed (%s); copying it", handle, exc)
            try:
                shutil.copyfile(body, tmp)
            except FileNotFoundError:
                raise NotFound("no such result (it may have expired)") from None
        if os.name != "nt":
            # Windows cannot remove a read-only file, and the store must
            # stay able to remove its entry, so this is POSIX only.
            os.chmod(tmp, 0o444)
        os.replace(tmp, target)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_path(files_dir: str, name: str) -> str:
    """The path of `files/<name>`, a regular file, or NotFound."""
    path = _target(files_dir, name)
    try:
        st = os.lstat(path)
    except OSError:
        raise NotFound(f"no file named {name}") from None
    if not stat.S_ISREG(st.st_mode):
        raise NotFound(f"no file named {name}")
    return path


def drop(files_dir: str, name: str) -> None:
    path = _target(files_dir, name)
    try:
        st = os.lstat(path)
    except OSError:
        raise NotFound(f"no file named {name}") from None
    if stat.S_ISDIR(st.st_mode):
        # `COPY ... (PARTITION_BY ...)` writes a directory.
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        raise NotFound(f"no file named {name}") from None
