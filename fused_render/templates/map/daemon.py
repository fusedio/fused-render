from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vcog import VirtualCOG  # noqa: E402

POOL = ThreadPoolExecutor(max_workers=os.cpu_count() or 4, thread_name_prefix="vcog")
TOKEN = secrets.token_urlsafe(16)
VERSION = ""
CACHE = ""
CHUNK = 1 << 26


@lru_cache(maxsize=16)
def _source(path: str, stamp: tuple) -> VirtualCOG:
    return VirtualCOG(path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json", headers: dict | None = None):
        self.send_response(status)
        for key, value in {"Content-Type": kind, "Content-Length": str(len(body)), **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if parse_qs(url.query).get("t") != [TOKEN]:
            return self._send(403, b"{}")
        if url.path == "/ping":
            return self._send(200, json.dumps({"ok": True, "version": VERSION}).encode())
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        key = re.fullmatch(r"/cog/([\w-]+)\.(\w+)\.tif", url.path)
        if not key or not match:
            return self._send(404, b"{}")
        try:
            path = base64.urlsafe_b64decode(key[1] + "=" * (-len(key[1]) % 4)).decode()
            secret = open(os.path.join(CACHE, "stream.key"), "rb").read()
            if not hmac.compare_digest(hmac.new(secret, path.encode(), "sha256").hexdigest()[:32], key[2]):
                return self._send(403, b"{}")
            stat = os.stat(path)
            cog = POOL.submit(_source, path, (stat.st_size, stat.st_mtime_ns)).result()
            start = int(match[1])
            end = min(int(match[2] or cog.size - 1) + 1, cog.size, start + CHUNK)
            if start >= cog.size:
                return self._send(416, b"", "image/tiff", {"Content-Range": f"bytes */{cog.size}"})
            body = POOL.submit(cog.read, start, end).result()
        except Exception as error:
            return self._send(500, json.dumps({"error": f"{type(error).__name__}: {error}"}).encode())
        self._send(206, body, "image/tiff", {"Content-Range": f"bytes {start}-{end - 1}/{cog.size}"})


def main():
    global VERSION, CACHE
    parser = argparse.ArgumentParser()
    for name in ("--status", "--cache", "--version"):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    VERSION, CACHE = args.version, args.cache
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(args.status + ".tmp", "w", encoding="utf-8") as handle:
        json.dump({"port": server.server_address[1], "token": TOKEN, "pid": os.getpid(), "version": VERSION}, handle)
    os.replace(args.status + ".tmp", args.status)
    server.serve_forever()


if __name__ == "__main__":
    main()
