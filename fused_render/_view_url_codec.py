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
import re
from pathlib import PureWindowsPath
from urllib.parse import quote, unquote, urlsplit


def _is_drive_path(fs_path: str) -> bool:
    # A drive-letter path has drive 'C:' + a non-empty root; a UNC path's
    # drive is '\\\\server\\share' (no trailing ':'); a POSIX path's is ''.
    # 'C:foo' (drive-relative, no root) is excluded.
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


# A launch argument is a URL (not a filesystem path) when it is a
# `fused-render:` deep link, a `file:` URI, or any `<scheme>://…`. A Windows
# drive path ('C:\\…') is deliberately NOT a URL: it has no '://' and neither
# the fused-render nor file scheme, so it round-trips through view_url_path.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def is_launch_url(raw: str) -> bool:
    """True when a raw launch argument is an OS-delivered URL rather than a
    filesystem path — the cue to skip existence checks and cwd resolution."""
    low = raw.lower()
    if low.startswith(("fused-render:", "file:")):
        return True
    return bool(_SCHEME_RE.match(raw))


def open_target_path(raw: str) -> str:
    """Shell URL path for a raw launch argument, shared by every platform's
    entry point (macOS app.py, the Windows/Linux supervisor).

    - a `fused-render:` deep link (case-insensitive) -> the `/clone` confirm
      page with the raw link ferried verbatim as ?src= (deeplink.py parses and
      validates it server-side);
    - a LOCAL `file:` URI (empty authority or `localhost`, RFC 8089) ->
      decoded to its filesystem path, then `view_url_path`;
    - a `file:` URI naming a REMOTE host, or any other `scheme://` URL ->
      raises OSError. There is no local filesystem path behind either, and
      decoding/falling through used to open a garbage /view page; raising an
      OSError makes `_safe_open` answer status 1 and the caller show the
      "FusedRender could not open" dialog, exactly like a missing file;
    - anything else (a plain absolute path, a folder) -> `view_url_path`.
    """
    if raw.lower().startswith("fused-render:"):
        return "/clone?src=" + quote(raw, safe="")
    if raw.lower().startswith("file:"):
        split = urlsplit(raw)
        if split.netloc.lower() not in ("", "localhost"):
            raise OSError(f"cannot open file URL on a remote host: {raw}")
        return view_url_path(unquote(split.path))
    if _SCHEME_RE.match(raw):
        raise OSError(f"cannot open URL (no local file behind this scheme): {raw}")
    return view_url_path(raw)


def open_target_url(port: int, raw: str) -> str:
    """Full local URL form of `open_target_path` (host/port prefixed)."""
    return f"http://127.0.0.1:{port}" + open_target_path(raw)
