"""Print the fused CLI's user access token on stdout, refreshing if expired.

Run as ``[sys.executable, _fused_token.py]`` by canvases.py (same in-interpreter
spawn pattern as _fused_cli.py). This is the `fused login` credential store
(~/.fused/credentials, Auth0 PKCE) — the LEGACY-workbench provider, distinct
from the `fused cloud login` store account.py manages. The refreshed token is
saved back so the on-disk expires_at stays truthful for the cheap staleness
check canvases.py does before spawning this child.
"""
from fused._auth import Credentials


def main() -> None:
    credentials = Credentials.from_disk()
    credentials.refresh_if_needed()
    credentials.save_to_disk()
    # No newline: the parent reads stdout verbatim as the token.
    import sys

    sys.stdout.write(credentials.access_token)


if __name__ == "__main__":
    main()
