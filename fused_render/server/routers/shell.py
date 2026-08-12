from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from fused_render.server.common import get_shell_path

router = APIRouter()


@router.get("/")
def shell_root(shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)

@router.get("/apps")
def shell_apps(shell_path: str = Depends(get_shell_path)):
    # The apps hub (the app home) — client-side, so serving the shell makes a
    # direct load and a refresh work. The wildcard that also served
    # /apps/<tag>/<name> is gone with that route: an app folder is browsed on
    # /explorer/view/<path> now, and a stale deep link is better off as a 404
    # than as a shell that renders "Unrecognized URL".
    return FileResponse(shell_path)

@router.get("/explorer")
@router.get("/explorer/{path:path}")
def shell_explorer(path: str = "", shell_path: str = Depends(get_shell_path)):
    # File-explorer namespace: /explorer (homepage), /explorer/view/<path>,
    # /explorer/embed/<path>.
    return FileResponse(shell_path)

@router.get("/learn")
@router.get("/sessions")
# The Claude pages: the settings panel (frontend apps/claude_config), the
# artifacts list, and the retired /claude-md page, which the client rewrites to
# the panel's MD Files section. Listed here for the same reason as the rest —
# in-app navigation is client-side, but a bookmark or a refresh on one of these
# URLs is a real GET the server has to answer with the shell.
@router.get("/claude-config")
@router.get("/claude-artifacts")
@router.get("/claude-md")
@router.get("/preferences")
@router.get("/templates")
@router.get("/mounts")
# The Hugging Face cache inventory (SPEC §37) — a client-side page like the
# rest, and reachable by URL even where the sidebar hides its entry.
@router.get("/ai-models")
def shell_page(shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)

# Pre-rename URL shapes; the client rewrites them in place at boot
# (frontend router.ts rewriteLegacyPath), so keep serving the shell here.
@router.get("/view/{path:path}")
def shell_view(path: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)

@router.get("/embed/{path:path}")
def shell_embed(path: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)
