"""How a remote (S3) object is reached — the one seam for it.

Scope is one public bucket, `BUCKET` in `REGION`, read with anonymous HTTPS.
Folders come from its index (`remote_index`); this module answers for objects:
`head(bucket, key)` and `read_url(bucket, key)`. A signed reader for other
buckets would replace these two functions here and nowhere else.

**No SSRF.** The only host ever contacted is built from `BUCKET`/`REGION`
(`require_supported` refuses anything else before any network call), and
redirects are never followed.

No import of anything under `fused_render.server` — the router imports this.
"""
from __future__ import annotations

import email.utils
from dataclasses import dataclass
from urllib.parse import quote

import httpx

BUCKET = "fused-asset"
REGION = "us-west-2"

# S3 caps a single key at 1024 bytes of UTF-8.
_KEY_MAX_BYTES = 1024

# A HEAD is tiny: 20 s of silence is a dead network, not a slow one.
TIMEOUT = httpx.Timeout(20.0, connect=8.0)

_client = httpx.Client(timeout=TIMEOUT, follow_redirects=False,
                       headers={"User-Agent": "fused-render"})


class RemoteError(Exception):
    """A failure with a contract code (the router maps code -> status).
    `message` is the plain sentence shown to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ObjectInfo:
    size: int | None
    mtime: float | None
    etag: str | None


# ------------------------------------------------------------------ addresses


def parse_url(s) -> tuple[str, str]:
    """`(bucket, key)` from `s3://bucket/key`, or `RemoteError("bad_url")`.

    Only the canonical spelling: the frontend canonicalises pasted https links
    (`canonicalRemoteUrl`) before calling. `key` is "" for the bucket root and
    keeps a trailing "/" for a prefix. Does not check the bucket is supported —
    that is `require_supported`.
    """
    if not isinstance(s, str) or not s.strip():
        raise RemoteError("bad_url", "no S3 address given")
    s = s.lstrip()  # only before the scheme: a key may end in whitespace
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON body
        raise RemoteError("bad_url", "the S3 address is not valid text") from None
    if not s.lower().startswith("s3://"):
        raise RemoteError("bad_url", "not an S3 address (expected s3://bucket/key)")
    bucket, _, key = s[5:].partition("/")
    if not bucket:
        raise RemoteError("bad_url", "the S3 address has no bucket")
    if "\0" in key:
        raise RemoteError("bad_url", "an S3 key cannot contain a NUL character")
    if len(key.encode("utf-8")) > _KEY_MAX_BYTES:
        raise RemoteError("bad_url", "an S3 key is at most 1024 bytes long")
    # "." / ".." segments are legal key bytes, but an HTTP client or S3 may
    # collapse them in the request path and address a different object.
    if any(seg in (".", "..") for seg in key.split("/")):
        raise RemoteError("bad_url", "an S3 address cannot contain '.' or '..' segments")
    return bucket, key


def canonical_url(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def require_supported(bucket: str) -> str:
    """`bucket`, or `RemoteError("unsupported_bucket")` unless it is `BUCKET`.
    Callers run this before any network call."""
    if bucket != BUCKET:
        raise RemoteError("unsupported_bucket", f"Only the {BUCKET} bucket can be opened for now.")
    return bucket


# ------------------------------------------------------- shared HTTP handling


def clean_etag(value: str | None) -> str | None:
    """An ETag header without its quotes or weak prefix."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"') or None


def error_for_status(status: int, what: str) -> RemoteError:
    """The RemoteError for a non-success HTTP status on `what`."""
    if status == 404:
        return RemoteError("not_found", f"{what} does not exist")
    if status == 403:
        return RemoteError("not_public", f"{what} is not publicly readable")
    return RemoteError("network", f"S3 answered HTTP {status} for {what}")


def _http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


# -------------------------------------------------------------------- objects


def read_url(bucket: str, key: str) -> str:
    """The https URL a plain GET (with Range) downloads the object from."""
    require_supported(bucket)
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{quote(key, safe='/~')}"


def head(bucket: str, key: str) -> ObjectInfo:
    url = read_url(bucket, key)
    what = canonical_url(bucket, key)
    try:
        r = _client.head(url)
    except httpx.HTTPError as e:
        raise RemoteError("network", f"could not reach S3 for {what}: {e}") from None
    if r.status_code != 200:
        raise error_for_status(r.status_code, what)
    size = r.headers.get("content-length")
    return ObjectInfo(size=int(size) if size and size.isdigit() else None,
                      mtime=_http_date(r.headers.get("last-modified")),
                      etag=clean_etag(r.headers.get("etag")))
