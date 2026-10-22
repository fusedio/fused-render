"""Print the fused CLI's user access token on stdout, refreshing if expired.

Run as ``[sys.executable, _fused_token.py]`` by canvases.py (same in-interpreter
spawn pattern as _fused_cli.py). This is the `fused login` credential store
(Auth0 PKCE). The refreshed token is saved back so the
on-disk expires_at stays truthful for the cheap staleness check canvases.py
does before spawning this child.

FUSED_ENV is honored the same way the CLI does it (fused._env), like the other
shims: prod/unstable read fused's hosted-tenant store (~/.fused/credentials)
even when ~/.fused/settings.toml names a custom deployment's `[auth]`
credentials_path, and only a run with FUSED_ENV unset (the custom env) reads
that one. Without this the import-time options — the settings file's — pick
the store, and a prod canvas fails with "Credentials file does not exist" as
soon as a settings.toml exists (canvases.credentials_file assumes this reset).
"""
import os

import fused

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

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
