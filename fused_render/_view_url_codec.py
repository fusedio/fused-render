"""Shared fs-path -> /view URL codec for the Windows entry points.

Mirrors the frontend codec (frontend/src/lib/router.ts urlForFsPath): only a
drive-letter path gets its backslashes normalized to '/' before segmenting —
a UNC path stays one percent-encoded segment, and on POSIX a backslash is a
legal filename character that must round-trip untouched. A `.bookmark` file
is not previewed directly (SB-9, D99): it routes through the `_bookmark`
sentinel, which reads it server-side and redirects to the view it describes.

Pure path classification only (no filesystem/OS calls), so this module and
its tests run identically on Windows, macOS, and Linux.
"""
from pathlib import PureWindowsPath
from urllib.parse import quote


def _is_drive_path(fs_path: str) -> bool:
    # PureWindowsPath does the shape reasoning that a hand-rolled
    # ^[A-Za-z]:[\\/] regex was doing before: a drive-letter path's drive is
    # 'C:' plus a non-empty root; a UNC path's drive is '\\\\server\\share'
    # (doesn't end in ':'); a POSIX path's drive is ''. 'C:foo' (drive-
    # relative, no root) is excluded, matching the old regex's behavior.
    p = PureWindowsPath(fs_path)
    return p.drive.endswith(":") and bool(p.root)


def view_url_path(fs_path: str) -> str:
    """/view URL path (no host/port) for an absolute fs path."""
    norm = fs_path.replace("\\", "/") if _is_drive_path(fs_path) else fs_path
    if fs_path.lower().endswith(".bookmark"):
        return "/view/_bookmark?file=" + quote(norm, safe="")
    segments = [quote(seg, safe="!*'()") for seg in norm.lstrip("/").split("/") if seg]
    return "/view/" + "/".join(segments)


def view_url(port: int, fs_path: str | None) -> str:
    """Full local URL for an absolute fs path; home page when fs_path is None."""
    if not fs_path:
        return f"http://127.0.0.1:{port}/"
    return f"http://127.0.0.1:{port}" + view_url_path(fs_path)
