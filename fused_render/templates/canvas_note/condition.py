"""Gate for the `canvas_note` template (SPEC §53.2 NT-2, CT-12).

True only for a NOTE node's file (`<node>.md`) of
a canvas folder under `user/`, with Canvases on.

Runs in the SERVER process when `/api/fs/conditions` resolves a file's gated
modes in the background (CT-12). Cheap, and it never raises: a gate that
cannot decide denies (fail closed).

The rule is the sync engine's own (`fused_render.canvases.node_template_gate`
over `canvas_node_file`), the one GET /api/canvases/node — which this
template's page then calls — also answers with. In the server process the
module is already loaded (its router is mounted), so the import is a dict
lookup.
"""

KINDS = ("note",)


def main(path) -> bool:
    try:
        from fused_render import canvases

        return canvases.node_template_gate(path, KINDS)
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
