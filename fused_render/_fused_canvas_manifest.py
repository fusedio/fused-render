"""Print one canvas's remote content manifest as JSON on stdout.

Run as ``[sys.executable, _fused_canvas_manifest.py, <canvas_name>]`` by
canvases.py (same in-interpreter spawn pattern as _fused_canvases_list.py).
The manifest is the cheap remote-change probe for the sync watcher: the
collection's ``last_updated`` (bumped by layout/dashboard edits) plus each
UDF's server-side ``udf_body_hash`` and ``last_updated`` (bumped by UDF
edits). Comparing two manifests taken at different times tells whether the
REMOTE canvas moved — the hashes are never compared against local files
(the exported file layout doesn't match ``md5(udf_body)``).

A server with the revision spine (2026-11-11) also sends the collection's
``revision`` (bumped by every content write), ``lease_owner`` /
``lease_until`` (who has the canvas checked out) and each UDF's
``revision``. They are copied ONLY when the server sent them — never
null-filled — so the manager feature-detects with ``"revision" in
manifest``: present, a move is a revision change and nothing else.

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

from fused_render._fused_options import raise_request_timeout

raise_request_timeout()

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
            # The UDF's server id: the embed learns it through an `ids`
            # delta once the push that created the UDF lands.
            "id": udf.get("id"),
        }
        if "revision" in udf:
            udfs[slug]["revision"] = udf.get("revision")
    out = {
        "id": collection.get("id"),
        "last_updated": collection.get("last_updated"),
        "udfs": udfs,
    }
    for key in ("revision", "lease_owner", "lease_until"):
        if key in collection:
            out[key] = collection.get(key)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
