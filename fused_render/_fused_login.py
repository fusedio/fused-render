"""`fused workbench login`, but with a humane callback timeout.

The CLI's own login (fused.workbench._auth.authenticate) opens the browser
and then waits at most 30 seconds for Auth0's redirect back to its
localhost callback server — shorter than a real human sign-in, so the child
dies mid-flow and the redirect lands on a dead port. This driver replays the
same PKCE flow with the same helpers (port selection, code challenge, token
exchange, credentials file format all come from the fused package), only the
callback server's timeout differs.

Spawned by canvases.py as ``[sys.executable, <this file>]`` when the fused
package is importable in the server's interpreter (the internal-CLI case);
an external FUSED_RENDER_FUSED_BIN override keeps the CLI's own login.
"""
import secrets
import socket
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# Matches canvases.LOGIN_CHILD_TIMEOUT: the server kills this child at the
# same deadline, so a longer wait here would never be observed.
CALLBACK_TIMEOUT_S = 600


def _pick_callback_port() -> int:
    """First Auth0-whitelisted port (3000-3003) free on BOTH localhost families.

    The CLI's own _find_available_port only bind-probes IPv4, so a server
    listening on IPv6 (e.g. a Node dev server on ``*:3000``) looks free —
    then the browser resolves ``localhost`` to ``::1`` and delivers the
    authorization code to that server instead of the callback. A port is
    usable only if nothing accepts a connection on 127.0.0.1 or ::1.
    """
    for port in range(3000, 3004):
        taken = False
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                probe = socket.socket(family, socket.SOCK_STREAM)
                probe.settimeout(0.25)
                taken = probe.connect_ex((host, port)) == 0
            except OSError:
                pass  # family unavailable on this host — cannot be taken
            finally:
                probe.close()
            if taken:
                break
        if not taken:
            return port
    raise OSError("no free callback port between 3000 and 3003")


def main() -> int:
    try:
        from fused.workbench import _auth  # fused >= 2.9 layout
    except ImportError:  # pragma: no cover - older package layout
        from fused import _auth  # type: ignore[no-redef]

    import requests

    options = _auth.OPTIONS

    code_verifier = secrets.token_urlsafe(48)
    code_challenge = _auth.get_code_challenge(code_verifier)

    port = _pick_callback_port()
    redirect_uri = f"http://localhost:{port}"

    params = {
        "audience": options.auth.audience,
        "scope": " ".join(options.auth.scopes),
        "response_type": "code",
        "client_id": options.auth.client_id,
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    authorize_url = f"{options.auth.authorize_url}?{urlencode(params)}"

    code: str | None = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            nonlocal code
            qs = parse_qs(urlparse(self.path).query)
            if "code" not in qs:
                # Favicon probes etc. — answer and keep waiting.
                self.send_response(404)
                self.end_headers()
                return
            code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"Signed in. You can close this tab and return to fused-render."
            )

        def log_message(self, format, *args):  # noqa: A002
            return

    server = HTTPServer(("localhost", port), Handler)
    # Short per-request timeout inside a long overall deadline: handle_request
    # returns after each served request OR each quiet 5s window, so the loop
    # survives non-callback hits (favicon probes) and still ends on time.
    server.timeout = 5
    deadline = time.time() + CALLBACK_TIMEOUT_S

    print(f"Waiting for browser sign-in on {redirect_uri} ...", flush=True)
    webbrowser.open(authorize_url)
    while code is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if code is None:
        print("Authentication timed out — sign-in was not completed.", file=sys.stderr)
        return 1

    token_data = {
        "client_id": options.auth.client_id,
        "grant_type": "authorization_code",
        "audience": options.auth.audience,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    resp = requests.post(
        options.auth.oauth_token_url, json=token_data, timeout=options.request_timeout
    )
    resp.raise_for_status()
    token = resp.json()
    token["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"] - 1)
    ).isoformat()
    token["auth_scheme"] = "Bearer"
    _auth.save_token_to_disk(token)
    print("Signed in; credentials saved.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
