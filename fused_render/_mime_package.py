"""Runtime generation of the freedesktop MIME associations, from the shipping
runtime's own registry (`fused_render.winopen`) — no dependency on `scripts/`.

The Linux self-integration (`supervisor/_linux/integration.py`) needs, at
runtime, the exact same two artifacts the AppImage build stages via
`scripts/file_associations.py`:

  * `desktop_mime_types()` — the ordered, deduped `MimeType=` list for the
    `.desktop` entry: each association's standard shared-mime-info type when one
    exists, else its custom `application/x-fused-render-<token>` glob type, with
    `x-scheme-handler/fused-render` appended for the deep-link handler.
  * `custom_mime_xml()` — the shared-mime-info package XML, defining a glob type
    ONLY for extensions with no standard type (defining a new glob type for an
    extension the shared database already names is the global-type-identity
    hijack the standard-MIME tiering removes).

Both derive from the same winopen data `scripts/file_associations.py` derives
its JSON from, so the packaging-time and runtime artifacts cannot drift; a test
(tests/test_mime_package.py) asserts the two XML/MimeType renderings are
byte-identical.
"""
from __future__ import annotations

_CUSTOM_MIME_PREFIX = "application/x-fused-render-"
_SCHEME_HANDLER = "x-scheme-handler/fused-render"


def _token(extension: str) -> str:
    return extension[1:].lower()


def custom_mime(token: str) -> str:
    return f"{_CUSTOM_MIME_PREFIX}{token}"


def effective_mime(extension: str) -> str:
    """The MIME type to register for an extension: its standard type if one
    exists, else the custom glob type."""
    from fused_render.winopen import standard_mime_for_token

    token = _token(extension)
    return standard_mime_for_token(token) or custom_mime(token)


def desktop_mime_types() -> list[str]:
    """Ordered, deduped `MimeType=` list, with the deep-link scheme appended."""
    from fused_render.winopen import extensions

    seen: dict[str, None] = {}  # dict preserves insertion order while deduping
    for extension in extensions():
        seen.setdefault(effective_mime(extension), None)
    seen.setdefault(_SCHEME_HANDLER, None)
    return list(seen)


def custom_mime_xml() -> str:
    """shared-mime-info package XML for the custom glob types only (extensions
    with no standard type). Byte-identical to scripts/file_associations.py's
    `mime-xml` output for the same association set."""
    from fused_render.winopen import extensions, standard_mime_for_token

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">',
    ]
    for extension in extensions():
        token = _token(extension)
        if standard_mime_for_token(token) is not None:
            continue
        lines.append(f'  <mime-type type="{custom_mime(token)}">')
        lines.append(f"    <comment>{token.upper()} File (FusedRender)</comment>")
        lines.append(f'    <glob pattern="*{extension}"/>')
        lines.append("  </mime-type>")
    lines.append("</mime-info>")
    return "\n".join(lines) + "\n"
