"""Fetch a ``.fused`` app file from a public URL into the managed downloads
dir (SPEC §26 DL-8).

``download_app_file(url)`` streams ``http(s)://…`` into
``<storage.home_dir()>/downloads/`` (``~/.fused-render/downloads``, nested
under the branch dir like every other piece of shell state) and validates it
with ``appfile.read_manifest`` before answering the path. The saved name is
keyed on the app's STABLE IDENTITY (``appfile.app_id_of``, the
``<meta name="fused-app-id">`` tag, D884): ``<app_id>.fused``. Two URLs
serving the same app (a mirror, a moved file, a new version) land on one
path, and the install page (``appfile.install_target``) decides whether
the file is new, an update or a downgrade of the installed copy. A file
exported before the id existed falls back to
``<name>-<url hash>.fused`` — stable per URL, the best identity such a file
offers.

Only ``http`` and ``https`` are accepted, and every redirect hop is checked
again, so a redirect to ``file://`` (or anything else urllib knows how to
open) is refused. Size is capped both by ``Content-Length`` and while
streaming — servers lie.

``download_github_app(url)`` is the ``fused-render://open?git=`` source
(DL-9): a public ``https://github.com/<owner>/<repo>`` link — that shape
only, the repo holding ONE app at its ROOT. It git-clones the repo into a
staging dir (appfile_git.py, which also owns the install: the clone itself
moves into ``app/<slug>``) and packs the root app with
``appfile.export_app_file`` into the same downloads dir, carrying ``git:
{url, commit}`` — so the caller gets a ``.fused`` path and the install page
is the step after, exactly as for a hosted file. An entry with no
``fused-app-id`` gets one derived from the repo
(``<name>-<sha256(github.com/owner/repo/)[:8]>``) in the MANIFEST only, and
``exported_at`` is the commit time. The ``fused-render open <github url>``
CLI rides the same function.

Ported from Render App's ``fetch.py`` (fused-render-lite PRs #27/#30) so a
``fused-render://open?url=`` link downloads like ``render-app://open?url=``
there: no confirm step before the download, the link click is the gesture.
The install page is the step before anything runs (AF-14).
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

from fused_render import __version__, app_listing, appfile

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


def _open(url: str):
    """Open ``url`` through the scheme-guarded opener; ``FetchError`` on any
    transport failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    try:
        return _opener.open(req, timeout=TIMEOUT_S)
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


def _stream(resp, out_path: str) -> None:
    """Copy ``resp``'s body into ``out_path`` under the 1 GB cap (declared
    AND counted — servers lie). An empty body is an error."""
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        raise FetchError("the file is larger than the 1 GB cap")
    total = 0
    try:
        with open(out_path, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise FetchError("the file is larger than the 1 GB cap")
                out.write(chunk)
    except (http.client.HTTPException, OSError) as exc:  # IncompleteRead is not an OSError
        raise FetchError(f"download failed: {exc}")
    if total == 0:
        raise FetchError("the URL answered an empty body")


#: ``https://github.com/<owner>/<repo>`` — optional ``www.``, ``.git`` and
#: trailing slash; nothing after the repo. Conservative owner/repo shapes so
#: nothing can smuggle path tricks into the archive URL.
_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.I,
)


def parse_github_repo(url: str) -> tuple[str, str]:
    """``(owner, repo)`` of a ``https://github.com/<owner>/<repo>`` link;
    ``FetchError`` for any other shape. A query or fragment (a link pasted
    from the address bar: ``?tab=readme-ov-file``, ``#readme``) is dropped
    first; the path alone decides."""
    raw = (url or "").strip()
    m = _GITHUB_REPO_RE.match(re.split(r"[?#]", raw, maxsplit=1)[0])
    if not m or m.group("repo") in (".", ".."):
        raise FetchError("only https://github.com/<owner>/<repo> links can be "
                         f"installed: {url or '(empty)'}")
    return m.group("owner"), m.group("repo")


def download_app_file(url: str) -> str:
    """Download ``url`` and answer the absolute path of the saved ``.fused``.
    Raises ``FetchError`` for a bad URL, transfer failure, oversize body or
    a body that is not a fused app file (nothing is left on disk then)."""
    url = (url or "").strip()
    if not is_url(url):
        raise FetchError("only http:// and https:// URLs can be opened")
    resp = _open(url)
    with resp:
        dest_dir = downloads_dir()
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".download-")
        os.close(fd)
        try:
            _stream(resp, tmp)
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
        except OSError as exc:
            raise FetchError(f"download failed: {exc}")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return dest


def download_github_app(url: str) -> str:
    """Clone the public GitHub repo at ``url`` (``https://github.com/<owner>/
    <repo>``) into its staging dir (appfile_git.clone) and pack the app at
    its ROOT into ``downloads/<app_id>.fused`` for the install page; answer
    that path. Installing the file then moves the clone itself into
    ``app/<slug>`` (appfile_git.install)."""
    from fused_render import appfile_git

    owner, repo = parse_github_repo(url)
    where = f"github.com/{owner}/{repo}"
    canonical = f"https://{where}"
    appfile_git.sweep_staging()
    try:
        clone_dir, commit, committed_at = appfile_git.clone(canonical, repo)
    except appfile_git.GitInstallError as exc:
        raise FetchError(str(exc))
    try:
        entry = app_listing.app_entry(clone_dir)
    except OSError:
        entry = None
    if entry is None:
        raise FetchError(f'no page at the root of {where} carries <meta name="fused-app">; '
                         "only repositories with one app at their root can be installed")
    # The page's own id when it has one; else one derived from the repo, kept
    # in the manifest and the install stamp only — the clone stays clean. The
    # install re-derives it from the clone and refuses a mismatch.
    derived = appfile_git.expected_identity(clone_dir, canonical)[1]
    dest_dir = downloads_dir()
    fd, built = tempfile.mkstemp(dir=dest_dir, prefix=".github-", suffix=".fused")
    os.close(fd)
    os.remove(built)
    try:
        try:
            manifest = appfile.export_app_file(
                clone_dir, built, exported_at=committed_at, app_id=derived,
                extra={"git": {"url": canonical, "commit": commit}})
        except appfile.AppFileError as exc:
            raise FetchError(str(exc))
        app_id = appfile.app_id_of(manifest)
        dest = os.path.join(dest_dir, f"{app_id}.fused" if app_id
                            else _name_from(f"{canonical}/{repo}.fused", None))
        os.replace(built, dest)
    except OSError as exc:
        raise FetchError(f"could not build the app from {where}: {exc}")
    finally:
        if os.path.exists(built):
            os.remove(built)
    return dest
