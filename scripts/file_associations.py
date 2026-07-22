"""Shared file-association table for BOTH desktop packaging pipelines.

The Windows installer registry generator (scripts/windows/generate_installer_
registry.py) and the Linux AppImage build (scripts/build_linux_appimage.sh, via
this module's `mime-types` / `mime-xml` commands) must agree on the exact set of
file extensions FusedRender registers as an "Open with" handler, and on each
extension's icon variant. That single source of truth is
`scripts/file_associations.json`; neither pipeline owns its own copy.

The JSON is *derived* from the shipping runtime's own association data
(`fused_render.winopen`) so a template/registry change flows to both packagers.
`regenerate` rewrites the JSON from winopen; a test
(tests/test_file_associations.py) asserts the committed JSON stays in sync, so
drift fails CI rather than shipping a stale association set.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_JSON = os.path.join(_HERE, "file_associations.json")

# Custom freedesktop MIME family. Using one glob-based type per extension makes
# "Open with" work for extensions with no standard shared-mime-info type
# (parquet, geojson, …) without guessing — and mis-guessing — a canonical type.
_MIME_PREFIX = "application/x-fused-render-"


@dataclass(frozen=True)
class Association:
    extension: str  # e.g. ".py"
    icon: str       # icon variant token, e.g. "code"

    @property
    def token(self) -> str:
        return self.extension[1:].lower()

    @property
    def mime(self) -> str:
        return f"{_MIME_PREFIX}{self.token}"

    @property
    def glob(self) -> str:
        return f"*{self.extension}"

    @property
    def type_name(self) -> str:
        return f"{self.token.upper()} File (FusedRender)"


def associations() -> list[Association]:
    with open(_JSON, encoding="utf-8") as handle:
        data = json.load(handle)
    return [Association(extension=a["extension"], icon=a["icon"]) for a in data["associations"]]


def derive_from_winopen() -> list[dict[str, str]]:
    """The canonical association list, derived from the shipping runtime's own
    registry + icon map. `regenerate` writes this to the JSON; the sync test
    compares against it."""
    from fused_render.winopen import _ICON_VARIANT_FOR_TOKEN, extensions

    result = []
    for ext in extensions():
        token = ext.rsplit(".", 1)[-1].lower()
        icon = _ICON_VARIANT_FOR_TOKEN.get(token, "file")
        result.append({"extension": ext, "icon": icon})
    return result


def _mime_xml(assocs: list[Association]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">',
    ]
    for a in assocs:
        lines.append(f'  <mime-type type="{a.mime}">')
        lines.append(f"    <comment>{a.type_name}</comment>")
        lines.append(f'    <glob pattern="{a.glob}"/>')
        lines.append("  </mime-type>")
    lines.append("</mime-info>")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("regenerate", help="rewrite file_associations.json from winopen")
    sub.add_parser("mime-types", help="print the ;-joined MIME type list (for a .desktop MimeType=)")
    xml = sub.add_parser("mime-xml", help="write the freedesktop MIME package XML")
    xml.add_argument("output")
    args = parser.parse_args()

    if args.command == "regenerate":
        with open(_JSON, "w", encoding="utf-8") as handle:
            json.dump({"associations": derive_from_winopen()}, handle, indent=2)
            handle.write("\n")
        print(f"wrote {_JSON}")
    elif args.command == "mime-types":
        # Trailing ';' is required by the desktop-entry spec for MimeType lists.
        print(";".join(a.mime for a in associations()) + ";")
    elif args.command == "mime-xml":
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(_mime_xml(associations()))
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
