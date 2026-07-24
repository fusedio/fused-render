# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography"]
# ///
"""Emit the signed update manifest (latest.json) the desktop updater polls.

Run via `uv run scripts/windows/generate_update_manifest.py <version>
<installer> <base-url> <output>` from release CI. The ed25519 private key
(base64 raw 32-byte seed) comes from the FUSED_RENDER_UPDATE_SIGNING_KEY
env var; the matching public key is pinned in the client. The signature
covers a domain-separated `version\\nsha256` line so a CDN/bucket compromise
cannot forge a manifest pointing the updater at a different installer.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_SCHEMA = 1
_SIGNING_CONTEXT = "fused-render-update"


def _signing_message(version: str, sha256: str) -> bytes:
    return f"{_SIGNING_CONTEXT}\n{version}\n{sha256}\n".encode("utf-8")


def main() -> None:
    version, installer, base_url, output = sys.argv[1:5]
    key_b64 = os.environ.get("FUSED_RENDER_UPDATE_SIGNING_KEY")
    if not key_b64:
        raise SystemExit("FUSED_RENDER_UPDATE_SIGNING_KEY is not set")

    installer_path = Path(installer)
    sha256 = hashlib.sha256(installer_path.read_bytes()).hexdigest()
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64))
    signature = base64.b64encode(key.sign(_signing_message(version, sha256))).decode()

    manifest = {
        "schema": _SCHEMA,
        "version": version,
        "url": f"{base_url.rstrip('/')}/{installer_path.name}",
        "sha256": sha256,
        "signature": signature,
    }
    Path(output).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
