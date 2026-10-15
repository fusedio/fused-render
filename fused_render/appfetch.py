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

A public GitHub REPOSITORY link (``https://github.com/<owner>/<repo>``, or
``…/tree/<ref>[/<subdir>]`` — ``deeplink.parse_github_url``'s shapes) is the
other accepted source: ``download_github_app`` fetches the archive at that
ref (``HEAD`` when none), extracts it through the hardened
``zip_import.extract_to_staging``, finds THE one folder whose page carries
``<meta name="fused-app">`` (the subdir when the link names one; zero or
several is an error, never a guess) and exports it with
``appfile.export_app_file`` into the same downloads dir — so the caller gets
a ``.fused`` path and the install page is the step after, exactly as for a
hosted file. Two stamps keep a re-click an UPDATE and not a new app:
  * an entry with no ``fused-app-id`` is given one derived from the repo
    (``<name>-<sha256(github.com/owner/repo/subdir)[:8]>``), stable across
    clicks, written into the extracted page before export;
  * ``exported_at`` is the commit's time (the archive's newest member mtime,
    which GitHub sets from the commit), so the same commit reads as SAME and
    a newer one as UPDATE on the install page.
Any other github.com URL (a release asset, ``/raw/``) is not a repo link and
takes the plain download path. This is not ``fused-render://open?git=``
(deeplink.py): that one clones an editable tree into the workspace; this one
installs the app under ``app/<slug>`` like any ``.fused``.

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
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from fused_render import __version__, app_listing, appfile, zip_import
from fused_render import app_id as app_identity

MAX_BYTES = 1024 * 1024 * 1024
TIMEOUT_S = 60
#: Member cap for a GitHub archive: the whole repo is extracted, not just the
#: app folder, so this sits above appfile.MAX_EXPORT_ENTRIES.
MAX_ARCHIVE_ENTRIES = 20000
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


def github_repo_spec(url: str) -> dict | None:
    """``deeplink.parse_github_url``'s spec when ``url`` is a github.com
    repository or ``/tree/`` link, else None — a release asset or ``/raw/``
    link to a ``.fused`` is a plain download, not a repo."""
    from fused_render.deeplink import DeeplinkError, parse_github_url

    try:
        return parse_github_url(url)
    except DeeplinkError:
        return None


def download_app_file(url: str) -> str:
    """Download ``url`` and answer the absolute path of the saved ``.fused``.
    Raises ``FetchError`` for a bad URL, transfer failure, oversize body or
    a body that is not a fused app file (nothing is left on disk then). A
    GitHub repository link is built into a ``.fused`` first
    (``download_github_app``)."""
    url = (url or "").strip()
    if not is_url(url):
        raise FetchError("only http:// and https:// URLs can be opened")
    spec = github_repo_spec(url)
    if spec is not None:
        return download_github_app(spec)
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


def _find_app_dir(root: str, where: str) -> str:
    """THE folder under ``root`` (itself included) whose page carries the
    fused-app marker (``app_listing.app_entry``). A found app is not
    descended into; hidden and machinery dirs are skipped. Zero or several
    candidates is a ``FetchError`` — the link must name one app."""
    found: list[str] = []
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        try:
            entry = app_listing.app_entry(dirpath)
        except OSError:
            entry = None
        if entry is not None:
            found.append(dirpath)
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in appfile._SKIP_DIRS)
    if not found:
        raise FetchError(f'no page in {where} carries <meta name="fused-app">, '
                         "so there is no app to install")
    if len(found) > 1:
        rels = ", ".join(os.path.relpath(d, root).replace(os.sep, "/") for d in found[:5])
        raise FetchError(f"{where} holds more than one app ({rels}); link to one with "
                         "https://github.com/<owner>/<repo>/tree/<ref>/<folder>")
    return found[0]


def _commit_time(zf: zipfile.ZipFile) -> str | None:
    """The archive's newest member mtime as an ``exported_at`` stamp —
    GitHub writes every member with the commit's time."""
    stamps = [i.date_time for i in zf.infolist() if i.date_time[0] >= 1980]
    if not stamps:
        return None
    y, mo, d, h, mi, s = max(stamps)
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}Z"


def download_github_app(spec: dict) -> str:
    """Build the one app in a public GitHub repo (``spec`` from
    ``github_repo_spec``) into ``downloads/<app_id>.fused`` and answer its
    path. See the module docstring for the identity and time stamps."""
    owner, repo, sub = spec["owner"], spec["repo"], spec["subpath"]
    where = f"github.com/{owner}/{repo}" + (f"/{sub}" if sub else "")
    archive = (f"https://github.com/{owner}/{repo}/archive/"
               f"{urllib.parse.quote(spec['ref'] or 'HEAD', safe='')}.zip")
    dest_dir = downloads_dir()
    work = tempfile.mkdtemp(dir=dest_dir, prefix=".github-")
    try:
        zip_path = os.path.join(work, "repo.zip")
        try:
            resp = _open(archive)
        except FetchError as exc:
            if "HTTP 404" in str(exc):
                raise FetchError(f"GitHub has no public repository or ref at {where} "
                                 "(private repositories cannot be installed from a link)")
            raise
        with resp:
            _stream(resp, zip_path)
        extracted = os.path.join(work, "x")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                exported_at = _commit_time(zf)
                zip_import.extract_to_staging(
                    zf, extracted, max_entries=MAX_ARCHIVE_ENTRIES,
                    max_entry_bytes=MAX_BYTES, max_total_bytes=MAX_BYTES)
        except zipfile.BadZipFile:
            raise FetchError(f"GitHub did not answer a zip archive for {where}")
        except (zip_import.ZipRejected, zip_import.ZipTooLarge) as exc:
            raise FetchError(f"cannot unpack {where}: {exc}")
        # GitHub wraps the tree in one `<repo>-<sha>/` folder.
        tops = [n for n in os.listdir(extracted) if not n.startswith(".")]
        if len(tops) != 1 or not os.path.isdir(os.path.join(extracted, tops[0])):
            raise FetchError(f"unexpected archive layout from GitHub for {where}")
        top = os.path.join(extracted, tops[0])
        root = os.path.join(top, *sub.split("/")) if sub else top
        if not os.path.isdir(root):
            raise FetchError(f"no folder {sub!r} in github.com/{owner}/{repo}")
        found = _find_app_dir(root, where)
        # The folder name is the app's name (manifest `name`, hence its slug
        # under app/): the repo's own name for a root-level app — never the
        # archive's `<repo>-<sha>` wrapper.
        name = repo if found == top else os.path.basename(found)
        app_dir = os.path.join(work, "app", name)
        os.makedirs(os.path.dirname(app_dir))
        os.rename(found, app_dir)
        entry = app_listing.app_entry(app_dir)
        if entry is not None and app_identity.app_id(entry) is None:
            # Stable per repo (+ subdir), so export_app_file never mints a
            # random one and a re-click finds the installed copy.
            key = f"github.com/{owner.lower()}/{repo.lower()}/{sub}".encode("utf-8")
            app_identity.stamp_entry(
                entry, f"{app_identity.kebab(name)}-{hashlib.sha256(key).hexdigest()[:8]}")
        built = os.path.join(work, "app.fused")
        try:
            manifest = appfile.export_app_file(app_dir, built, exported_at=exported_at)
        except appfile.AppFileError as exc:
            raise FetchError(str(exc))
        app_id = appfile.app_id_of(manifest)
        canonical = f"https://github.com/{owner}/{repo}" + (f"/{sub}" if sub else "")
        dest = os.path.join(dest_dir, f"{app_id}.fused" if app_id
                            else _name_from(f"{canonical}/{name}.fused", None))
        os.replace(built, dest)
    except OSError as exc:
        raise FetchError(f"could not build the app from {where}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dest
