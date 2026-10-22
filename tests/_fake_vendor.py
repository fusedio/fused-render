"""A fake OAuth vendor + hosted MCP server for the connect tests
(SPEC-connect-oauth §11, "Test fake vendor").

One stdlib `http.server` on 127.0.0.1 plays every vendor role the first wave
needs, so a single provider pack (`tests/_fake_provider/fakevendor`) exercises
every auth kind and the MCP client without the network:

    GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728 PRM
    GET  /.well-known/oauth-authorization-server[<issuer_path>]   RFC 8414 (or
         /.well-known/openid-configuration only, with oidc_only=True)
    POST /register      RFC 7591 DCR (public clients)
    GET  /authorize     auto-approve: 302 to redirect_uri with code + state;
                        ?approve=manual renders a page with Allow / Deny links
    POST /token         authorization_code (PKCE S256 checked), refresh_token
                        (rotates the refresh token), device_code (pending for
                        two polls, approved on the third)
    POST /device        RFC 8628 device authorization
    GET  /me            identity: {"login": "fake-user", ...}
    POST /revoke        RFC 7009
    POST /mcp           Streamable HTTP MCP: initialize, notifications/initialized,
                        tools/list (echo [readOnlyHint], write_note [no annotations]),
                        tools/call; JSON, or text/event-stream with ?sse=1

Access tokens live 60 s (`access_ttl`) so refresh is exercised; `expire_all()`
ages every live token at once. `FAKE_CLIENT_ID` is a pre-registered client that
accepts any loopback redirect; `FAKE_API_KEY` is accepted as a Bearer anywhere
a token is (the api-key kind).

Importable (`FakeVendor(port=0).start()` -> `.base_url`, `.stop()`) and runnable
standalone (`python tests/_fake_vendor.py --port N` prints the base URL and
serves until killed). Stdlib only, no fused_render import.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

FAKE_CLIENT_ID = "fake-client"
FAKE_API_KEY = "fake-api-key"
FAKE_LOGIN = "fake-user"
DEVICE_PENDING_POLLS = 2

TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_note", "description": "Write a note.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"},
                                                      "body": {"type": "string"}},
                     "required": ["title"]}},
]


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _is_loopback(uri: str) -> bool:
    u = urlparse(uri)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


class FakeVendor:
    """The fake. Behaviour switches are plain attributes, changeable live."""

    def __init__(self, port: int = 0, *, access_ttl: float = 60, issuer_path: str = "",
                 oidc_only: bool = False, pkce: bool = True, www_authenticate: bool = True,
                 cimd: bool = False, registration: bool = True, device_interval: float = 0.05,
                 device_slow_down: bool = False, device_outcome: str = "approve"):
        self.port = port
        self.access_ttl = access_ttl
        self.issuer_path = issuer_path.rstrip("/")
        self.oidc_only = oidc_only
        self.pkce = pkce
        self.www_authenticate = www_authenticate
        self.cimd = cimd
        self.registration = registration
        self.device_interval = device_interval
        self.device_slow_down = device_slow_down
        #: "approve" | "deny" (access_denied) | "expire" (expired_token)
        self.device_outcome = device_outcome
        self.lock = threading.Lock()
        self.clients: dict[str, dict] = {}      # DCR registrations by client_id
        self.codes: dict[str, dict] = {}        # authorization codes
        self.access: dict[str, dict] = {}       # access token -> {expires_at, client_id, scope}
        self.refresh: dict[str, dict] = {}      # refresh token -> {client_id, scope}
        self.devices: dict[str, dict] = {}      # device_code -> {polls, client_id, scope}
        self.sessions: set[str] = set()         # Mcp-Session-Id values issued
        self.notes: list[dict] = []
        self.revoked: list[str] = []
        self.requests: list[tuple[str, str]] = []   # (method, path) log
        self.token_requests: list[dict] = []        # form bodies POSTed to /token
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def issuer(self) -> str:
        return self.base_url + self.issuer_path

    def start(self) -> FakeVendor:
        vendor = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):  # skip getfqdn
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        class Handler(_Handler):
            fake = vendor

        self._server = Server(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,),
                                        name="fake-vendor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- helpers for tests -------------------------------------------------------

    def expire_all(self) -> None:
        """Age every live access token past its expiry (refresh-path tests)."""
        with self.lock:
            for t in self.access.values():
                t["expires_at"] = time.time() - 1

    def issue(self, client_id: str = FAKE_CLIENT_ID, scope: str = "read write") -> dict:
        """Mint a token pair directly (tests that skip the sign-in)."""
        with self.lock:
            return self._issue(client_id, scope)

    def _issue(self, client_id: str, scope: str) -> dict:
        at, rt = "at-" + secrets.token_hex(12), "rt-" + secrets.token_hex(12)
        self.access[at] = {"expires_at": time.time() + self.access_ttl, "client_id": client_id,
                           "scope": scope}
        self.refresh[rt] = {"client_id": client_id, "scope": scope}
        return {"access_token": at, "refresh_token": rt, "token_type": "Bearer",
                "expires_in": int(self.access_ttl) if float(self.access_ttl).is_integer()
                else self.access_ttl, "scope": scope}

    def token_valid(self, token: str | None) -> bool:
        if not token:
            return False
        if token == FAKE_API_KEY:
            return True
        with self.lock:
            t = self.access.get(token)
            return bool(t) and t["expires_at"] > time.time()

    def known_client(self, client_id: str | None) -> bool:
        if client_id == FAKE_CLIENT_ID:
            return True
        if self.cimd and client_id and client_id.startswith("https://"):
            return True
        with self.lock:
            return client_id in self.clients

    def redirect_ok(self, client_id: str, uri: str) -> bool:
        with self.lock:
            reg = self.clients.get(client_id)
        if reg is not None:
            return uri in reg["redirect_uris"]
        return _is_loopback(uri)


class _Handler(BaseHTTPRequestHandler):
    fake: FakeVendor
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    # -- plumbing -----------------------------------------------------------------

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              headers: dict | None = None) -> None:
        self.send_response(status)
        if body or status not in (202, 204):
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None) -> None:
        self._send(status, json.dumps(obj).encode(), headers=headers)

    def _html(self, status: int, text: str) -> None:
        self._send(status, text.encode(), ctype="text/html; charset=utf-8")

    def _read(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict:
        return {k: v[0] for k, v in parse_qs(self._read().decode(), keep_blank_values=True).items()}

    def _bearer(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def _route(self):
        u = urlparse(self.path)
        self.fake.requests.append((self.command, u.path))
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    # -- GET ----------------------------------------------------------------------

    def do_GET(self):
        path, q = self._route()
        f = self.fake
        base = f.base_url
        if path in ("/.well-known/oauth-protected-resource/mcp",
                    "/.well-known/oauth-protected-resource"):
            return self._json(200, {"resource": base + "/mcp", "authorization_servers": [f.issuer],
                                    "scopes_supported": ["read", "write"],
                                    "bearer_methods_supported": ["header"],
                                    "resource_name": "Fake Vendor MCP"})
        as_path = "/.well-known/oauth-authorization-server" + f.issuer_path
        oidc_path = "/.well-known/openid-configuration" + f.issuer_path
        if (path == as_path and not f.oidc_only) or path == oidc_path:
            meta = {"issuer": f.issuer,
                    "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token",
                    "revocation_endpoint": base + "/revoke",
                    "device_authorization_endpoint": base + "/device",
                    "scopes_supported": ["read", "write"],
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token",
                                              "urn:ietf:params:oauth:grant-type:device_code"],
                    "token_endpoint_auth_methods_supported": ["none"]}
            if f.registration:
                meta["registration_endpoint"] = base + "/register"
            if f.pkce:
                meta["code_challenge_methods_supported"] = ["S256"]
            if f.cimd:
                meta["client_id_metadata_document_supported"] = True
            return self._json(200, meta)
        if path == "/authorize":
            return self._authorize(q)
        if path in ("/authorize/allow", "/authorize/deny"):
            return self._authorize_decide(q, allow=path.endswith("allow"))
        if path == "/me":
            if not f.token_valid(self._bearer()):
                return self._json(401, {"error": "invalid_token"})
            return self._json(200, {"login": FAKE_LOGIN, "name": "Fake User", "id": 1})
        if path == "/device/verify":
            return self._html(200, "<p>Enter the code. (The fake approves by itself.)</p>")
        if path == "/keys":
            return self._html(200, f"<p>Your API key: <code>{FAKE_API_KEY}</code></p>")
        if path == "/mcp":
            return self._json(405, {"error": "use POST"})
        return self._json(404, {"error": "not_found"})

    def _authorize(self, q: dict):
        f = self.fake
        client_id, redirect_uri = q.get("client_id"), q.get("redirect_uri") or ""
        if not f.known_client(client_id):
            return self._html(400, "<p>Unknown client_id.</p>")
        if not f.redirect_ok(client_id, redirect_uri):
            return self._html(400, "<p>redirect_uri is not registered for this client.</p>")
        if q.get("response_type") != "code":
            return self._html(400, "<p>response_type must be code.</p>")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return self._html(400, "<p>PKCE S256 is required.</p>")
        if q.get("approve") == "manual":
            qs = urlencode({k: v for k, v in q.items() if k != "approve"})
            return self._html(200, (
                "<h2>Fake Vendor wants access</h2>"
                f'<p><a id="allow" href="/authorize/allow?{html.escape(qs)}">Allow</a> '
                f'<a id="deny" href="/authorize/deny?{html.escape(qs)}">Deny</a></p>'))
        return self._authorize_decide(q, allow=True)

    def _authorize_decide(self, q: dict, *, allow: bool):
        f = self.fake
        redirect_uri = q.get("redirect_uri") or ""
        if not f.known_client(q.get("client_id")) or not f.redirect_ok(q.get("client_id"), redirect_uri):
            return self._html(400, "<p>Bad request.</p>")
        params = {"state": q.get("state", "")}
        if allow:
            code = "code-" + secrets.token_hex(8)
            with f.lock:
                f.codes[code] = {"client_id": q.get("client_id"), "redirect_uri": redirect_uri,
                                 "challenge": q.get("code_challenge"),
                                 "scope": q.get("scope") or "read write",
                                 "resource": q.get("resource")}
            params["code"] = code
        else:
            params["error"] = "access_denied"
        sep = "&" if urlparse(redirect_uri).query else "?"
        self._send(302, headers={"Location": redirect_uri + sep + urlencode(params)})

    # -- POST ---------------------------------------------------------------------

    def do_POST(self):
        path, q = self._route()
        if path == "/register":
            return self._register()
        if path == "/token":
            return self._token()
        if path == "/device":
            return self._device()
        if path == "/revoke":
            form = self._form()
            tok = form.get("token", "")
            with self.fake.lock:
                self.fake.access.pop(tok, None)
                self.fake.refresh.pop(tok, None)
                self.fake.revoked.append(tok)
            return self._send(200, b"")
        if path == "/mcp":
            return self._mcp(sse=q.get("sse") == "1")
        return self._json(404, {"error": "not_found"})

    def _register(self):
        f = self.fake
        if not f.registration:
            return self._json(404, {"error": "not_found"})
        try:
            body = json.loads(self._read() or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid_client_metadata"})
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            return self._json(400, {"error": "invalid_redirect_uri",
                                    "error_description": "redirect_uris required"})
        if body.get("token_endpoint_auth_method", "client_secret_basic") != "none":
            return self._json(400, {"error": "invalid_client_metadata",
                                    "error_description": "public clients only"})
        client_id = "dcr-" + secrets.token_hex(8)
        reg = {"client_id": client_id, "client_id_issued_at": int(time.time()),
               "client_name": body.get("client_name"), "redirect_uris": uris,
               "grant_types": body.get("grant_types", ["authorization_code"]),
               "response_types": body.get("response_types", ["code"]),
               "token_endpoint_auth_method": "none"}
        with f.lock:
            f.clients[client_id] = reg
        return self._json(201, reg)

    def _token(self):
        f = self.fake
        form = self._form()
        f.token_requests.append(dict(form))
        grant = form.get("grant_type")
        bad = lambda code, desc=None: self._json(
            400, {"error": code, **({"error_description": desc} if desc else {})})
        if grant == "authorization_code":
            with f.lock:
                rec = f.codes.pop(form.get("code", ""), None)
            if rec is None:
                return bad("invalid_grant", "unknown or used code")
            if form.get("client_id") != rec["client_id"]:
                return bad("invalid_grant", "client_id mismatch")
            if form.get("redirect_uri") != rec["redirect_uri"]:
                return bad("invalid_grant", "redirect_uri mismatch")
            verifier = form.get("code_verifier") or ""
            if not verifier or _s256(verifier) != rec["challenge"]:
                return bad("invalid_grant", "PKCE verification failed")
            if rec.get("resource") and form.get("resource") not in (None, rec["resource"]):
                return bad("invalid_target", "resource mismatch")
            with f.lock:
                return self._json(200, f._issue(rec["client_id"], rec["scope"]))
        if grant == "refresh_token":
            with f.lock:
                rec = f.refresh.get(form.get("refresh_token", ""))
                if rec is None or rec["client_id"] != form.get("client_id"):
                    return bad("invalid_grant", "unknown refresh token")
                del f.refresh[form["refresh_token"]]   # rotation: the old one is dead
                return self._json(200, f._issue(rec["client_id"], rec["scope"]))
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            with f.lock:
                rec = f.devices.get(form.get("device_code", ""))
                if rec is None or rec["client_id"] != form.get("client_id"):
                    return bad("invalid_grant", "unknown device code")
                rec["polls"] += 1
                polls = rec["polls"]
                if f.device_slow_down and polls == 1:
                    rec["interval"] = rec["interval"] + 0.05
                    return self._json(400, {"error": "slow_down", "interval": rec["interval"]})
                if polls <= DEVICE_PENDING_POLLS:
                    return bad("authorization_pending")
                if f.device_outcome == "deny":
                    return bad("access_denied")
                if f.device_outcome == "expire":
                    return bad("expired_token")
                del f.devices[form["device_code"]]
                return self._json(200, f._issue(rec["client_id"], rec["scope"]))
        return bad("unsupported_grant_type")

    def _device(self):
        f = self.fake
        form = self._form()
        if not f.known_client(form.get("client_id")):
            return self._json(400, {"error": "invalid_client"})
        dc = "dc-" + secrets.token_hex(8)
        user_code = "FAKE-" + secrets.token_hex(2).upper()
        with f.lock:
            f.devices[dc] = {"polls": 0, "client_id": form["client_id"],
                             "scope": form.get("scope") or "read write",
                             "interval": f.device_interval}
        return self._json(200, {"device_code": dc, "user_code": user_code,
                                "verification_uri": f.base_url + "/device/verify",
                                "verification_uri_complete": f.base_url + "/device/verify?code=" + user_code,
                                "expires_in": 900, "interval": f.device_interval})

    # -- MCP ----------------------------------------------------------------------

    def _mcp(self, *, sse: bool):
        f = self.fake
        body_bytes = self._read()
        if not f.token_valid(self._bearer()):
            headers = {}
            if f.www_authenticate:
                headers["WWW-Authenticate"] = (
                    f'Bearer realm="OAuth", resource_metadata="{f.base_url}'
                    f'/.well-known/oauth-protected-resource/mcp", scope="read write"')
            return self._json(401, {"error": "invalid_token"}, headers=headers)
        try:
            msg = json.loads(body_bytes or b"null")
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "parse error"}})
        if not isinstance(msg, dict):
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32600, "message": "invalid request"}})
        method, mid = msg.get("method"), msg.get("id")
        session = self.headers.get("Mcp-Session-Id")
        extra_headers = {}
        if method == "initialize":
            session = "sess-" + secrets.token_hex(8)
            with f.lock:
                f.sessions.add(session)
            extra_headers["Mcp-Session-Id"] = session
        else:
            if not session:
                return self._json(400, {"jsonrpc": "2.0", "id": mid,
                                        "error": {"code": -32600, "message": "Mcp-Session-Id required"}})
            with f.lock:
                if session not in f.sessions:
                    return self._json(404, {"jsonrpc": "2.0", "id": mid,
                                            "error": {"code": -32001, "message": "session not found"}})
        if mid is None:  # a notification: accepted, no body
            return self._send(202, b"", headers=extra_headers)

        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": requested,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "fakevendor", "version": "1"}}
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        elif method == "tools/list":
            reply = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                result = {"content": [{"type": "text", "text": str(args.get("text", ""))}],
                          "isError": False}
            elif name == "write_note":
                with f.lock:
                    f.notes.append(dict(args))
                result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
            else:
                result = {"content": [{"type": "text", "text": f"unknown tool {name}"}],
                          "isError": True}
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        elif method == "ping":
            reply = {"jsonrpc": "2.0", "id": mid, "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": mid,
                     "error": {"code": -32601, "message": f"method not found: {method}"}}

        if not sse:
            return self._json(200, reply, headers=extra_headers)
        # An unrelated notification first, so a client must read on to the id.
        events = [
            {"jsonrpc": "2.0", "method": "notifications/message",
             "params": {"level": "info", "data": "working"}},
            reply,
        ]
        out = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self._send(200, out, ctype="text/event-stream", headers=extra_headers)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)
    fake = FakeVendor(port=args.port).start()
    print(fake.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
