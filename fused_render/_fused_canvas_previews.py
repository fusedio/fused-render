"""Sign a batch of canvas preview images IN PARALLEL; print JSON on stdout.

Run as ``[sys.executable, _fused_canvas_previews.py]`` by canvases.py (same
in-interpreter spawn pattern as _fused_canvases_list.py), with the collection
ids as a JSON array on STDIN — not argv, which has a length limit an account
with hundreds of canvases would reach.

Prints ``{"<collection_id>": "<https url>" | null, …}``: one entry per id
asked for, null where the control plane has no image for it (404) or the
request failed. A missing/failed signature is never an error — the card falls
back to its letter thumb.

Why a shim of its own (D360): the listing shim used to sign each preview
inline, one blocking round trip per canvas, so the whole page waited on
N × ~0.9s before rendering a single card. Here the round trips run on a small
thread pool (they are pure network waits, so the GIL is irrelevant), which
makes the batch cost ~one round trip regardless of N — and it runs AFTER the
listing has already painted.

FUSED_ENV is honored the same way the CLI does it (fused._env), so the
signatures come from the same environment the listing came from.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import fused

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

import requests  # a fused dependency; imported after env setup like the CLI

from fused._global_api import get_api

_SIGN_TIMEOUT = 20
# Enough concurrency to collapse a big account's batch into ~one round trip,
# low enough not to look like a burst to the control plane.
_MAX_WORKERS = 8


def _sign(api, headers: dict, collection_id: str) -> str | None:
    # 404 = no thumbnail uploaded yet; any failure just means no preview.
    try:
        r = requests.get(
            f"{api.base_url}/collection/sign-image",
            params={"collection_id": collection_id},
            headers=headers,
            timeout=_SIGN_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        signed = r.json()
    except ValueError:
        return None
    return signed if isinstance(signed, str) and signed else None


def main() -> None:
    try:
        ids = json.load(sys.stdin)
    except ValueError:
        ids = None
    if not isinstance(ids, list):
        json.dump({}, sys.stdout)
        return
    wanted = [i for i in ids if isinstance(i, str) and i]
    if not wanted:
        json.dump({}, sys.stdout)
        return
    api = get_api()
    # Generated once, outside the pool: _generate_headers may refresh the
    # credential, and N threads racing to refresh the same one is both wasteful
    # and the kind of thing that trips a token endpoint's rate limit.
    headers = api._generate_headers()
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(wanted))) as pool:
        signed = list(pool.map(lambda i: _sign(api, headers, i), wanted))
    json.dump(dict(zip(wanted, signed)), sys.stdout)


if __name__ == "__main__":
    main()
