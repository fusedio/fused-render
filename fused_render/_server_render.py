import urllib.error
import urllib.request

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from fused_render._server_common import _error
from fused_render._server_session import _is_file_mount_safe
from fused_render.shell import mounts as shell_mounts

router = APIRouter()


@router.get("/render")
def render(path: str):
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
