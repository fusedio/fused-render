"""Write latest.json for the download page (scripts/download_page/index.html).

Run by .github/workflows/release.yml after the DMG/wheel upload, with the
release facts passed as environment variables; prints the manifest JSON to
stdout. A separate script (not inline in the workflow) so the JSON goes
through json.dumps rather than shell interpolation, and so it's runnable
locally:

    VERSION=0.3.2 DMG_URL=... DMG_SHA256=... WHL_URL=... \
        python3 scripts/download_page/write_manifest.py
"""
import json
import os
import sys
import time


def main() -> int:
    try:
        manifest = {
            "name": "fused-render",
            "version": os.environ["VERSION"],
            "dmg_url": os.environ["DMG_URL"],
            "dmg_sha256": os.environ["DMG_SHA256"],
            "wheel_url": os.environ["WHL_URL"],
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    except KeyError as missing:
        print(f"write_manifest.py: missing env var {missing}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
