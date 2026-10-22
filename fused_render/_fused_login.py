"""`fused login`, but with a humane callback timeout.

The CLI's own login (fused._auth.authenticate) opens the browser
and then waits at most 30 seconds for Auth0's redirect back to its
localhost callback server — shorter than a real human sign-in, so the child
dies mid-flow and the redirect lands on a dead port. This driver replays the
same PKCE flow with the same helpers (port selection, code challenge, token
exchange, credentials file format all come from the fused package), only the
callback server's timeout differs.

Spawned by canvases.py as ``[sys.executable, <this file>]`` when the fused
package is importable in the server's interpreter (the internal-CLI case);
an external FUSED_RENDER_FUSED_BIN override keeps the CLI's own login.

FUSED_ENV is honored the same way the CLI does it (fused._env), like the other
shims: a prod/unstable sign-in goes to fused's hosted tenant and writes
~/.fused/credentials — the store canvases.py watches for completion — even
when ~/.fused/settings.toml names a custom deployment's `[auth]`; only a run
with FUSED_ENV unset (the custom env) signs in against that one.
"""
import os
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# Matches canvases.LOGIN_CHILD_TIMEOUT: the server kills this child at the
# same deadline, so a longer wait here would never be observed.
CALLBACK_TIMEOUT_S = 300


def main() -> int:
    import fused

    env_name = os.environ.get("FUSED_ENV")
    if env_name:
        fused._env(env_name)

    from fused import _auth

    import requests

    options = _auth.OPTIONS

    code_verifier = secrets.token_urlsafe(48)
    code_challenge = _auth.get_code_challenge(code_verifier)

    port = _auth._find_available_port(3000)
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
    denied: str | None = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            nonlocal code, denied
            qs = parse_qs(urlparse(self.path).query)
            if "error" in qs:
                # Auth0 redirects a refused sign-in (Decline on the consent
                # screen, an account the tenant rejects) back here with
                # `error=...` and NO code. That is the flow's final word, not
                # a stray probe: treating it like one left this child — and
                # the page polling it — waiting out the whole deadline.
                denied = qs.get("error_description", qs["error"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"Not signed in. You can close this tab and try again from fused-render."
                )
                return
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
    while code is None and denied is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if denied is not None:
        print(f"Authentication refused — {denied}", file=sys.stderr)
        return 1
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
