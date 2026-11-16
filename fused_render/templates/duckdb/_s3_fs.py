"""DuckDB reads an `s3://` parquet through this fsspec filesystem (D1486).

`reader.py` gets `_file=s3://bucket/key` when the explorer streams a bucket
object (the `stream-s3` marker beside it). DuckDB cannot read that itself
without its httpfs extension, which is downloaded at runtime from
extensions.duckdb.org (unreliable behind proxies, and nothing else here needs
it). Instead `register(con)` hands DuckDB a Python filesystem whose files are
`shared/remote_range.RemoteBuffer`s: HTTP range reads of a URL the server
signs (`fused_s3`). DuckDB's parquet reader then does what it does on a local
file — the footer, then only the row groups and columns a query touches.

    fs = register(con)
    con.execute(f"SELECT * FROM read_parquet('{duckdb_path(address)}') LIMIT 5")

fsspec is always importable here: `fused`, a core dependency of fused-render,
requires it, and this template runs in the app's own interpreter.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import threading

from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

PROTOCOL = "fusedrange"


def remote_range():
    """`templates/shared/remote_range.py`, loaded by path once per process
    under its `LOADED_AS` name (never via sys.path — a user's same-named
    module must not stand in for it)."""
    name = "_fused_remote_range"
    mod = sys.modules.get(name)
    if mod is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "shared", "remote_range.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return mod


def is_s3(path) -> bool:
    return isinstance(path, str) and path[:5].lower() == "s3://"


def duckdb_path(address: str) -> str:
    """`s3://bucket/key` as the path DuckDB opens through `register`."""
    return f"{PROTOCOL}://{address[5:]}"


def is_streamed(path) -> bool:
    """`path` is a `duckdb_path` (what reader.py passes around once a streamed
    file has been translated)."""
    return isinstance(path, str) and path.startswith(PROTOCOL + "://")


class _File(AbstractBufferedFile):
    def _fetch_range(self, start, end):
        return self.fs.buffer(self.path).read(start, end)


class RemoteFS(AbstractFileSystem):
    """Read-only, one object per path; no listing (DuckDB only globs a path
    that has glob characters, and a key's literal `*` or `[` must not expand)."""

    protocol = PROTOCOL
    cachable = False  # one instance per connection, each with its own buffers

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._bufs = {}
        self._lock = threading.Lock()

    def buffer(self, path):
        key = self._strip_protocol(path)
        with self._lock:
            buf = self._bufs.get(key)
            if buf is None or buf.changed:
                buf = self._bufs[key] = remote_range().RemoteBuffer("s3://" + key)
            return buf

    def info(self, path, **kwargs):
        return {"name": self._strip_protocol(path), "size": len(self.buffer(path)),
                "type": "file"}

    def ls(self, path, detail=True, **kwargs):
        entry = self.info(path)
        return [entry] if detail else [entry["name"]]

    def glob(self, path, **kwargs):
        return [self._strip_protocol(path)]

    def exists(self, path, **kwargs):
        try:
            self.buffer(path)
            return True
        except OSError:
            return False

    def modified(self, path):
        mtime = self.buffer(path).mtime or 0.0
        return datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)

    def _open(self, path, mode="rb", block_size=None, autocommit=True,
              cache_options=None, **kwargs):
        if mode != "rb":
            raise PermissionError("a streamed S3 object is read-only")
        # RemoteBuffer already caches small reads in blocks; a second cache
        # here would only double the memory.
        return _File(self, self._strip_protocol(path), mode,
                     block_size=block_size or 1 << 20, cache_type="none",
                     size=len(self.buffer(path)))

    def stats(self) -> dict:
        """Requests and bytes over every object this connection read."""
        out = {"requests": 0, "bytes_fetched": 0}
        for buf in list(self._bufs.values()):
            out["requests"] += buf.requests
            out["bytes_fetched"] += buf.bytes_fetched
        return out


def register(con) -> RemoteFS:
    """A fresh `RemoteFS` registered on DuckDB connection `con`."""
    fs = RemoteFS()
    con.register_filesystem(fs)
    return fs
