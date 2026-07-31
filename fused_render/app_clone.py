"""Clone a **deployed** fused-render page back to this machine (`023` §8.3, viewer half).

The publisher's half is `deploy.py` (the Deploy dialog's "let viewers clone this app"
toggle → `fused share create --allow-clone`). This is the other end: given the URL of a
deployed page whose publisher enabled cloning, fetch its export bundle from the mount's
host-reserved `_clone` sub-path and unpack it into the user's Fused workspace as an
ordinary local page — `page.html` beside its `runPython` targets, its assets, and the
modules those entrypoints import, each at its real page-relative path. It opens, runs, and
re-deploys with no rewriting.

**Not the GitHub flow.** `deeplink.py` clones git repositories: it relies on `.git` for
identity, so it can tell "update this existing clone" from "that folder is something else",
and it never extracts an archive. This module has no such identity — an archive carries no
provenance we can verify — so it never updates in place. Every clone lands in a **fresh,
unused folder** (`zip_import.unique_dir`) and the two flows share nothing but the deep-link
transport and the shape of their confirm step. Keeping them separate is deliberate: bolting
archive extraction onto the git path would have given one code path two trust models.

**The archive is hostile until proven otherwise.** A public URL does not make its bytes
trustworthy: the user pasted the address, the server on the other end is not ours, and the
zip is attacker-controlled in the case that matters. So:

- **HTTPS only**, and the URL is rebuilt from parsed components rather than string-joined,
  so a query, fragment, or userinfo cannot ride along into the request we make.
- **No redirects followed.** A redirect is the standard way to turn an allowed URL into a
  request at an address the caller never approved (including a local one), and a clone
  endpoint has no legitimate reason to redirect.
- **Loopback, private, link-local, and cloud-metadata addresses are refused** — before the
  request, by resolving the host and checking every answer. This process runs on the user's
  machine with access to their LAN and to `169.254.169.254`-style endpoints; a pasted URL
  must not become a probe of either.
- **The download is streamed and counted**, capped on bytes actually received rather than
  on `Content-Length` (which the far end chooses freely).
- **The unpack goes through `zip_import`** — the same guards the template-pack import uses:
  entry validation before any write, symlink refusal, path-escape refusal, per-entry and
  total decompressed caps, staging-then-move.

MVP is **public mounts only**. An authed mount needs a token whose audience satisfies that
mount's gate, and neither a query-string parameter nor a deep link is an acceptable place
to carry one (both leak through history and logs), so this module never sends credentials
and a gated mount simply reports that it cannot be cloned from here.
"""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import zipfile
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import zip_import
from fused_render._view_url_codec import view_url_path as _view_url_path
from fused_render.shell.seed import fused_dir

logger = logging.getLogger("fused_render")

router = APIRouter()

#: The host-reserved sub-path a deployed page serves its own bundle at (the `fused` repo's
#: `serve_reserved.CLONE_SUB_PATH`). Not configurable: it is part of the serve contract.
CLONE_SUB_PATH = "_clone"

#: Sub-paths a pasted URL may legitimately end in — the shapes a page is actually served
#: at, mirroring the runtime's own base derivation. Anything else is a path we should not
#: silently strip, because guessing wrong would clone a different mount than the user meant.
_STRIPPABLE_TAIL = frozenset({"", "_shell", CLONE_SUB_PATH})

#: Ceiling on the archive we will download, on bytes ACTUALLY received. Above the serve
#: path's own 5 MiB wire cap so a bundle the server is willing to send is never refused
#: here — this is a runaway guard, not a policy.
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024

#: Unpack budgets. Deliberately not the template-pack numbers: a page bundle is small by
#: construction (its ceiling is the serve path's clone cap), so a tighter total costs
#: nothing legitimate.
MAX_ENTRIES = 2000
MAX_ENTRY_UNCOMPRESSED = 25 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024

#: Staged clones expire after 15 minutes, like a staged template import.
STAGING_TTL_SEC = 900

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

#: A safe folder name: what survives from an app name before it is used as a directory.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class CloneError(Exception):
    """A clone could not be performed, with a message fit to show the user."""


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    """Same guard the other mutating routers use: only our own shell may POST here."""
    if x_fused != "1":
        return _error("missing X-Fused header", 403)
    return None


# -- the URL contract ----------------------------------------------------------


def clone_url_from(src: str) -> str:
    """The canonical `…/<token>/_clone` URL for a pasted page address.

    Accepts the shapes a page is actually served at — `…/<token>`, `…/<token>/`, and
    `…/<token>/_shell` — plus an already-complete `…/_clone`, and rebuilds the URL from
    parsed components so nothing from the input rides along:

    - **query and fragment are dropped.** A pasted address may carry either; neither
      belongs in the request, and a query string is also where a credential would hide
      (MVP sends none, so silently forwarding one would be the worst of both).
    - **userinfo is refused**, not stripped. `https://user:pw@host/…` is either a
      credential the user did not mean to hand us or an attempt to make the host look like
      something it is not; stripping it quietly would clone from a different origin than
      the URL appears to name.

    Raises :class:`CloneError` with a specific reason — the caller shows it verbatim.
    """
    text = (src or "").strip()
    if not text:
        raise CloneError("paste the URL of a deployed page")
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        # http:// is refused rather than upgraded: a clone carries a page's source, and
        # silently "fixing" the scheme would hide that the user's link was insecure.
        raise CloneError("only https:// URLs can be cloned")
    if "@" in parts.netloc:
        raise CloneError("URLs with embedded credentials cannot be cloned")
    if not parts.hostname:
        raise CloneError(f"{text!r} has no host")
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[-1] in _STRIPPABLE_TAIL:
        segments = segments[:-1]
    if not segments:
        raise CloneError(
            "that URL names no deployed page — it should look like "
            "https://<host>/<link-name>"
        )
    path = "/" + "/".join([*segments, CLONE_SUB_PATH])
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _validated_address(host: str) -> str:
    """Resolve `host`, refuse it unless **every** answer is public, and return the address
    we will connect to.

    Returning the address is the whole point, and the reason this is not a bare assertion:
    validating a name and then handing the *name* to an HTTP client leaves a
    **DNS-rebinding** window — the client resolves again when it connects, so an attacker
    can answer with a public address for the check and a loopback, private, or
    metadata address for the connection. The caller dials this address and keeps the
    hostname only for the `Host` header and TLS (see :func:`_get`).

    Checked on the **resolved** addresses, never the name: a hostname under someone else's
    control can point anywhere, so a name-based allow/deny list proves nothing. Every
    answer must be public — a name resolving to both a public and a private address is
    refused outright rather than filtered, since "some answers are fine" is not a property
    we can hold a connection to.

    This is the guard that matters most in a desktop app: the process sits inside the
    user's network, so an unchecked URL is a request their browser could not make.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise CloneError(f"could not resolve {host!r}: {exc}") from None
    if not infos:
        raise CloneError(f"could not resolve {host!r}")
    chosen: str | None = None
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            raise CloneError(f"{host!r} resolved to an address we cannot check: {raw}") from None
        # `is_global` is the single check that covers loopback, private ranges, link-local
        # (including the 169.254.169.254 metadata endpoint), multicast, and reserved space
        # — enumerating those by hand is how one gets missed.
        if not addr.is_global:
            raise CloneError(
                f"{host!r} resolves to a non-public address ({raw}); "
                "only publicly hosted pages can be cloned"
            )
        if chosen is None:
            chosen = raw
    assert chosen is not None  # non-empty infos and every answer validated above
    return chosen


class _Fetched:
    """A completed, size-checked response body.

    A small value object rather than an ``httpx.Response``: the response is streamed inside
    a context manager, so the real object is closed by the time a caller sees it, and
    handing back a closed response invites a ``.content`` access that raises.
    """

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


def _get(url: str) -> _Fetched:
    """One hardened GET: address-checked, no redirects, bounded timeout, streamed size cap.

    ``follow_redirects=False`` is the load-bearing argument — every other guard here is
    applied to the URL we validated, and a followed redirect would apply none of them to
    wherever the request actually landed.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    address = _validated_address(host)
    # Dial the address we validated, not the name. httpx would otherwise resolve the name
    # again at connect time, and nothing guarantees it gets the answer we checked — the
    # DNS-rebinding window. The hostname still travels as `Host` (so the far end routes
    # correctly) and as `sni_hostname`, which is what the TLS layer uses for SNI *and* for
    # certificate verification — so pinning the address costs no authentication: a cert
    # valid for the hostname is still required.
    literal = f"[{address}]" if ":" in address else address
    netloc = literal if parts.port is None else f"{literal}:{parts.port}"
    connect_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    body = bytearray()
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            with client.stream(
                "GET",
                connect_url,
                headers={"accept": "*/*", "host": parts.netloc},
                extensions={"sni_hostname": host},
            ) as resp:
                if resp.is_redirect:
                    raise CloneError(
                        "the page's host redirected the request; a clone URL must be "
                        "served directly (check the link)"
                    )
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    # Counted on bytes ACTUALLY received: Content-Length is chosen by the
                    # far end, so capping on that caps nothing.
                    if len(body) > MAX_DOWNLOAD_BYTES:
                        raise CloneError(
                            f"the download exceeded "
                            f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB and was stopped"
                        )
                status = resp.status_code
    except httpx.HTTPError as exc:
        raise CloneError(f"could not reach the page: {exc}") from None
    fetched = _Fetched(status, bytes(body))
    _raise_for_clone_status(fetched.status_code, fetched.content)
    return fetched


def _raise_for_clone_status(status: int, content: bytes) -> None:
    """Turn the serve path's status codes into messages a viewer can act on.

    `404` is deliberately ambiguous at the source — the gate must not confirm whether a
    mount exists or whether cloning is enabled — so this says both possibilities rather
    than guessing one, which would send users chasing the wrong problem.
    """
    if status == 200:
        return
    if status == 404:
        raise CloneError(
            "this page does not offer a download. Either its publisher has not enabled "
            "cloning, or the URL is wrong."
        )
    if status in (401, 403):
        raise CloneError(
            "this page requires sign-in, and cloning a private page is not supported yet — "
            "ask its publisher for the source, or for a public link."
        )
    if status == 410:
        raise CloneError("that link has been taken down by its publisher.")
    if status == 413:
        detail = _detail_of(content) or "the page's bundle is too large to download"
        raise CloneError(detail)
    if status == 429:
        raise CloneError("the page's host is rate-limiting; try again shortly.")
    detail = _detail_of(content)
    suffix = f": {detail}" if detail else ""
    raise CloneError(f"the page's host returned HTTP {status}{suffix}")


def _detail_of(content: bytes) -> str | None:
    """A server-supplied error string, if the body carries one in either shape the two
    serve planes use (a JSON `error` field, or plain text)."""
    text = content[:2048].decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        detail = data.get("error") if isinstance(data, dict) else None
        return detail if isinstance(detail, str) and detail else None
    return text


# -- preview -------------------------------------------------------------------


def _safe_folder_name(name: str | None) -> str:
    """A directory name derived from the clone's own `name`.

    The name comes from a server we do not control and is about to become a path, so it is
    reduced to a conservative allow-list rather than trusted — a name of `..`, an absolute
    path, or one carrying separators must not be able to steer where the clone lands.
    """
    cleaned = _UNSAFE_NAME.sub("-", (name or "").strip()).strip("-.")
    return cleaned or "cloned-page"


def probe(src: str) -> dict:
    """What cloning `src` would fetch and where it would land — no writes.

    Mirrors the git flow's `/api/clone/info`: the confirm step must describe exactly what
    the commit step will do, so the destination reported here is the one the clone uses —
    the client passes `folder` back to :func:`clone`, which honours it while it is free
    (CL-1). Reserving it here would mean creating a directory during a preview the user has
    not confirmed, so the guarantee is carry-through rather than a lock: if the name is
    taken between the two calls, the clone's own response names where it landed.
    """
    url = clone_url_from(src)
    fetched = _get(f"{url}?meta=1")
    try:
        meta = json.loads(fetched.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CloneError("the page's host did not return a readable clone inventory") from None
    if not isinstance(meta, dict) or not meta.get("clone"):
        raise CloneError("that URL is not a clonable fused-render page")
    files = meta.get("files")
    files = files if isinstance(files, list) else []
    name = _safe_folder_name(meta.get("name") if isinstance(meta.get("name"), str) else None)
    dest = zip_import.unique_dir(fused_dir(), name)
    return {
        "url": url,
        "name": meta.get("name") if isinstance(meta.get("name"), str) else name,
        "files": [
            {"path": f.get("path"), "bytes": f.get("bytes")}
            for f in files
            if isinstance(f, dict)
        ],
        "bytes": meta.get("bytes"),
        # What the transfer will actually cost, when the host reports it (a newer serve
        # path does; an older one does not, and a client must not print a wrong number).
        "download_bytes": meta.get("download_bytes"),
        "dest": dest,
        # The folder name only ever differs from `name` when something is already there —
        # surface it so the confirm step can say "will be cloned as …" rather than
        # surprising the user after the fact.
        "folder": os.path.basename(dest),
        "renamed": os.path.basename(dest) != name,
    }


# -- clone ---------------------------------------------------------------------


def _staging_root() -> str:
    return os.path.join(fused_dir(), ".clone-staging")


def clone(src: str, folder: str | None = None) -> dict:
    """Download, validate, unpack, and move a deployed page into the Fused workspace.

    Staged first, then moved: an archive that fails any check must leave nothing behind,
    and a half-written page in the workspace would look like a real one. The move is a
    rename into a directory that did not exist a moment ago (`unique_dir`), so it can
    neither overwrite nor merge into anything.

    `folder` is the destination the confirm step showed the user (CL-1). It is honoured when
    it is still free, which is what makes the preview's promise hold: the preview writes
    nothing, so it cannot *reserve* a name, and recomputing the name here would silently
    land the clone somewhere else if the workspace changed in between. When it is no longer
    free — or absent, as for a programmatic caller — the name is derived as usual and the
    returned `folder` is the authoritative answer. Client-supplied, so it goes through
    `_safe_folder_name` like any other untrusted name and can only ever be one segment.
    """
    url = clone_url_from(src)
    fetched = _get(url)
    try:
        zf = zipfile.ZipFile(io.BytesIO(fetched.content))
    except zipfile.BadZipFile:
        raise CloneError("the download was not a valid .zip archive") from None

    root = _staging_root()
    os.makedirs(root, exist_ok=True)
    zip_import.sweep_stale_staging(root, STAGING_TTL_SEC)
    staging_dir = os.path.join(root, secrets.token_hex(16))
    try:
        zip_import.extract_to_staging(
            zf,
            staging_dir,
            max_entries=MAX_ENTRIES,
            max_entry_bytes=MAX_ENTRY_UNCOMPRESSED,
            max_total_bytes=MAX_TOTAL_UNCOMPRESSED,
        )
    except (zip_import.ZipRejected, zip_import.ZipTooLarge) as exc:
        raise CloneError(str(exc)) from None
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise CloneError(f"could not unpack the download: {exc}") from None

    try:
        layout = _read_bundle(staging_dir)
        name = _safe_folder_name(layout["name"])
        os.makedirs(fused_dir(), exist_ok=True)
        reserved = _safe_folder_name(folder) if folder else None
        if reserved and not os.path.exists(os.path.join(fused_dir(), reserved)):
            dest = os.path.join(fused_dir(), reserved)
        else:
            dest = zip_import.unique_dir(fused_dir(), name)
        # The payload dir becomes the page folder: the manifest's `root` holds the files at
        # their real page-relative paths, which is exactly the local layout. The manifest
        # itself rides along as a dotfile so a re-export can reproduce the same bundle
        # without the user having to keep it somewhere.
        shutil.move(layout["payload"], dest)
        try:
            shutil.move(
                layout["manifest_path"], os.path.join(dest, ".fused-render-bundle.json")
            )
        except OSError as exc:
            # The commit is two moves, so the second one failing would otherwise leave a
            # clone in the workspace that this call reports as failed — the one state the
            # stage-then-move design exists to prevent. Roll the first move back.
            shutil.rmtree(dest, ignore_errors=True)
            raise CloneError(f"could not finish writing the clone: {exc}") from None
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    page = os.path.join(dest, layout["page"])
    return {
        "dest": dest,
        "folder": os.path.basename(dest),
        "page": page,
        "view": _view_url_path(os.path.abspath(page)),
        "files": layout["count"],
    }


def _read_bundle(staging_dir: str) -> dict:
    """Locate the manifest, the payload dir, and the page inside a staged bundle.

    Everything here is read from an archive we did not create, so each field is validated
    rather than trusted: `root` and `page` become paths, so a value like `../..` or `/etc`
    must be refused before it is joined — the same reason the serve path validates them at
    publish time.
    """
    manifest_path = os.path.join(staging_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise CloneError("the download is missing its manifest.json — not a page bundle")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        raise CloneError("the download's manifest.json could not be read") from None
    if not isinstance(manifest, dict):
        raise CloneError("the download's manifest.json is not an object")
    version = manifest.get("fused_render_bundle")
    if version != 2:
        raise CloneError(
            f"unsupported bundle format ({version!r}); this version of fused-render "
            "can only clone v2 export bundles"
        )
    root = manifest.get("root")
    page = manifest.get("page")
    if not isinstance(root, str) or not root or not isinstance(page, str) or not page:
        raise CloneError("the download's manifest.json is missing 'root' or 'page'")
    payload = _contained(staging_dir, root, "root", child=True)
    if not os.path.isdir(payload):
        raise CloneError(f"the download declares root {root!r} but no such folder is in it")
    page_path = _contained(payload, page, "page")
    if not os.path.isfile(page_path):
        raise CloneError(f"the download declares page {page!r} but no such file is in it")
    count = sum(len(files) for _r, _d, files in os.walk(payload))
    return {
        "name": manifest.get("name") if isinstance(manifest.get("name"), str) else None,
        "payload": payload,
        "page": page.replace("\\", "/"),
        "manifest_path": manifest_path,
        "count": count,
    }


def _contained(base: str, relative: str, field: str, *, child: bool = False) -> str:
    """`base/relative`, proven to stay under `base`.

    Same containment rule `zip_import.reject_reason` applies to entry names, applied to the
    manifest's own path fields — the archive's entries can be safe while a manifest field
    points outside the bundle, and this one is used to *move* a directory.

    ``child=True`` additionally requires the result to be strictly *inside* `base`, which is
    what `root` needs: a `root` of ``"."`` resolves to the staging directory itself, and
    accepting that broke the staged-then-move guarantee outright — `clone` moved the staging
    dir into the workspace and then tried to move `manifest.json` out of the path it had just
    vacated, raising an uncaught `FileNotFoundError` and leaving a half-built clone behind. So
    `root` must name a real child directory, not the bundle root.
    """
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or os.path.isabs(normalized):
        raise CloneError(f"the download's {field} must be a relative path, not {relative!r}")
    if any(part == ".." for part in normalized.split("/")):
        raise CloneError(f"the download's {field} may not contain '..' ({relative!r})")
    target = os.path.normpath(os.path.join(base, normalized))
    root = os.path.normpath(base)
    if target != root and not target.startswith(root + os.sep):
        raise CloneError(f"the download's {field} escapes the bundle ({relative!r})")
    if child and target == root:
        raise CloneError(
            f"the download's {field} must name a folder inside the bundle, not the bundle "
            f"itself ({relative!r})"
        )
    return target


# -- routes (included by server.create_app) ------------------------------------


@router.get("/api/clone-app/info")
def api_clone_app_info(src: str = ""):
    """Read-only preview for the confirm step: what would be cloned, and where to."""
    try:
        return probe(src)
    except CloneError as exc:
        return _error(str(exc))


@router.post("/api/clone-app")
def api_clone_app(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    folder = body.get("folder")
    try:
        return clone(
            str(body.get("src") or ""),
            folder if isinstance(folder, str) and folder else None,
        )
    except CloneError as exc:
        return _error(str(exc))
