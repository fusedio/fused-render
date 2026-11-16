"""Gate for the `udf` template — the node view (SPEC §53.3 NV-1, NV-2).

`udf` is a NATIVE template (PT-6 amendment, like `claude`): the node view is
drawn by the React shell (frontend/src/apps/canvases/NodeView.tsx), and this
folder is only its registry name, this gate and its icon. It is bound to
`.py`, `.json`, `.*.json`, `.html` and `.md` after each key's default, and is
conditional, so a file's default mode never changes (PT-8).

Runs in the SERVER process when `/api/fs/conditions` resolves a file's gated
modes in the background (CT-12). Cheap, and it never raises: a gate that
cannot decide denies (fail closed).

`main(path)` is True only when, cheapest first:

  * the `canvases_enabled` preference is on (a read of prefs.json — every
    file of the four kinds pays only this while Canvases is off);
  * the path is a node's source file at the root of a `user/<canvas>` folder
    of the canvases root, with a node row of that name in the folder's
    canvas.toml (at most 2 MB) whose file is this one —
    `fused_render.canvases.canvas_node_file`, the one rule this gate and
    GET /api/canvases/node share. `team/` and `public/` copies never pass; a
    reference copy under `user/` does (the view clones it in place).

`fused_render.canvases` is imported only after the preference said yes. In
the server process it is already loaded (its router is mounted), so the
import is a dict lookup; it never imports `fused_render.server`, so no cycle
can start here.
"""


def main(path) -> bool:
    try:
        from fused_render.shell import prefs

        if not prefs.canvases_enabled():
            return False
        from fused_render import canvases

        return canvases.canvas_node_file(path) is not None
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
