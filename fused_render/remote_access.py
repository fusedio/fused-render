"""How a remote (S3) object is reached — the one seam for it.

Scope is one public bucket, `BUCKET` in `REGION`, read with anonymous HTTPS.
Folders come from its index (`remote_index`); this module answers for objects:
`head(bucket, key)`, `read_url(bucket, key)` and `read_range(bucket, key,
start, end)`. A signed reader for other buckets would replace these
functions here and nowhere else.

**No SSRF.** The only host ever contacted is built from `BUCKET`/`REGION`
(`require_supported` refuses anything else before any network call), and
redirects are never followed.

No import of anything under `fused_render.server` — the router imports this.
"""
from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


def http_date(value: str | None) -> float | None:
    """An HTTP date header as an epoch time, or None."""
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


def iso_utc(ts: float | None) -> str | None:
    """An epoch time as ISO-8601 UTC (`2026-04-26T02:05:41Z`); None for an
    unknown time (None or 0, the index's placeholder)."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


# ------------------------------------------------------------------- ranges
#
# A range is a Python slice of the object's bytes: `obj[start:end]`, `end`
# exclusive, None = to the end, a negative value counted from the end. So
# `(-65536, None)` is the last 64 KiB and `(0, 4096)` the first 4 KiB — no
# HTTP Range vocabulary leaks to callers. `max_bytes` caps the window's
# length after it is resolved against the object size.

#: Bytes per chunk handed to the caller.
RANGE_CHUNK = 64 * 1024


def resolve_range(start: int, end: int | None, size: int,
                  max_bytes: int | None = None) -> tuple[int, int]:
    """`(first, stop)` of `obj[start:end]` for an object of `size` bytes,
    stop exclusive and at most `max_bytes` past first. A non-negative
    `start` at or past the end is `range_not_satisfiable` (what S3 answers
    for it); a negative one before the start clamps to 0, like a slice."""
    if start >= 0 and start >= size:
        raise RemoteError("range_not_satisfiable",
                          f"offset {start} is past the end of the object ({size} bytes)")
    window = range(size)[slice(start, end)]
    first = window.start
    stop = max(first, window.stop)
    if max_bytes is not None:
        stop = min(stop, first + max(0, max_bytes))
    return first, stop


class RangeBody:
    """The bytes of one resolved range, as an iterator of chunks.

    `info` is the object, `start`/`end` the window actually served (end
    exclusive). Iterating stops after exactly `end - start` bytes whatever
    the source sends, and raises `RemoteError("network")` if it ends short.
    `close()` releases the source (an HTTP response, a file); it is safe to
    call more than once and from another thread, and must be called even
    when the body is never iterated."""

    def __init__(self, chunks, closer, info: ObjectInfo, start: int, end: int,
                 skip: int = 0, what: str = "the object"):
        self._chunks = chunks
        self._closer = closer
        self._skip = skip
        self._what = what
        self._closed = False
        self.info = info
        self.start = start
        self.end = end

    def __iter__(self):
        skip, left = self._skip, self.end - self.start
        try:
            for chunk in self._chunks:
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk, skip = chunk[skip:], 0
                if len(chunk) >= left:
                    if left:
                        yield chunk[:left]
                    left = 0
                    break
                left -= len(chunk)
                yield chunk
        except (httpx.HTTPError, httpx.StreamError, OSError) as e:
            raise RemoteError("network", f"reading {self._what} failed: {e}") from None
        finally:
            self.close()
        if left:
            raise RemoteError("network", f"{self._what} ended {left} bytes early")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._closer()
        except Exception:  # noqa: BLE001 - releasing must never raise
            pass


def _content_range(value: str | None) -> tuple[int, int, int] | None:
    """`(first, last, total)` of `bytes first-last/total`, else None."""
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", (value or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _http_range(start: int, end: int | None, max_bytes: int | None) -> str:
    """The narrowest `Range` header that covers `obj[start:end]` (capped)."""
    if start < 0:
        # Suffix form: the server resolves it against the size in one trip.
        return f"bytes={start}"
    stop = end if end is not None and end >= 0 else None
    if max_bytes is not None:
        stop = start + max_bytes if stop is None else min(stop, start + max_bytes)
    if stop is None or stop <= start:
        return f"bytes={start}-"
    return f"bytes={start}-{stop - 1}"


def read_range(bucket: str, key: str, start: int, end: int | None = None, *,
               max_bytes: int | None = None) -> tuple[RangeBody, ObjectInfo]:
    """`obj[start:end]` of `s3://bucket/key` (see "ranges" above): one ranged
    GET, never a HEAD — size, etag and the resolved window come from the
    response itself. Returns `(body, info)`; `body.start`/`body.end` is the
    window served. The response is open when this returns (status already
    checked), so a caller can still answer an error before streaming; it
    MUST `body.close()` (iterating to the end does it).

    Errors are `RemoteError` with `head`'s codes, plus
    `range_not_satisfiable` for a start past the end. A server that ignores
    `Range` (200) is honoured only for a window starting at 0; otherwise it
    is a `network` error rather than a download of the whole object."""
    url = read_url(bucket, key)
    what = canonical_url(bucket, key)
    # Raw bytes, never a decoded body: the window is in object bytes.
    headers = {"Range": _http_range(start, end, max_bytes),
               "Accept-Encoding": "identity"}
    try:
        r = _client.send(_client.build_request("GET", url, headers=headers), stream=True)
    except httpx.HTTPError as e:
        raise RemoteError("network", f"could not reach S3 for {what}: {e}") from None
    try:
        if r.status_code == 416:
            raise RemoteError("range_not_satisfiable", _past_end(r, start, what))
        if r.status_code == 206:
            cr = _content_range(r.headers.get("content-range"))
            if cr is None:
                raise RemoteError("network", f"S3 sent an unreadable byte range for {what}")
            served_from, size = cr[0], cr[2]
        elif r.status_code == 200:
            length = r.headers.get("content-length")
            if not (length and length.isdigit()):
                raise RemoteError("network", f"S3 sent {what} without a length")
            served_from, size = 0, int(length)
        else:
            raise error_for_status(r.status_code, what)
        info = ObjectInfo(size=size, mtime=http_date(r.headers.get("last-modified")),
                          etag=clean_etag(r.headers.get("etag")))
        first, stop = resolve_range(start, end, size, max_bytes)
        if first < served_from:
            raise RemoteError("network", f"S3 sent the wrong byte range for {what}")
        if r.status_code == 200 and first > 0:
            raise RemoteError("network", f"S3 ignored the byte range for {what}; "
                                         "refusing to download the whole object")
    except BaseException:
        r.close()
        raise
    body = RangeBody(r.iter_raw(RANGE_CHUNK), r.close, info, first, stop,
                     skip=first - served_from, what=what)
    return body, info


def _past_end(r: httpx.Response, start: int, what: str) -> str:
    """The 416 message, with the object size when S3's error body says it."""
    size = None
    try:
        buf = b""
        for chunk in r.iter_raw(4096):
            buf += chunk
            if len(buf) >= 4096:
                break
        m = re.search(rb"<ActualObjectSize>(\d+)</ActualObjectSize>", buf)
        size = int(m[1]) if m else None
    except httpx.HTTPError:
        pass
    if size is None:
        return f"offset {start} is past the end of {what}"
    return f"offset {start} is past the end of {what} ({size} bytes)"


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
                      mtime=http_date(r.headers.get("last-modified")),
                      etag=clean_etag(r.headers.get("etag")))
