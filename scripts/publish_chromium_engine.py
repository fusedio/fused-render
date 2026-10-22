#!/usr/bin/env python3
"""Pin a built Chromium engine in fused_render/chromium/manifest.json.

    scripts/publish_chromium_engine.py path/to/FusedChromium-1-arm64.json [more .json]

Each .json is what native/build.sh writes next to its zip ({engine_version,
cef_version, arch, file, sha256, size}). The zip itself must already be
uploaded to the GitHub release the manifest's `base_url` names (the
chromium-engine workflow does both). This writes the sha256 and size into the
manifest's artifact entry for that arch and refuses a file whose engine or
CEF version is not the manifest's — the pin and the upload must be the same
build.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "fused_render", "chromium", "manifest.json")


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    for path in paths:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
        for key in ("engine_version", "cef_version"):
            if str(info.get(key)) != str(manifest[key]):
                print(f"{path}: {key} {info.get(key)!r} != manifest {manifest[key]!r}", file=sys.stderr)
                return 1
        arch = info["arch"]
        entry = manifest["artifacts"].get(arch)
        if entry is None or entry["file"] != info["file"]:
            print(f"{path}: manifest has no artifact {arch}/{info['file']}", file=sys.stderr)
            return 1
        entry["sha256"] = info["sha256"]
        entry["size"] = int(info["size"])
        print(f"pinned {arch}: {info['file']} sha256={info['sha256'][:12]}… size={info['size']}")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
