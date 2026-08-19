"""Print the account's canvases (lite collections) as JSON on stdout.

Run as ``[sys.executable, _fused_canvases_list.py]`` by canvases.py (same
in-interpreter spawn pattern as _fused_token.py). Richer than the CLI's
`canvas list` (which prints bare names): each entry carries the collection id,
last_updated, and its preview state.

Preview resolution mirrors the hosted workbench: `preview_image_url` is either
a public https URL (used as-is — free, it is already in the list payload), the
"fused_uploaded_preview" sentinel (the image lives in the private image bucket
and needs a presigned URL from the control plane), or null (no preview).

This shim resolves ONLY the free case. A sentinel is reported as
``preview_pending: true`` and signed later by _fused_canvas_previews.py, off
the listing's critical path (D364): signing costs one control-plane round trip
per canvas, and doing them here — sequentially, before the page can render
anything — made the listing scale linearly with the account's canvas count.

FUSED_ENV is honored the same way the CLI does it (fused._env), so the list
comes from the same environment the pull/push CLI calls target.
"""
import json
import os
import sys

import fused

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

from fused._global_api import get_api

# The workbench's marker for "preview stored in the private image bucket".
_SENTINEL = "fused_uploaded_preview"

# The workbench counts a canvas's UDFs with these exclusions (client's
# `getCodeUdfCount`): sticky notes and widgets are nodes but not code, and an
# `app` node is a published app rather than a UDF. Mirrored here so the card's
# count matches what the hosted gallery prints for the same canvas.
_NON_UDF_PREFIXES = ("note_", "widget_")
_NON_UDF_TYPE = "app"


def _code_udf_count(collection: dict) -> int | None:
    """Count the collection's code UDFs, or None if the payload has no node list.

    `lite=True` already carries `udf_ids` (id, slug, udf_type per node), so this
    costs no extra control-plane call — it must not, since the whole point of
    D364 was getting round trips off the listing's critical path.
    """
    nodes = collection.get("udf_ids")
    if not isinstance(nodes, list):
        return None
    count = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        slug = node.get("slug")
        if isinstance(slug, str) and slug.startswith(_NON_UDF_PREFIXES):
            continue
        if node.get("udf_type") == _NON_UDF_TYPE:
            continue
        count += 1
    return count


def main() -> None:
    api = get_api()
    collections = api.list_collections(whose="self", lite=True)
    out = []
    for collection in collections:
        if not isinstance(collection, dict):
            continue
        name = collection.get("name")
        if not isinstance(name, str) or not name:
            continue
        collection_id = collection.get("id")
        raw_preview = collection.get("preview_image_url")
        preview_url = None
        preview_pending = False
        if raw_preview == _SENTINEL and collection_id:
            preview_pending = True
        elif isinstance(raw_preview, str) and raw_preview.startswith(
            ("http://", "https://")
        ):
            preview_url = raw_preview
        out.append(
            {
                "name": name,
                "id": collection_id,
                "preview_url": preview_url,
                "preview_pending": preview_pending,
                "n_code_udfs": _code_udf_count(collection),
                "last_updated": collection.get("last_updated"),
            }
        )
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
