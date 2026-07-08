#!/usr/bin/env python3
"""Stamp per-branch identity into the files that must be COMMITTED for the
git-URL plugin/marketplace/skill install path (SPEC per-branch-isolation).

Runtime code (fused_render/_branch.py) and the DMG/app build (build_dmg.sh,
setup_py2app.py) resolve the branch ref live and never need this script.
This script exists solely for the handful of values baked into committed
JSON/markdown that a git-URL install reads as-is: `.claude-plugin/
marketplace.json`, `.claude-plugin/plugin.json`, and the `:8765` port
mentioned in the SKILL.md docs.

Usage:
    python scripts/branch_stamp.py [--ref REF] [--reset]

With no args, stamps using the currently-resolved branch ref (env var / git
branch, see fused_render._branch). `--reset` (or `--ref ""`) restores the
baseline names/port exactly.
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fused_render._branch import branch_port, branch_ref  # noqa: E402

BASE_PLUGIN_NAME = "fused-render"
BASE_DISPLAY_NAME = "Fused Render"
BASE_PORT = 8765

# Only the two contexts actually used in the SKILL.md docs to show the
# default server port: `http://127.0.0.1:<port>` and
# `--port <port> --no-browser`. Deliberately does NOT match unrelated
# example ports (e.g. `--start-dir ~/data --port 9000`, which has no
# trailing `--no-browser`), so those stay untouched.
_HOST_PORT_PATTERN = re.compile(r"(127\.0\.0\.1:)\d+")
_FLAG_PORT_PATTERN = re.compile(r"(--port )\d+( --no-browser)")


def _plugin_name(ref: str) -> str:
    return f"{BASE_PLUGIN_NAME}-{ref}" if ref else BASE_PLUGIN_NAME


def _display_name(ref: str) -> str:
    return f"{BASE_DISPLAY_NAME} ({ref})" if ref else BASE_DISPLAY_NAME


def stamp_marketplace(marketplace_path: Path, ref: str) -> None:
    with open(marketplace_path) as f:
        data = json.load(f)
    data["name"] = _plugin_name(ref)
    data["plugins"][0]["name"] = _plugin_name(ref)
    with open(marketplace_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def stamp_plugin(plugin_path: Path, ref: str) -> None:
    with open(plugin_path) as f:
        data = json.load(f)
    data["name"] = _plugin_name(ref)
    data["displayName"] = _display_name(ref)
    with open(plugin_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def stamp_skill_docs(skill_files: list[Path], ref: str) -> None:
    port = str(branch_port(ref) if ref else BASE_PORT)
    for path in skill_files:
        text = path.read_text()
        new_text = _HOST_PORT_PATTERN.sub(rf"\g<1>{port}", text)
        new_text = _FLAG_PORT_PATTERN.sub(rf"\g<1>{port}\g<2>", new_text)
        if new_text != text:
            path.write_text(new_text)


def stamp(
    ref: str,
    marketplace_path: Path,
    plugin_path: Path,
    skill_files: list[Path],
) -> None:
    stamp_marketplace(marketplace_path, ref)
    stamp_plugin(plugin_path, ref)
    stamp_skill_docs(skill_files, ref)


def _default_skill_files() -> list[Path]:
    return [
        REPO_ROOT / "skills" / "fused-render-usage" / "SKILL.md",
        REPO_ROOT / "skills" / "fused-render-authoring" / "SKILL.md",
        REPO_ROOT / "skills" / "fused-render-custom-templates" / "SKILL.md",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=None, help="branch ref to stamp (default: resolved via fused_render._branch)")
    parser.add_argument("--reset", action="store_true", help="restore baseline names/port (ref '')")
    args = parser.parse_args()

    if args.reset:
        ref = ""
    elif args.ref is not None:
        ref = branch_ref(args.ref)
    else:
        ref = branch_ref()

    marketplace_path = REPO_ROOT / ".claude-plugin" / "marketplace.json"
    plugin_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
    skill_files = _default_skill_files()

    stamp(ref, marketplace_path, plugin_path, skill_files)

    port = branch_port(ref) if ref else BASE_PORT
    label = ref or "baseline"
    print(f"==> stamped plugin identity for ref={label!r} (name={_plugin_name(ref)}, port={port})")
    print("==> commit and push so the git-URL install path picks this up: git add -A && git commit && git push")


if __name__ == "__main__":
    main()
