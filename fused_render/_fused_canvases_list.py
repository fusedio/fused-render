"""Print the account's canvases (lite collections) as JSON on stdout.

Run as ``[sys.executable, _fused_canvases_list.py]`` by canvases.py (same
in-interpreter spawn pattern as _fused_token.py). Richer than the CLI's
`canvas list` (which prints bare names): each entry carries the collection id,
last_updated, and a resolved preview image URL.

Preview resolution mirrors the hosted workbench: `preview_image_url` is either
a public https URL (used as-is), the "fused_uploaded_preview" sentinel (the
image lives in the private image bucket — exchanged for a presigned URL via
the control plane's /collection/sign-image), or null (no preview).

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

import requests  # a fused dependency; imported after env setup like the CLI

from fused._global_api import get_api

# The workbench's marker for "preview stored in the private image bucket".
_SENTINEL = "fused_uploaded_preview"
_SIGN_TIMEOUT = 20


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
        if raw_preview == _SENTINEL and collection_id:
            # 404 = no thumbnail uploaded yet; any failure just means no
            # preview — the card falls back to its letter thumb.
            try:
                r = requests.get(
                    f"{api.base_url}/collection/sign-image",
                    params={"collection_id": collection_id},
                    headers=api._generate_headers(),
                    timeout=_SIGN_TIMEOUT,
                )
                if r.status_code == 200:
                    signed = r.json()
                    if isinstance(signed, str) and signed:
                        preview_url = signed
            except (requests.RequestException, ValueError):
                pass
        elif isinstance(raw_preview, str) and raw_preview.startswith(
            ("http://", "https://")
        ):
            preview_url = raw_preview
        out.append(
            {
                "name": name,
                "id": collection_id,
                "preview_url": preview_url,
                "last_updated": collection.get("last_updated"),
            }
        )
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
