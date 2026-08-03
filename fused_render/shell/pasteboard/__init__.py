"""Platform-agnostic OS clipboard (pasteboard) bridge for file references.

A plain webview can't read or write the *native file flavors* the system file
managers use — `clipboardData.files` would upload every byte and drops folders
entirely. The Python backend runs on the user's own machine (the server always
binds 127.0.0.1, D2/D3), so it can be the bridge the webview lacks: it hands
the frontend real absolute paths, and the existing server-side copy does the
work — instant at any file size, folders included.

The public surface is deliberately tiny and carries no platform vocabulary:

    read_files()        -> (paths, token, supported)
    write_files(paths)  -> (token, supported)
    fingerprint(paths)  -> str

`token` is a content fingerprint of the ordered path list, *not* a native
change counter: macOS has `changeCount` and Windows has
`GetClipboardSequenceNumber`, but Linux has no analog, so hashing the paths is
the only thing all three platforms can agree on — and it is testable with no
OS involved. The frontend tracks it as "last seen" so an untouched clipboard
never clobbers a pending in-app cut.

Cut is deliberately not modelled. No platform exposes a reliable cut-vs-copy
flag on read, so honouring one would mean deleting source files on a guess.

Backends live in sibling private modules and are dispatched on `sys.platform`,
mirroring `supervisor/_backend.py`. Unlike that module, a missing backend (or
a backend whose own imports fail — no pyobjc, no xclip) is *not* fatal: it
degrades to `supported=False` and the app keeps its existing in-app-only
clipboard behaviour. Each backend module provides exactly two functions:

    read_files() -> list[str]      # may raise; may return []
    write_files(paths: list[str])  # may raise
"""
from __future__ import annotations

import hashlib
import os
import sys
from types import ModuleType

# Sentinel distinct from None, which is the legitimate "no backend on this
# platform" answer we want to cache rather than re-probe on every focus event.
_UNPROBED = object()
_backend: object = _UNPROBED


def _load_backend() -> ModuleType | None:
    """Import the backend for this platform, or None if there isn't one.

    Cached: the import is attempted once per process. A backend whose own
    imports fail (pyobjc absent on a source install, say) counts as "no
    backend" — the caller reports unsupported and nothing else changes.
    Tests monkeypatch this whole function, which is why the dispatch and the
    caching live here rather than at module scope.
    """
    global _backend
    if _backend is not _UNPROBED:
        return _backend  # type: ignore[return-value]

    mod: ModuleType | None = None
    try:
        if sys.platform == "darwin":
            from fused_render.shell.pasteboard import _darwin as mod  # noqa: F401
        elif sys.platform == "win32":
            from fused_render.shell.pasteboard import _win32 as mod  # noqa: F401
        elif sys.platform.startswith("linux"):
            from fused_render.shell.pasteboard import _linux as mod  # noqa: F401
    except Exception:
        mod = None

    _backend = mod
    return mod


def fingerprint(paths: list[str]) -> str:
    """A stable, order-sensitive token for a list of paths.

    Empty list -> empty token, so "nothing on the clipboard" is never confused
    with "some state we last saw". The NUL join can't be produced by any real
    path, so no two distinct lists can collide by concatenation.
    """
    if not paths:
        return ""
    joined = "\0".join(paths).encode("utf-8", "surrogatepass")
    return hashlib.sha256(joined).hexdigest()[:32]


def _validate(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        if not isinstance(p, str) or not p:
            raise ValueError("clipboard paths must be non-empty strings")
        # Absolute-only, same rule the fs mutation routes enforce: a relative
        # path would resolve against the *server's* cwd, which is meaningless
        # to both the file manager and the user.
        if not _is_abs(p):
            raise ValueError(f"clipboard paths must be absolute: {p!r}")
        out.append(p)
    return out


def _is_abs(p: str) -> bool:
    # Not os.path.isabs: the backends deal in the *other* platforms' paths only
    # in their own tests, but a POSIX server should still call "/x" absolute
    # and a Windows one "C:/x" — os.path.isabs on the running platform is
    # exactly that rule, plus we accept a leading slash everywhere so a
    # UNC/posix path from a backend is never silently dropped on Windows.
    return p.startswith("/") or p.startswith("\\") or os.path.isabs(p)


def read_files() -> tuple[list[str], str, bool]:
    """Absolute paths currently on the OS clipboard, plus their token.

    Returns `([], "", False)` when there is no working bridge on this machine
    — a missing backend, missing pyobjc, an absent xclip, or a hardened
    sandbox all look the same to the caller, which is the point.
    """
    backend = _load_backend()
    if backend is None:
        return [], "", False
    try:
        raw = backend.read_files()
    except Exception:
        # A clipboard read is never important enough to fail a request: a
        # transient "another app owns the clipboard" error on Windows or a
        # dead wl-paste both just mean "we don't know", i.e. unsupported.
        return [], "", False
    paths = [p for p in (raw or []) if isinstance(p, str) and p and _is_abs(p)]
    return paths, fingerprint(paths), True


def write_files(paths: list[str]) -> tuple[str, bool]:
    """Publish `paths` on the OS clipboard as file references.

    Raises ValueError for a caller error (relative or non-string path) —
    that's a bug worth surfacing. A *platform* failure is not: it comes back
    as `("", False)` exactly like an absent backend.
    """
    checked = _validate(paths)
    backend = _load_backend()
    if backend is None:
        return "", False
    if not checked:
        # Clearing the OS clipboard is not something an in-app copy should
        # ever do — leave whatever is there alone.
        return "", True
    try:
        backend.write_files(checked)
    except Exception:
        return "", False
    return fingerprint(checked), True
