"""Shared helpers for the appearance/theme source-contract tests (test_theme.py).

Source-level parsing only: reads repo files and pulls the two palette blocks
(`:root` and `:root[data-theme="light"]`) plus any colour literal that escaped
them out of a stylesheet. Deliberately naive about CSS in general — it only
needs to be exact about the shapes this project actually writes.

TEMPLATES_DIR below is the *repo* tree on purpose: these are authoring
invariants, and they should fail on the file a developer edits. It is NOT what
the server serves — built-in templates reach a browser via the staged copy at
~/.fused-render/.core-templates (fused_render/core_templates.py). Nothing here
can observe a stale stage; tests/test_core_templates.py does that.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "fused_render", "templates")

# The single localStorage key holding the user's System/Light/Dark choice.
THEME_KEY = "fused-render:theme"

# The <html> attribute a built-in template sets to ask the injected runtime to
# keep its `data-theme` in sync with the shell. Absent = no theme signal, which
# is what every user-authored .html view gets.
OPT_IN_ATTR = "data-fused-theme"

# Real light palettes authored.
TIER_ONE_TEMPLATES = (
    "annotate",
    "api",
    "app",
    "bundle",
    "claude",
    "code",
    "duckdb",
    "git",
    "graph",
    "history",
    "log_studio",
    "markdown",
    "notebook",
    "plist",
    "sqlite",
    "structure",
    "text",
    "tree",
    "vector",
    "versions",
    "xlsx",
    "zip",
)

# Light by design: these views are light in both modes and ignore the setting.
EXEMPT_TEMPLATES = (
    "docs",
    "latex",
    "map",
    "pano",
    "pdf_studio",
    "pyramid",
    "slides",
    "usd",
)

# Views that ship their own in-view theme toggle and private storage key;
# explicitly not migrated and not overridden. (log_studio was here until its
# toggle was removed — it now uses the shared opt-in like any tier-1 view.)
SELF_TOGGLING_TEMPLATES = ("excel", "tableau")

# ---------------------------------------------------------------- shared assets
# `templates/shared/*.js` is loaded BY templates with a plain
# <script src="/template-shared/…">, so any CSS such a file injects lands in the
# host template's themed document and is bound by exactly the same rule as the
# template's own <style>. Scanning only `template.html` missed that whole layer,
# which is how folder-picker.js shipped a hardcoded dark palette.
#
# Tokenized: no colour literal outside a palette block. (graph-canvas.js counts
# — the colours it paints with are passed in by the host template; the one hex in
# it is a last-resort canvas fallback, not a stylesheet rule.)
#
# Unmigrated: literals still present, listed so the omission is on the record
# rather than an oversight. ro-badge.js is a tooltip shared by DEFERRED templates
# too, so retheming it belongs with that pass; sciviz.mjs injects no CSS at all —
# its one hit is a canvas `fillStyle` for a hover highlight, which the (naive by
# design) parser cannot tell from a rule body.
TOKENIZED_SHARED_ASSETS = ("folder-picker.js", "graph-canvas.js")
UNMIGRATED_SHARED_ASSETS = ("ro-badge.js", "sciviz.mjs")

# The selectors folder-picker.js uses for its OWN dark/light palette pair. It
# cannot use `:root` — a shared asset injecting `:root` rules would redefine the
# host template's palette — so it scopes the pair to its root element, and this
# is where the test learns that those two blocks are palette blocks.
SHARED_PALETTE_SELECTORS = (".fp-backdrop", ':root[data-theme="light"] .fp-backdrop')


# Media / geospatial / studio groups — keep today's appearance for now.
DEFERRED_TEMPLATES = (
    "canvas",
    "geometry_editor",
    "geotiff",
    "glb",
    "h3",
    "image",
    "las",
    "media",
    "netcdf",
    "pdf",
    "photos",
    "pmtiles",
    "reader",
    "tar",
    "zarr_aoi",
)


def all_template_names():
    """Every built-in template folder (a dir holding a template.html)."""
    return sorted(
        name
        for name in os.listdir(TEMPLATES_DIR)
        if os.path.isfile(os.path.join(TEMPLATES_DIR, name, "template.html"))
    )


def all_shared_asset_names():
    """Every script under `templates/shared/` a template can <script src> in."""
    shared = os.path.join(TEMPLATES_DIR, "shared")
    return sorted(
        name for name in os.listdir(shared) if name.endswith((".js", ".mjs"))
    )


def read_repo_file(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


def read_template(name):
    return read_repo_file(os.path.join("fused_render", "templates", name, "template.html"))


def read_shared_asset(name):
    return read_repo_file(os.path.join("fused_render", "templates", "shared", name))


# `:root { … }` and `:root[data-theme="light"] { … }`, quotes optional.
_DARK_ROOT = re.compile(r"(?<![\w\-\]])\:root\s*\{([^{}]*)\}")
_LIGHT_ROOT = re.compile(r":root\[data-theme=[\"']?light[\"']?\]\s*\{([^{}]*)\}")

_DECL = re.compile(r"(--[\w-]+|color-scheme)\s*:\s*([^;]+)")
_COMMENT = re.compile(r"/\*[\s\S]*?\*/")


def _declarations(block):
    # Comments first: a token NAME mentioned in prose ("above --bg-alt: the
    # tooltip …") otherwise reads as a declaration whose value runs to the next
    # semicolon, swallowing the real declaration that follows it.
    return {name: value.strip() for name, value in _DECL.findall(_COMMENT.sub("", block))}


def palette_blocks(source):
    """The dark and light palette declaration maps of a stylesheet/template.

    Returns ``(dark, light)``; either is ``{}`` when that block is absent.
    """
    dark_match = _DARK_ROOT.search(source)
    light_match = _LIGHT_ROOT.search(source)
    return (
        _declarations(dark_match.group(1)) if dark_match else {},
        _declarations(light_match.group(1)) if light_match else {},
    )


def _selector_block(selector):
    """`<selector> { … }` — the rule for one exact selector list.

    The leading boundary keeps a *suffix* match from counting: without it the
    pattern for `.fp-backdrop` also matches the tail of
    `:root[data-theme="light"] .fp-backdrop`, i.e. one selector would silently
    consume the other's block.
    """
    return re.compile(
        r"(?:^|[\n;}\"'])\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}"
    )


def scoped_palette_blocks(source, dark_selector, light_selector):
    """`palette_blocks` for a pair of palettes scoped to a selector rather than
    to `:root` — what a shared asset must do, since injecting `:root` rules from
    a <script> would redefine the host template's own palette."""
    dark = _selector_block(dark_selector).search(source)
    light = _selector_block(light_selector).search(source)
    return (
        _declarations(dark.group(1)) if dark else {},
        _declarations(light.group(1)) if light else {},
    )


_STYLE_BLOCK = re.compile(r"<style[^>]*>([\s\S]*?)</style>", re.I)
# Only declaration bodies — an `#id` selector is not a colour, and neither is
# anything in a comment.
_RULE_BODY = re.compile(r"\{([^{}]*)\}")
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_FUNC = re.compile(r"\brgba?\(([^()]*(?:\([^()]*\)[^()]*)*)\)")


def style_color_literals(source, palette_selectors=()):
    """Colour literals in a stylesheet that are NOT inside a palette block.

    A `rgb()`/`rgba()` call whose arguments are built from `var(...)` is a
    tokenized colour, not a literal, so it does not count. Anything else — a
    bare hex, a bare `rgba(0, 0, 0, .5)` — does: it cannot follow the theme.

    `palette_selectors` names additional rules to treat as palette blocks, for a
    stylesheet whose palette cannot live on `:root` (a shared asset — see
    `scoped_palette_blocks`).
    """
    sheets = _STYLE_BLOCK.findall(source) or [source]
    found = []
    for sheet in sheets:
        # Blank out the palette blocks: those are exactly where literals belong.
        sheet = _DARK_ROOT.sub("", _LIGHT_ROOT.sub("", sheet))
        for selector in palette_selectors:
            sheet = _selector_block(selector).sub("", sheet)
        sheet = _COMMENT.sub("", sheet)
        for body in _RULE_BODY.findall(sheet):
            found.extend(_HEX.findall(body))
            found.extend(
                "rgba(" + args + ")" for args in _FUNC.findall(body) if "var(" not in args
            )
    return found
