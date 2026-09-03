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

@router.get("/home")
# `/sessions` — the Claude Sessions inbox — stood here until 2026-08-18, and
# `/learn` — the bundled App Basics content — until 2026-08-22 (D419); both are
# gone with the pages they served: Tasks answers the same question the inbox
# did and answers it better (Akshil), and the learn content ships as a
# community app now. The sessions MOUNT is untouched: the bundled inbox app is
# still on disk under the mounts root and still opens in the explorer like any
# other view. What was deleted is the shell pages that gave them routes.
# The Claude pages: the settings panel (frontend apps/claude_config) and the
# retired /claude-md page, which the client rewrites to the panel's MD Files
# section. Listed here for the same reason as the rest — in-app navigation is
# client-side, but a bookmark or a refresh on one of these URLs is a real GET
# the server has to answer with the shell.
@router.get("/claude-config")
@router.get("/claude-md")
@router.get("/preferences")
@router.get("/templates")
@router.get("/mounts")
# AI Models (SPEC §37) — a client-side page like the rest, and reachable by URL
# even where the sidebar hides its entry. The bare prefix is what the sidebar
# links to; its five TABS are sub-paths (`/ai-models/local`, …) served by the
# wildcard below.
@router.get("/ai-models")
# Tasks (SPEC §41) — served at `/scheduled` until 2026-08-18 and renamed with NO
# redirect behind it: the page is called Tasks everywhere a person reads it, and
# one address for one page is the whole point of the rename. An old `/scheduled`
# link now 404s, which is the accepted cost.
#
# Omitting a page here is what a missing entry in this list looks like from the
# outside: in-app navigation worked (it is a client-side pushState and never asks
# the server), and a refresh or a bookmark 404'd. That asymmetry is why
# test_shell_routes.py derives this list from the shell's own route table rather
# than trusting the next page to remember.
@router.get("/tasks")
# The first-run setup wizard (frontend shell/onboarding): its own page, so
# the client lands on it with a plain navigation and leaves it the same way,
# and Help › Setup wizard is an ordinary link. A refresh mid-wizard stays on it.
@router.get("/onboarding")
# Canvases (legacy-workbench local development): the listing page and the
# per-canvas workspace (/canvases/<name>, matched by the wildcard below).
@router.get("/canvases")
def shell_page(shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)


@router.get("/canvases/{name}")
def shell_canvas_workspace(name: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)


# The app page (D488, redesigned 2026-08-26): `/apps/<folder path>?_tab=<tab>`
# is ONE app folder — anywhere the Current apps desk can name (a workspace
# folder, a linked app elsewhere on disk) — with the app running in an
# Overview tab, its tasks in a Tasks tab and its files in a Files tab
# (frontend shell/AppPage.tsx). The folder rides as path segments in the
# explorer's own codec (router.ts encodeFsPathSegments) and the tab as a
# query param (a trailing tab segment would be ambiguous with a folder named
# `tasks`), so this is a wildcard over everything under /apps/. The path is
# validated client-side (current-apps-lib appPathFromPath); this is only the
# shell fallback that lets a refresh or a bookmark land on the page.
@router.get("/apps/{path:path}")
def shell_app_page(path: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)


# The AI Models tabs. A wildcard rather than five literals because the tab set
# is the FRONTEND's list (apps/ai_models/routes.ts) and the server has no
# business holding a second copy of it — an unknown tab falls back to the
# default in the client, which is the same forgiving posture a stale `?tab=`
# had. One level only: this page has no second.
@router.get("/ai-models/{tab}")
def shell_ai_models_tab(tab: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)

# Pre-rename URL shapes; the client rewrites them in place at boot
# (frontend router.ts rewriteLegacyPath), so keep serving the shell here.
@router.get("/view/{path:path}")
def shell_view(path: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)

@router.get("/embed/{path:path}")
def shell_embed(path: str, shell_path: str = Depends(get_shell_path)):
    return FileResponse(shell_path)
