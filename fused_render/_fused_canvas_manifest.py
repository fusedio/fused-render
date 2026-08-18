"""Print one canvas's remote content manifest as JSON on stdout.

Run as ``[sys.executable, _fused_canvas_manifest.py, <canvas_name>]`` by
canvases.py (same in-interpreter spawn pattern as _fused_canvases_list.py).
The manifest is the cheap remote-change probe for the sync watcher: the
collection's ``last_updated`` (bumped by layout/dashboard edits) plus each
UDF's server-side ``udf_body_hash`` and ``last_updated`` (bumped by UDF
edits). Comparing two manifests taken at different times tells whether the
REMOTE canvas moved — the hashes are never compared against local files
(the exported file layout doesn't match ``md5(udf_body)``).

FUSED_ENV is honored the same way the CLI does it (fused._env), so the
manifest comes from the same environment the pull/push CLI calls target.
"""
import json
import os
import sys

import fused

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

from fused._global_api import get_api


def main() -> None:
    name = sys.argv[1]
    collection = get_api().get_collection_by_name(name)
    udfs = {}
    for udf in collection.get("udfs") or []:
        if not isinstance(udf, dict):
            continue
        slug = udf.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        udfs[slug] = {
            "hash": udf.get("udf_body_hash"),
            "last_updated": udf.get("last_updated"),
        }
    json.dump(
        {
            "id": collection.get("id"),
            "last_updated": collection.get("last_updated"),
            "udfs": udfs,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
