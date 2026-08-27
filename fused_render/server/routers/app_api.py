"""The app page's API tab backend: every ``.py`` under one app folder, described
the way the ``api`` template describes ONE file — module docstring, project
dependencies, and the entrypoint the active engine would call, with its
parameters. ``GET /api/apps/py?path=<dir>`` answers for the whole folder in one
request so the tab paints a complete list at once instead of inspecting file by
file.

The description is ``templates/api/inspector.py``'s, imported from the package's
own templates tree (the source of truth core_templates.py stages from) rather
than reimplemented: the tab and the template must describe a file identically,
and two ``ast`` walkers would drift. The inspector is stdlib-only with no import
side effects (pinned by ``test_no_template_imports_fused_render``), so an
in-process import is safe; running it through the executor per file would cost
a subprocess spawn per ``.py`` on every tab open.

The engine is resolved HERE (``shell_prefs.effective_engine()``), the same rule
``/api/run`` applies, so the form always describes the function that will
actually run — the template asks ``/api/config`` for the same value.

The walk is ``server/walk._walk_bfs``: gitignore-pruned, hidden entries skipped,
bounded — an app folder is small, and a bound that fires is reported as
``truncated`` rather than hidden.
"""
import importlib.util
import os

from fastapi import APIRouter

from fused_render.core_templates import PACKAGE_TEMPLATES_DIR
from fused_render.server import walk as _server_walk
from fused_render.server.common import _error
from fused_render.shell import mounts as shell_mounts
from fused_render.shell import prefs as shell_prefs

router = APIRouter()

# Bounds for the app-folder walk: an app is one to three levels deep with a
# handful of files; these only exist so a folder that is secretly a data dump
# cannot make the tab inspect thousands of scripts.
_MAX_ENTRIES = 2000
_MAX_DEPTH = 8
_MAX_PY_FILES = 200

_inspector = None


def _load_inspector():
    """The api template's inspector module, imported once from the package tree."""
    global _inspector
    if _inspector is None:
        path = os.path.join(PACKAGE_TEMPLATES_DIR, "api", "inspector.py")
        spec = importlib.util.spec_from_file_location("fused_render_api_inspector", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _inspector = mod
    return _inspector


def _py_files(root: str):
    """Posix-relative ``.py`` paths under *root*, and whether the walk was cut short."""
    rels = []
    truncated = False
    walker = _server_walk._walk_bfs(
        root, False, max_entries=_MAX_ENTRIES, max_depth=_MAX_DEPTH
    )
    for item in walker:
        if item is _server_walk._WALK_TRUNCATED:
            truncated = True
            continue
        if item["is_dir"] or not item["rel"].endswith(".py"):
            continue
        rels.append(item["rel"])
        if len(rels) >= _MAX_PY_FILES:
            truncated = True
            break
    return rels, truncated


def describe_folder(root: str) -> dict:
    """Every ``.py`` under *root* described by the api inspector, sorted by path."""
    inspector = _load_inspector()
    engine = shell_prefs.effective_engine()
    rels, truncated = _py_files(root)
    endpoints = []
    for rel in sorted(rels, key=str.lower):
        path = root + "/" + rel
        endpoints.append(_describe_file(inspector, path, rel, engine))
    return {"engine": engine, "endpoints": endpoints, "truncated": truncated}


def _describe_file(inspector, path: str, rel: str, engine: str) -> dict:
    """One file's entry. Never raises: one unreadable or unparseable file must
    not take the rest of the list down with it. Two distinct failure fields —
    ``parse_error`` is the file's fault (a syntax error, a null byte),
    ``read_error`` is the filesystem's (permissions, a vanished file) — so the
    tab can say which, instead of calling every failure a syntax error."""
    try:
        info = inspector.main(path, engine)
    except OSError as e:
        info = {"parse_error": None, "read_error": e.strerror or str(e)}
    except ValueError as e:
        # ast.parse raises this for source it refuses outright (null bytes).
        info = {"parse_error": str(e)}
    except Exception as e:  # noqa: BLE001 — display-only; report, don't propagate
        info = {"parse_error": None, "read_error": f"{type(e).__name__}: {e}"}
    info["rel"] = rel
    info["path"] = path
    info["entrypoint"] = _entrypoint_kind(inspector, info, path, engine)
    return info


def _entrypoint_kind(inspector, info: dict, path: str, engine: str):
    """``"udf"`` | ``"main"`` | ``"result"`` | None — WHICH rule picked the
    function, which the inspector's payload does not say. A ``@fused.udf``
    function may itself be named ``main``, so the name cannot tell. The builtin
    executor never returns a decorated function (inspector._find_entrypoint),
    so under the fused engine a second, builtin-rules pass distinguishes the
    two: a function fused picks that builtin does not is the UDF."""
    fn = info.get("function")
    if fn is None:
        return "result" if info.get("static_result") else None
    if engine != "fused":
        return "main"
    try:
        builtin_fn = inspector.main(path, "builtin").get("function")
    except Exception:  # noqa: BLE001 — the first pass succeeded; be lenient
        return "main" if fn["name"] == "main" else "udf"
    return "main" if builtin_fn and builtin_fn["name"] == fn["name"] else "udf"


@router.get("/api/apps/py")
def api_app_py(path: str):
    # Sync on purpose: FastAPI runs it in the threadpool, and the walk plus the
    # per-file parse are blocking filesystem work.
    root = path.replace("\\", "/").rstrip("/")
    if not root or not os.path.isdir(root):
        return _error(f"not a directory: {path}", status=404)
    try:
        return describe_folder(root)
    except shell_mounts.RcListError as e:
        # A mount-backed app folder whose listing failed (dead mount, timeout):
        # the same structured answer /api/fs/walk gives, not a bare 500.
        return _server_walk._mount_list_error_response(root, e)
