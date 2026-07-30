"""GET /api/app/resolve — nearest enclosing fused_app for a path.

Backs `fused.navigate()` (runtime.js): a page asks "which app am I inside?"
by sending its own file path; the server walks UP from that path's directory
to the nearest ancestor holding a VALID fused_app.json and returns the app
dir plus the parsed manifest, or `{"app_dir": null}` when no ancestor
qualifies.

Validity is exactly the fused_app template's condition gate — the walk calls
`templates._run_condition` with the same condition.py the /api/fs/conditions
endpoint runs, so the two can never disagree on what counts as an app, and a
mount-backed candidate dir automatically gets the gate's rc-routed builtins
shim (never the kernel NFS path). An INVALID manifest at one level is simply
skipped and the walk continues (a valid grandparent still wins).

The walk is bounded: it stops at the filesystem root (dirname fixpoint) and
at _MAX_HOPS as a belt-and-braces guard against pathological paths. Each hop
costs one constant-time gate run — no directory is ever enumerated.
"""
import json
import os

from fastapi import APIRouter

from fused_render.server.common import _error
from fused_render.server.templates import _condition_file, _resolve_name, _run_condition

router = APIRouter()

_MAX_HOPS = 64

# Same bound as the gate's own size guard — the gate just proved the file
# parses under it, so this read can never truncate a manifest the gate passed.
_MANIFEST_MAX_BYTES = 256 * 1024


def _fused_app_condition():
    """The fused_app template's condition.py (user override wins, like every
    template resolution), or None if the template folder is missing/broken."""
    template, _err = _resolve_name("fused_app")
    if template is None:
        return None
    return _condition_file(template)


def _read_manifest(app_dir: str):
    """Parse the manifest the gate just validated. Mount-backed dirs read via
    the rc API (bounded), local dirs via a plain open — mirrors the gate's own
    open() routing so the bytes come from the same place the verdict did."""
    from fused_render.shell import mounts

    manifest_path = os.path.join(app_dir, "fused_app.json")
    if mounts.is_mount_backed(manifest_path):
        data = mounts.rc_read_bounded(manifest_path, cap=_MANIFEST_MAX_BYTES)
    else:
        with open(manifest_path, "rb") as f:
            data = f.read(_MANIFEST_MAX_BYTES)
    return json.loads(data.decode("utf-8"))


@router.get("/api/app/resolve")
def api_app_resolve(path: str):
    """Nearest ancestor app for `path` (a file or directory). Returns
    {"app_dir": <abs dir>, "manifest": {...}} or {"app_dir": null}."""
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute path")
    condition = _fused_app_condition()
    if condition is None:
        return {"app_dir": None}

    # A file's search starts at its own directory; a directory's at itself.
    # No stat needed to tell them apart cheaply/mount-safely: probing the dir
    # itself first is harmless (a file path just fails the gate's isfile on
    # <file>/fused_app.json and the walk moves to its parent — the same dir a
    # stat would have started at).
    current = os.path.normpath(path)
    for _ in range(_MAX_HOPS):
        allowed, _err = _run_condition(condition, current)
        if allowed:
            try:
                return {"app_dir": current, "manifest": _read_manifest(current)}
            except Exception:
                # Manifest vanished/changed between gate and read — treat this
                # level as invalid and keep walking, same skip posture as a
                # gate denial.
                pass
        parent = os.path.dirname(current)
        if parent == current:  # filesystem root — nowhere left to walk
            break
        current = parent
    return {"app_dir": None}
