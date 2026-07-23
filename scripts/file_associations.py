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
# "Open with" work for extensions with NO standard shared-mime-info type
# (parquet, pmtiles, …) without guessing — and mis-guessing — a canonical type.
# Extensions that DO have a standard type carry it in `standard_mime` and reuse
# it instead (macOS "Alternate rank" parity — see fused_render.winopen's
# _STANDARD_MIME_FOR_TOKEN), so we never mint a new global type identity for an
# extension the shared database already names.
_MIME_PREFIX = "application/x-fused-render-"

# Deep-link scheme handler, appended to the desktop MimeType= list so the DE
# execs FusedRender for `fused-render://` URLs (Task B).
_SCHEME_HANDLER = "x-scheme-handler/fused-render"


@dataclass(frozen=True)
class Association:
    extension: str  # e.g. ".py"
    icon: str       # icon variant token, e.g. "code"
    standard_mime: str | None = None  # canonical shared-mime-info type, or None

    @property
    def token(self) -> str:
        return self.extension[1:].lower()

    @property
    def mime(self) -> str:
        return f"{_MIME_PREFIX}{self.token}"

    @property
    def effective_mime(self) -> str:
        """The type to register: the standard type when one exists, else the
        custom glob type."""
        return self.standard_mime or self.mime

    @property
    def glob(self) -> str:
        return f"*{self.extension}"

    @property
    def type_name(self) -> str:
        return f"{self.token.upper()} File (FusedRender)"


def associations() -> list[Association]:
    with open(_JSON, encoding="utf-8") as handle:
        data = json.load(handle)
    return [
        Association(
            extension=a["extension"],
            icon=a["icon"],
            standard_mime=a.get("standard_mime"),
        )
        for a in data["associations"]
    ]


def derive_from_winopen() -> list[dict[str, str | None]]:
    """The canonical association list, derived from the shipping runtime's own
    registry, icon map, and standard-MIME table. `regenerate` writes this to the
    JSON; the sync test compares against it."""
    from fused_render.winopen import (
        _ICON_VARIANT_FOR_TOKEN,
        extensions,
        standard_mime_for_token,
    )

    result = []
    for ext in extensions():
        token = ext.rsplit(".", 1)[-1].lower()
        icon = _ICON_VARIANT_FOR_TOKEN.get(token, "file")
        result.append(
            {"extension": ext, "icon": icon, "standard_mime": standard_mime_for_token(token)}
        )
    return result


def mime_types(assocs: list[Association]) -> list[str]:
    """Ordered, deduped MimeType= list for the .desktop: each association's
    effective type, with the deep-link scheme handler appended."""
    seen: dict[str, None] = {}  # dict preserves insertion order while deduping
    for a in assocs:
        seen.setdefault(a.effective_mime, None)
    seen.setdefault(_SCHEME_HANDLER, None)
    return list(seen)


def _mime_xml(assocs: list[Association]) -> str:
    """shared-mime-info package XML — glob types ONLY for associations with no
    standard type. Defining a glob type for an extension that already has a
    standard type is the global-type-identity hijack the tiering removes."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">',
    ]
    for a in assocs:
        if a.standard_mime is not None:
            continue
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
        print(";".join(mime_types(associations())) + ";")
    elif args.command == "mime-xml":
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(_mime_xml(associations()))
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
