"""Write latest.json for the download page (scripts/download_page/index.html).

Run by .github/workflows/release.yml's publish-download-page job once the
macOS/Windows/Linux artifact jobs finish, with the facts each job produced
passed as environment variables; prints the merged manifest JSON to stdout.
A separate script (not inline in the workflow) so the JSON goes through
json.dumps rather than shell interpolation, and so it's runnable locally.

Only VERSION is required (falling back to an existing manifest's version if
unset). Every artifact field is optional and, when its env var is unset,
carries forward whatever value is already in the manifest at
EXISTING_MANIFEST_PATH (if given) — a workflow_dispatch that rebuilds a
single platform must not blank out the other platforms' still-good links on
the live page.

    VERSION=0.3.2 DMG_URL=... DMG_SHA256=... WHL_URL=... \
        WINDOWS_URL=... LINUX_URL=... \
        python3 scripts/download_page/write_manifest.py
"""
import json
import os
import sys
import time

# (manifest key, source env var) - env var wins when set; otherwise the
# existing manifest's value for that key (if any) is carried forward.
OPTIONAL_FIELDS = [
    ("dmg_url", "DMG_URL"),
    ("dmg_sha256", "DMG_SHA256"),
    ("wheel_url", "WHL_URL"),
    ("windows_url", "WINDOWS_URL"),
    ("linux_url", "LINUX_URL"),
]


def _load_existing(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    existing = _load_existing(os.environ.get("EXISTING_MANIFEST_PATH"))

    version = os.environ.get("VERSION") or existing.get("version")
    if not version:
        print(
            "write_manifest.py: no VERSION given and no existing manifest to fall back to",
            file=sys.stderr,
        )
        return 1

    manifest = {
        "name": "fused-render",
        "version": version,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for key, env_name in OPTIONAL_FIELDS:
        value = os.environ.get(env_name) or existing.get(key)
        if value:
            manifest[key] = value

    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
