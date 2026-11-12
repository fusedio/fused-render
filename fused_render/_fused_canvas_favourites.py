"""Read or toggle the account's favourite canvases; print JSON on stdout.

Run as ``[sys.executable, _fused_canvas_favourites.py]`` by canvases.py (same
in-interpreter spawn pattern as _fused_canvas_previews.py), with the request
as a JSON object on STDIN:

  {"op": "get"}                       → {"collection_ids": [...]}
  {"op": "put" | "delete", "id": ID}  → {"ok": true}

The control plane's own favourites endpoints (`/collection/self/favourites`),
the same ones the hosted workbench home's star calls — so a canvas starred on
either side is starred on both. A failed write exits non-zero with the
server's message on stderr; the page rolls its optimistic toggle back.

FUSED_ENV is honored the same way the CLI does it (fused._env), so the
favourites are the environment's the listing came from.
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

import requests  # a fused dependency; imported after env setup like the CLI

from fused._global_api import get_api

_TIMEOUT = 20


def _fail(message: str) -> None:
    sys.stderr.write(message)
    sys.exit(1)


def main() -> None:
    try:
        req = json.load(sys.stdin)
    except ValueError:
        req = None
    if not isinstance(req, dict):
        _fail("favourites helper: expected a JSON object on stdin")
        return
    op = req.get("op")
    api = get_api()
    headers = api._generate_headers()
    base = f"{api.base_url}/collection/self/favourites"
    if op == "get":
        r = requests.get(base, headers=headers, timeout=_TIMEOUT)
        if r.status_code != 200:
            _fail(f"reading favourites failed ({r.status_code}): {r.text[:200]}")
            return
        ids = r.json().get("collection_ids") if isinstance(r.json(), dict) else None
        out = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
        json.dump({"collection_ids": out}, sys.stdout)
        return
    if op in ("put", "delete"):
        cid = req.get("id")
        if not isinstance(cid, str) or not cid:
            _fail("favourites helper: 'id' must be a collection id")
            return
        method = requests.put if op == "put" else requests.delete
        r = method(f"{base}/{cid}", headers=headers, timeout=_TIMEOUT)
        if r.status_code not in (200, 204):
            verb = "adding" if op == "put" else "removing"
            _fail(f"{verb} favourite failed ({r.status_code}): {r.text[:200]}")
            return
        json.dump({"ok": True}, sys.stdout)
        return
    _fail(f"favourites helper: unknown op {op!r}")


if __name__ == "__main__":
    main()
