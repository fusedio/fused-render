"""A remote object as a lazy, sliceable byte buffer — the template stream path.

Readers that take a local file as an mmap or a seekable file can take an
`s3://` address instead (D1484/D1485): `RemoteBuffer` answers `len(buf)`,
`buf[a:b]`, `buf[i]` and `buf.read(start, stop)` by HTTP range requests
against a URL the fused-render server signs (`fused_s3.sign`). So a COG opens
with one request for its header, a parquet page reads one footer and the
column chunks it shows, instead of a download of the whole object.

    buf = RemoteBuffer("s3://fused-asset/x.tif")
    buf[:2], len(buf)                      # b"II", 175009443
    buf.read_many([(a, b), (c, d)])        # coalesced, fetched concurrently

Consumers: geotiff (`_tiff_core`, the tile daemon) and duckdb (`_s3_fs`, an
fsspec filesystem DuckDB reads through). Each loads this file BY PATH under
the module name `LOADED_AS` (the recipe is beside it), so a user's own
`remote_range.py` on sys.path can never stand in for it.

Reads:
  * A small read (at most one block) is served from aligned BLOCK-sized blocks
    kept in an LRU (`max_cache` bytes): headers, footers, neighbouring tiles.
    The first request fetches `HEAD_BYTES`, which holds a COG's whole header.
  * A larger read goes straight to the network and is not kept — consumers
    cache what they decode.
  * `read_many` merges ranges closer than `GAP` and fetches the merged runs on
    up to `PARALLEL` connections: a tile window's chunks of one level are
    usually adjacent in a COG, so a row of tiles is one request.

Consistency: the ETag from the first response is sent as `If-Match` on every
later request, so a changed object fails (`RemoteChanged`) instead of mixing
bytes of two versions. Expiry: the URL is re-signed before `expires_at` and
once after a 403. Redirects are never followed (http.client does not).

Stdlib only (the shared-dir rule); `certifi` is used for TLS roots when
importable unless `SSL_CERT_FILE` names a bundle. HTTPS proxies from the
environment are honoured (CONNECT tunnel). Never imports `fused_render`.
"""
from __future__ import annotations

import http.client
import importlib.util
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

BLOCK = 64 * 1024
HEAD_BYTES = 64 * 1024
GAP = 64 * 1024
PARALLEL = 6
MAX_CACHE = 64 * 1024 * 1024
TIMEOUT_S = 30.0
RETRIES = 2


class RemoteError(OSError):
    """A remote read that failed (network, HTTP status, a short answer)."""


class RemoteChanged(RemoteError):
    """The object changed since this buffer opened it (ETag mismatch)."""


#: The sys.modules name every consumer loads this file under, by path:
#:
#:     mod = sys.modules.get("_fused_remote_range")
#:     if mod is None:
#:         spec = importlib.util.spec_from_file_location(
#:             "_fused_remote_range", "<templates>/shared/remote_range.py")
#:         mod = importlib.util.module_from_spec(spec)
#:         sys.modules["_fused_remote_range"] = mod
#:         spec.loader.exec_module(mod)
LOADED_AS = "_fused_remote_range"


def _load_fused_s3():
    """`fused_s3.py` beside this file, by path: a same-named module earlier on
    sys.path (a user's own) must not shadow it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fused_s3.py")
    spec = importlib.util.spec_from_file_location("_remote_range_fused_s3", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fused_s3 = _load_fused_s3()
is_s3 = fused_s3.is_s3


def _ssl_context() -> ssl.SSLContext:
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = None
_CTX_LOCK = threading.Lock()


def _context() -> ssl.SSLContext:
    global _CTX
    with _CTX_LOCK:
        if _CTX is None:
            _CTX = _ssl_context()
        return _CTX


def _connect(url: str) -> http.client.HTTPSConnection:
    """A connection to `url`'s host, through the environment's HTTPS proxy
    when one applies."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise RemoteError(f"not an https URL: {u.scheme}://{u.hostname}")
    host, port = u.hostname, u.port or 443
    proxy = urllib.request.getproxies().get("https")
    if proxy and not urllib.request.proxy_bypass(host):
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        conn = http.client.HTTPSConnection(p.hostname, p.port or 8080, timeout=TIMEOUT_S,
                                           context=_context())
        headers = {}
        if p.username:
            import base64
            cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        conn.set_tunnel(host, port, headers=headers)
        return conn
    return http.client.HTTPSConnection(host, port, timeout=TIMEOUT_S, context=_context())


_EXECUTOR = None


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _CTX_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=PARALLEL,
                                           thread_name_prefix="remote-read")
        return _EXECUTOR


def _http_date(value):
    import email.utils
    try:
        return email.utils.parsedate_to_datetime(value).timestamp() if value else None
    except (TypeError, ValueError, IndexError):
        return None


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


class RemoteBuffer:
    """See the module docstring. Thread-safe: the tile daemon is threaded."""

    def __init__(self, address: str, *, sign=None, max_cache: int = MAX_CACHE):
        if not is_s3(address):
            raise ValueError(f"not an s3:// address: {address!r}")
        self.address = address
        self._sign = sign or fused_s3.sign
        self._signed = None
        self._sign_lock = threading.Lock()
        self._pool: list = []
        self._pool_lock = threading.Lock()
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._cached_bytes = 0
        self._max_cache = max_cache
        self._lock = threading.Lock()
        self.etag: str | None = None
        self.size: int | None = None
        #: The object's Last-Modified as an epoch time (None if not sent).
        self.mtime: float | None = None
        self.changed = False
        self.requests = 0
        self.bytes_fetched = 0
        # One request learns size + ETag and holds a COG's header.
        head = self._fetch(0, HEAD_BYTES)
        self._store(0, head)

    # ---------------------------------------------------------------- signing

    def _url(self, force: bool = False) -> str:
        with self._sign_lock:
            if force or self._signed is None or fused_s3.expired(self._signed):
                self._signed = self._sign(self.address)
            return self._signed["url"]

    # ------------------------------------------------------------------ HTTP
    #
    # Keep-alive connections in a small pool shared by every thread (a fresh
    # TLS handshake per tile would cost more than the tile).

    def _acquire(self, url: str) -> http.client.HTTPSConnection:
        key = urllib.parse.urlsplit(url).netloc
        with self._pool_lock:
            while self._pool:
                c_key, c = self._pool.pop()
                if c_key == key:
                    return c
                c.close()
        return _connect(url)

    def _release(self, url: str, conn: http.client.HTTPSConnection) -> None:
        key = urllib.parse.urlsplit(url).netloc
        with self._pool_lock:
            if len(self._pool) < PARALLEL:
                self._pool.append((key, conn))
                return
        conn.close()

    def _fetch(self, start: int, stop: int) -> bytes:
        """Bytes `[start, stop)`, clamped to the object: one ranged GET, retried
        on a dropped connection, re-signed once after a 403."""
        if self.size is not None:
            stop = min(stop, self.size)
            if start >= stop:
                return b""
        resigned = False
        attempt = 0
        while True:
            url = self._url()
            u = urllib.parse.urlsplit(url)
            target = (u.path or "/") + ("?" + u.query if u.query else "")
            headers = {"Range": f"bytes={start}-{stop - 1}", "Accept-Encoding": "identity",
                       "User-Agent": "fused-render"}
            if self.etag:
                headers["If-Match"] = f'"{self.etag}"'
            conn = None
            try:
                conn = self._acquire(url)
                conn.request("GET", target, headers=headers)
                r = conn.getresponse()
                status = r.status
                if status in (200, 206):
                    if status == 200 and start > 0:
                        conn.close()
                        raise RemoteError(f"S3 ignored the byte range for {self.address}; "
                                          "refusing to download the whole object")
                    body = r.read()
                else:
                    r.read(64 * 1024)  # drain a small error body so the socket can be reused
                    body = b""
                if r.will_close:
                    conn.close()
                else:
                    self._release(url, conn)
            except (OSError, http.client.HTTPException) as e:
                if isinstance(e, RemoteError):
                    raise
                if conn is not None:
                    conn.close()
                attempt += 1
                if attempt > RETRIES:
                    raise RemoteError(f"reading {self.address} failed: {e}") from None
                time.sleep(0.2 * attempt)
                continue
            self.requests += 1
            if status == 403 and not resigned:
                resigned = True
                self._url(force=True)
                continue
            if status == 412:
                self.changed = True
                raise RemoteChanged(f"{self.address} changed while it was open; reopen it")
            if status == 416 and self.size is None:
                self.size = 0  # an empty object: no byte is satisfiable
                return b""
            if status not in (200, 206):
                raise RemoteError(f"reading {self.address} failed: HTTP {status}")
            etag = (r.getheader("ETag") or "").strip().removeprefix("W/").strip('"') or None
            if self.etag is None:
                self.etag = etag
                self.mtime = _http_date(r.getheader("Last-Modified"))
            elif etag and etag != self.etag:
                self.changed = True
                raise RemoteChanged(f"{self.address} changed while it was open; reopen it")
            if status == 206:
                m = _CONTENT_RANGE.fullmatch((r.getheader("Content-Range") or "").strip())
                if not m or int(m[1]) != start:
                    raise RemoteError(f"S3 sent the wrong byte range for {self.address}")
                if self.size is None and m[3] != "*":
                    self.size = int(m[3])
            elif self.size is None:
                self.size = len(body)  # 200 for a window at 0: the whole object
            want = min(stop, self.size if self.size is not None else stop) - start
            if len(body) < want:
                raise RemoteError(f"{self.address} ended {want - len(body)} bytes early")
            self.bytes_fetched += len(body)
            return body[:want]

    # ----------------------------------------------------------------- blocks

    def _store(self, start: int, data: bytes) -> None:
        """Keep the whole BLOCKs inside `[start, start+len(data))` (and the
        object's final, short block)."""
        end = start + len(data)
        first = -(-start // BLOCK)
        with self._lock:
            b = first
            while b * BLOCK < end:
                lo = b * BLOCK
                hi = min(lo + BLOCK, self.size if self.size is not None else lo + BLOCK)
                if hi > end:
                    break
                if b not in self._blocks:
                    chunk = data[lo - start:hi - start]
                    self._blocks[b] = chunk
                    self._cached_bytes += len(chunk)
                b += 1
            while self._cached_bytes > self._max_cache and self._blocks:
                _, old = self._blocks.popitem(last=False)
                self._cached_bytes -= len(old)

    def _from_blocks(self, start: int, stop: int) -> bytes | None:
        """`[start, stop)` from cached blocks, or None if any is missing."""
        parts = []
        with self._lock:
            for b in range(start // BLOCK, (stop - 1) // BLOCK + 1):
                blk = self._blocks.get(b)
                if blk is None:
                    return None
                self._blocks.move_to_end(b)
                parts.append(blk)
        joined = b"".join(parts)
        off = start - (start // BLOCK) * BLOCK
        return joined[off:off + (stop - start)]

    # ------------------------------------------------------------ public reads

    def __len__(self) -> int:
        return self.size or 0

    def read(self, start: int, stop: int) -> bytes:
        """Bytes `[start, stop)`, clamped to the object like a bytes slice."""
        size = len(self)
        start, stop = max(0, min(start, size)), max(0, min(stop, size))
        if start >= stop:
            return b""
        hit = self._from_blocks(start, stop)
        if hit is not None:
            return hit
        if stop - start <= BLOCK:
            lo = (start // BLOCK) * BLOCK
            hi = min(size, -(-stop // BLOCK) * BLOCK)
            data = self._fetch(lo, hi)
            self._store(lo, data)
            return data[start - lo:stop - lo]
        return self._fetch(start, stop)

    def __getitem__(self, k):
        if isinstance(k, slice):
            if k.step not in (None, 1):
                raise ValueError("RemoteBuffer slices take no step")
            start, stop, _ = k.indices(len(self))
            return self.read(start, stop)
        if isinstance(k, int):
            size = len(self)
            i = k + size if k < 0 else k
            if not 0 <= i < size:
                raise IndexError("RemoteBuffer index out of range")
            return self.read(i, i + 1)[0]
        raise TypeError("RemoteBuffer indices must be integers or slices")

    def read_many(self, ranges) -> list:
        """`[buf[a:b] for a, b in ranges]`, with nearby ranges merged into one
        request and the merged runs fetched concurrently."""
        ranges = [(int(a), int(b)) for a, b in ranges]
        out: list = [None] * len(ranges)
        todo = []
        for i, (a, b) in enumerate(ranges):
            hit = self._from_blocks(a, b) if b > a else b""
            if hit is not None:
                out[i] = hit
            else:
                todo.append(i)
        if not todo:
            return out
        todo.sort(key=lambda i: ranges[i][0])
        runs: list[list] = []
        for i in todo:
            a, b = ranges[i]
            if runs and a <= runs[-1][1] + GAP:
                runs[-1][1] = max(runs[-1][1], b)
                runs[-1][2].append(i)
            else:
                runs.append([a, b, [i]])

        def fetch(run):
            lo, hi, members = run
            data = self._fetch(lo, hi)
            if hi - lo <= 4 * BLOCK:
                self._store(lo, data)
            return [(i, data[ranges[i][0] - lo:ranges[i][1] - lo]) for i in members]

        if len(runs) == 1:
            results = [fetch(runs[0])]
        else:
            results = list(_executor().map(fetch, runs))
        for pairs in results:
            for i, data in pairs:
                out[i] = data
        return out

    def close(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, []
        for _, c in pool:
            c.close()

    def stats(self) -> dict:
        return {"size": self.size, "requests": self.requests,
                "bytes_fetched": self.bytes_fetched, "cached_bytes": self._cached_bytes}
