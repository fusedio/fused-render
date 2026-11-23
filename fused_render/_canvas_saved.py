"""The latest saved canvas the sync's manifest probe was given by the Fused
service, kept on disk as the service's JSON text (SPEC §53.3 NW-8).

The text is opaque here: it is stored, served and relayed, never parsed. It
lives next to the sync state, outside the clone (`<root>/.sync/<name>.canvas.json`),
so a pull never overwrites it and no file of the clone holds it. It can hold
parameters and tokens, so the file is private to the user (0600 on POSIX).

Stdlib only: the manifest shim imports this next to the fused SDK.
"""

import os

# The environment variable through which the host tells the manifest shim
# where to write the document. Unset: the shim writes nothing.
OUT_ENV = "FUSED_RENDER_CANVAS_DOC_OUT"


def path_for(root: str, name: str) -> str:
    return os.path.join(root, ".sync", f"{name}.canvas.json")


def write(path: str, text: str) -> None:
    """Replace `path` with `text` atomically (temp file, then `os.replace`),
    creating the file private to the user. Raises OSError on failure; the
    caller decides what a failure means."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def age_s(path: str) -> float:
    """Seconds since `path` was last written (infinite when unreadable)."""
    import time

    try:
        return max(0.0, time.time() - os.stat(path).st_mtime)
    except OSError:
        return float("inf")
