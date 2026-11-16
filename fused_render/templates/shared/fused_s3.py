"""A Python client for `fused.s3` (runtime.js): a read URL for an `s3://` object.

The streaming path for templates and user `.py` files. Rather than waiting for
the explorer to download an object, a reader asks the running server for a
URL and reads the object itself with plain HTTPS range requests — DuckDB's
`read_parquet('https://…')`, GDAL's `/vsicurl/`, a range-reading decoder:

    import fused_s3
    if fused_s3.is_s3(file):
        signed = fused_s3.sign(file)          # {"url": "https://…", "expires_at": None}
        con.execute("SELECT * FROM read_parquet(?)", [signed["url"]])

The server (`POST /api/remote/sign`, `remote_access.sign_read_url`) decides
what may be signed and, once private buckets arrive, holds the credentials;
this module only ever sees a URL for the one object it named. `expires_at` is
an ISO-8601 UTC string, or None when the URL does not expire (the public
bucket, today) — a long-lived reader checks `expired()` and signs again, and
re-signs once on a 403.

Pass the `s3://` address between processes (a `runPython` param, a daemon
request), never the signed URL: `runPython` params are kept in the call log,
which goes into diagnostics bundles. Key caches by the address too — the URL
may differ on every call.

**Travels with `appenv.py`.** Stdlib only and never `import fused_render`
(SPEC PY-15; the `adding-a-shared-template-utility` skill). The server's
address comes from `appenv.origin()` (`FUSED_RENDER_ORIGIN`, exported to every
process the server starts), with `appenv.py` loaded by PATH so a user's own
same-named module cannot shadow it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime


def _load_sibling_appenv():
    """THIS file's own `appenv.py`, by path: the shared dir is APPENDED to a
    user module's `sys.path`, so a bare `import appenv` can resolve a user's
    same-named module instead."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_fused_s3_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


appenv = _load_sibling_appenv()

# Signing is local and makes no network call; this bounds "the server stopped
# answering".
_TIMEOUT_S = 30.0


class S3Error(RuntimeError):
    """A sign request the server refused or could not answer. `code` is the
    route's code (disabled | bad_url | unsupported_bucket | forbidden |
    unreachable | …) so a caller can fall back — e.g. to the download path
    when the integration is off."""

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def is_s3(path) -> bool:
    """Whether `path` is an `s3://` address rather than a local path."""
    return isinstance(path, str) and path[:5].lower() == "s3://"


def _origin() -> str:
    origin = appenv.origin()
    if not origin:
        raise S3Error(
            "fused_s3: no Fused server to ask — FUSED_RENDER_ORIGIN is not set. "
            "Run this from a page or task the Fused app started, or set "
            "FUSED_RENDER_ORIGIN to the app's address (http://127.0.0.1:<port>).",
            code="unreachable")
    return origin.rstrip("/")


def _error_body(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def sign(url: str) -> dict:
    """`{"url": <https URL>, "expires_at": <ISO-8601 UTC> | None}` for the
    object at `url` (`s3://bucket/key`). Raises `S3Error`."""
    origin = _origin()
    req = urllib.request.Request(
        origin + "/api/remote/sign",
        data=json.dumps({"url": url}).encode("utf-8"),
        # Not authentication (D3): the marker every POST on this server needs.
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "X-Fused": "1"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        body = _error_body(e.read() or b"")
        said = body.get("error") if isinstance(body.get("error"), str) else f"HTTP {e.code}"
        code = body.get("code") if isinstance(body.get("code"), str) else None
        raise S3Error(f"fused_s3: could not sign {url}: {said}", code=code, status=e.code) from None
    except urllib.error.URLError as e:
        raise S3Error(f"fused_s3: could not reach the Fused server at {origin}: {e.reason}",
                      code="unreachable") from None
    except (ValueError, UnicodeDecodeError):
        raise S3Error("fused_s3: the Fused server sent an unreadable answer",
                      code="internal") from None
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        raise S3Error("fused_s3: the Fused server sent an unreadable answer", code="internal")
    expires = data.get("expires_at")
    return {"url": data["url"], "expires_at": expires if isinstance(expires, str) else None}


def expired(signed: dict, margin_s: float = 60.0) -> bool:
    """Whether `signed` (from `sign`) expires within `margin_s` seconds — sign
    again before the next read. A URL with no expiry never expires; an
    unreadable time counts as expired."""
    expires = signed.get("expires_at") if isinstance(signed, dict) else None
    if not expires:
        return False
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return True
    return when - time.time() <= margin_s
