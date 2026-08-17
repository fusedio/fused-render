import logging
import os
import urllib.error
import urllib.request

from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Header
from fastapi.responses import HTMLResponse

from fused_render.server.common import _error
from fused_render.server.session import _is_file_mount_safe
from fused_render.shell import mounts as shell_mounts

router = APIRouter()

logger = logging.getLogger(__name__)


def _referred_by_preview(referer: str | None) -> bool:
    """True when the DOCUMENT that requested this render is itself a preview
    (`_preview=1` in the referring URL's query). Covers the flag's one gap: a
    previewed page that directly iframes another `/render?path=...` URL — its
    author never wrote the flag, but same-origin requests carry a full-URL
    Referer, so the parent's stamp is visible here. Shell-mediated nesting
    (/embed/, /view/) is covered client-side instead (router.IS_PREVIEW walks
    ancestor frames), because there the referrer is the unflagged shell URL."""
    if not referer:
        return False
    try:
        return parse_qs(urlsplit(referer).query).get("_preview") == ["1"]
    except ValueError:
        return False


@router.get("/render")
def render(
    path: str,
    _preview: str | None = None,
    referer: str | None = Header(default=None),
):
    if not _is_file_mount_safe(path):
        return _error(f"no such file: {path}", status=404)
    # Mount-backed pages read through the rclone serve like /api/fs/raw:
    # the kernel mount's first cold read can fail (EINVAL) mid-warmup.
    upstream = shell_mounts.serve_url_for(path)
    if upstream is not None:
        try:
            with urllib.request.urlopen(upstream, timeout=120) as r:
                html = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            e.close()
            return _error(f"cannot read {path}: HTTP {e.code}",
                          status=404 if e.code == 404 else 400)
        except OSError:
            return _error("mount serve unavailable", status=503)
    elif shell_mounts.is_mount_backed(path):
        return _error("mount serve unavailable", status=503)
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
        except OSError as e:
            return _error(f"cannot read {path}: {e}", status=400)

    # A page carrying the fused-app marker being rendered IS the app being
    # opened (D301): record recency here — and, for a folder outside the
    # workspace, registration on the /apps hub — instead of trusting any
    # client-side post. `_preview=1` is the card/preview iframes saying "this
    # render is a thumbnail, not an open": the default RECORDS, deliberately —
    # a preview missing the flag pollutes recency (minor), a real open wrongly
    # flagged never registers an external app (breaks the only path in).
    # Templates render through here too and carry no marker, so they never
    # record. Best-effort: recording must never fail the render.
    if _preview != "1" and not _referred_by_preview(referer):
        from fused_render.app_listing import text_has_fused_meta
        from fused_render.server.routers.apps import record_app_open

        try:
            if text_has_fused_meta(html):
                record_app_open(os.path.dirname(os.path.abspath(path)))
        except Exception:
            logger.warning("recording app open failed for %s", path, exc_info=True)

    # Always inject the runtime.
    injection = '<script src="/static/runtime.js"></script>'
    lower = html.lower()
    head_idx = lower.find("<head>")
    if head_idx != -1:
        insert_at = head_idx + len("<head>")
        html = html[:insert_at] + injection + html[insert_at:]
    else:
        html = injection + html
    return HTMLResponse(html)
