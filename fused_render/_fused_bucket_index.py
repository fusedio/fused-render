"""Talk to the Fused server about bucket indexes; print JSON on stdout.

Run as ``[sys.executable, _fused_bucket_index.py, <op>]`` by remote_buckets.py
(the same in-interpreter spawn as _fused_canvas_previews.py), with the op's
arguments as one JSON object on STDIN. FUSED_ENV is honored the way the CLI
does it, so every call goes to the environment the caller named.

Ops:

* ``sign``     ``{"path": "s3://bucket/key"}`` -> ``{"url": <https url>}``.
  `fused.api.sign_url`: the plain URL for a public bucket, a presigned GET
  for a private one. This is the one op that runs against today's server.
* ``manifest`` ``{}`` -> ``{"indexes": [...]}``.
* ``resolve``  ``{"index_id": ..., "since": <version or null>}`` ->
  ``{"status": <http status>, "url": <signed link or null>, "version": ...}``.
  The download route answers a redirect to a short-lived signed link; the
  redirect is NOT followed here, the link is handed back.

`manifest` and `resolve` follow the "Bucket index export" plan. Their route
paths and the version header are the plan's, not yet confirmed against the
server code: this is the single place to change when they are.
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

_TIMEOUT = 30
MANIFEST_PATH = "/bucket-index/manifest"
DOWNLOAD_PATH = "/bucket-index/download"
VERSION_HEADER = "X-Bucket-Index-Version"


def _sign(args: dict) -> dict:
    return {"url": fused.api.sign_url(args["path"])}


def _manifest(_args: dict) -> dict:
    api = get_api()
    r = requests.get(f"{api.base_url}{MANIFEST_PATH}", headers=api._generate_headers(),
                     timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {"indexes": data if isinstance(data, list) else data.get("indexes") or []}


def _resolve(args: dict) -> dict:
    api = get_api()
    params = {"index_id": args["index_id"]}
    if args.get("since") is not None:
        params["since"] = args["since"]
    r = requests.get(f"{api.base_url}{DOWNLOAD_PATH}", params=params,
                     headers=api._generate_headers(), timeout=_TIMEOUT,
                     allow_redirects=False)
    version = r.headers.get(VERSION_HEADER)
    return {"status": r.status_code, "url": r.headers.get("Location"),
            "version": int(version) if version and version.isdigit() else None}


def main() -> None:
    ops = {"sign": _sign, "manifest": _manifest, "resolve": _resolve}
    op = ops.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if op is None:
        print("unknown op", file=sys.stderr)
        sys.exit(2)
    try:
        args = json.load(sys.stdin)
    except ValueError:
        args = {}
    json.dump(op(args if isinstance(args, dict) else {}), sys.stdout)


if __name__ == "__main__":
    main()
