"""Acquire, renew or release a canvas's server-side edit lease; print one
JSON line on stdout.

Run as ``[sys.executable, _fused_canvas_lease.py, acquire|release,
<collection_id>, <owner>, <ttl_s>]`` by canvases.py's sync manager (same
in-interpreter spawn pattern as _fused_canvas_manifest.py), so no CLI output
has to be parsed. Every handled outcome exits 0 with one of:

  {"ok": true, "owner": ..., "lease_until": ...}          acquired / renewed
  {"ok": true}                                             released
  {"ok": false, "code": "leased", "owner": ..., "lease_until": ...}
                                                           held by someone else
  {"ok": false, "code": "unsupported"}                    fused-py predates the
                                                           lease API: the manager
                                                           logs once and stops
A non-zero exit (or junk) is a transient failure, retried at the next
heartbeat.

FUSED_ENV is honored the same way the CLI does it (fused._env).
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


def _conflict(exc: Exception) -> dict | None:
    """The 409 `leased` detail carried by `exc`, or None when it is not one."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    body = None
    if response is not None:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 — not JSON
            body = None
    if body is None:
        text = str(exc)
        start = text.find("{")
        if start >= 0:
            try:
                body = json.loads(text[start:])
            except ValueError:
                body = None
    detail = body.get("detail") if isinstance(body, dict) and "detail" in body else body
    if isinstance(detail, dict) and detail.get("code") == "leased":
        return detail
    if status == 409:
        return {"code": "leased"}
    return None


def main() -> int:
    action, collection_id, owner = sys.argv[1], sys.argv[2], sys.argv[3]
    ttl_s = int(sys.argv[4]) if len(sys.argv) > 4 else 90
    api = get_api()
    method = getattr(api, "acquire_collection_lease" if action == "acquire"
                     else "release_collection_lease", None)
    if method is None:
        json.dump({"ok": False, "code": "unsupported"}, sys.stdout)
        return 0
    try:
        if action == "acquire":
            got = method(collection_id, owner, ttl_s)
        else:
            method(collection_id, owner)
            json.dump({"ok": True}, sys.stdout)
            return 0
    except Exception as exc:  # noqa: BLE001 — sorted below
        detail = _conflict(exc)
        if detail is None:
            sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
            return 1
        json.dump({"ok": False, "code": "leased", "owner": detail.get("owner"),
                   "lease_until": detail.get("lease_until")}, sys.stdout)
        return 0
    got = got if isinstance(got, dict) else {}
    json.dump({"ok": True, "owner": got.get("owner", owner),
               "lease_until": got.get("lease_until")}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
