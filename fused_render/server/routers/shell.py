from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from fused_render.server.common import get_shell_path

router = APIRouter()


@router.get("/")
def shell_root(shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)

@router.get("/apps")
@router.get("/apps/{path:path}")
def shell_apps(path: str = "", shell_path: str = Depends(get_shell_path)):
    # Apps hub (the app home) plus /apps/<tag>/<name> app-builder routes — all
    # client-side; serving the shell makes direct loads and refreshes work.
    return FileResponse(shell_path)

@router.get("/explorer")
@router.get("/explorer/{path:path}")
def shell_explorer(path: str = "", shell_path: str = Depends(get_shell_path)):
    # File-explorer namespace: /explorer (homepage), /explorer/view/<path>,
    # /explorer/embed/<path>.
    return FileResponse(shell_path)

@router.get("/learn")
@router.get("/sessions")
# The Claude Config app's two pages (frontend apps/claude_config): the settings
# panel and the CLAUDE.md file explorer. Listed here for the same reason as the
# rest — in-app navigation is client-side, but a bookmark or a refresh on one of
# these URLs is a real GET the server has to answer with the shell.
@router.get("/claude-config")
@router.get("/claude-md")
@router.get("/preferences")
@router.get("/templates")
@router.get("/mounts")
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
