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

    generate_update_manifest.py newer-than <version> <live-manifest-json>

exits 0 when <version> is at least the version the live manifest names (or
the live text is empty, unreadable, or would not verify — nothing published
yet, or nothing the app would accept), 1 when the live manifest is newer:
release CI uses it so rebuilding an old tag never moves latest.json
backwards.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The verifier the shipped app uses — same pinned key, same context — so
# `newer-than` accepts exactly the manifests the app would and no others.
# The package's __init__ is light enough to import with cryptography alone
# (release CI runs this under `uv run --no-project --with cryptography`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fused_render.update import common  # noqa: E402

_SCHEMA = 1
_SIGNING_CONTEXT = "fused-render-update"


def _signing_message(version: str, sha256: str) -> bytes:
    return f"{_SIGNING_CONTEXT}\n{version}\n{sha256}\n".encode("utf-8")


def not_behind(version: str, live_text: str) -> bool:
    """True unless `live_text` is a VERIFIED manifest naming a version newer
    than `version`. Empty, unparsable, malformed or badly signed live text
    means nothing to protect — a manifest the app itself would reject must
    not be able to stop a real release from publishing (bugbot,
    fusedio/fused-render-lite#26)."""
    try:
        live = json.loads(live_text)
        if not isinstance(live, dict) or live.get("schema") != _SCHEMA or not all(
                isinstance(live.get(k), str) for k in ("version", "url", "sha256", "signature")):
            return True
        # `public_key=` passed explicitly: the default is bound at import
        # time, and tests pin a throwaway key on the module.
        common.verify_signature(live["version"], live["sha256"], live["signature"],
                                public_key=common.PUBLIC_KEY)
        return not common.is_newer(live["version"], version)
    except (ValueError, TypeError):
        return True


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "newer-than":
        raise SystemExit(0 if not_behind(sys.argv[2], sys.argv[3]) else 1)
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
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
