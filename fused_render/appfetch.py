"""Fetch a ``.fused`` app file from a public URL into the managed downloads
dir (SPEC §26 DL-8).

``download_app_file(url)`` streams ``http(s)://…`` into
``<storage.home_dir()>/downloads/`` (``~/.fused-render/downloads``, nested
under the branch dir like every other piece of shell state) and validates it
with ``appfile.read_manifest`` before answering the path. The saved name is
keyed on the app's STABLE IDENTITY (``appfile.app_id_of``, the
``<meta name="fused-app-id">`` tag, D884): ``<app_id>.fused``. Two URLs
serving the same app (a mirror, a moved file, a new version) land on one
path, so the Apps hub keeps one row and the content-addressed extract cache
(``appfile.open_app_file``) decides whether the extract is re-used or
rebuilt. A file exported before the id existed falls back to
``<name>-<url hash>.fused`` — stable per URL, the best identity such a file
offers.

Only ``http`` and ``https`` are accepted, and every redirect hop is checked
again, so a redirect to ``file://`` (or anything else urllib knows how to
open) is refused. Size is capped both by ``Content-Length`` and while
streaming — servers lie.

Ported from Render App's ``fetch.py`` (fused-render-lite PRs #27/#30) so a
``fused-render://open?url=`` link behaves exactly like ``render-app://open?url=``
there: no confirm step, the link click is the gesture.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from fused_render import __version__, appfile

MAX_BYTES = 1024 * 1024 * 1024
TIMEOUT_S = 60
_USER_AGENT = f"fused-render/{__version__}"


class FetchError(Exception):
    """User-correctable failure; the message goes verbatim into the 400 body."""


class _SchemeGuard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise FetchError(f"redirect to a non-http(s) URL refused: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SchemeGuard())


def is_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value or "", re.I))


def downloads_dir() -> str:
    """Where fetched ``.fused`` files land: one stable path per app id, so
    re-opening the same link updates in place."""
    from fused_render.shell import storage

    path = os.path.join(storage.home_dir(), "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def _name_from(url: str, headers) -> str:
    """Fallback name for a file with no app id: ``<basename>-<8 hex of the
    URL>.fused``, basename sanitised to a safe file name. Content-Disposition
    wins over the URL path when the server sends one."""
    raw = ""
    cd = headers.get("Content-Disposition") if headers is not None else None
    if cd:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
        if m:
            raw = urllib.parse.unquote(m.group(1))
    if not raw:
        raw = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", os.path.basename(raw)).strip()
    stem = re.sub(r"\.fused$", "", stem, flags=re.I).strip(" .") or "app"
    tag = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{tag}.fused"


def download_app_file(url: str) -> str:
    """Download ``url`` and answer the absolute path of the saved ``.fused``.
    Raises ``FetchError`` for a bad URL, transfer failure, oversize body or
    a body that is not a fused app file (nothing is left on disk then)."""
    url = (url or "").strip()
    if not is_url(url):
        raise FetchError("only http:// and https:// URLs can be opened")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    try:
        resp = _opener.open(req, timeout=TIMEOUT_S)
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            # urllib surfaces a redirect it refused to follow (a non-http(s)
            # Location, or too many hops) as the bare 3xx.
            raise FetchError(f"redirect refused: non-http(s) target or too many hops "
                             f"(HTTP {exc.code} from {url})")
        raise FetchError(f"download failed: HTTP {exc.code} for {url}")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise FetchError(f"download failed: {getattr(exc, 'reason', exc)}")
    with resp:
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
            raise FetchError("the file is larger than the 1 GB cap")
        dest_dir = downloads_dir()
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".download-")
        total = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise FetchError("the file is larger than the 1 GB cap")
                    out.write(chunk)
            if total == 0:
                raise FetchError("the URL answered an empty body")
            try:
                manifest = appfile.read_manifest(tmp)
            except appfile.AppFileError as exc:
                raise FetchError(str(exc))
            # Keyed on the app's identity when the file declares one (the
            # id is validated by app_identity, so it is a safe file name);
            # else on the URL.
            app_id = appfile.app_id_of(manifest)
            name = f"{app_id}.fused" if app_id else _name_from(url, resp.headers)
            dest = os.path.join(dest_dir, name)
            os.replace(tmp, dest)
        except (http.client.HTTPException, OSError) as exc:  # IncompleteRead is not an OSError
            raise FetchError(f"download failed: {exc}")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return dest
