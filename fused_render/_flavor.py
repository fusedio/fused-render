"""The app's identity: names, bundle id, port, deep-link scheme, update feed.

Every branded constant in the package comes from here (or is derived from one
of these) rather than spelling the brand out, so a rename is one edit.

The package once built two macOS apps from this table (Fused Render and Fused
Bot); only Fused Render is built now, so there is one identity and nothing to
resolve at runtime. The branch ref in ``_branch.py`` still nests inside it
(``io.fused.render.<ref>``).

Stdlib only, no package imports: ``_branch.py`` imports this for the port
base.
"""
import os

_APP_NAME = "FusedRender"          # binary, DMG/PKG/AppImage file names
_DISPLAY_NAME = "Fused Render"     # window title, copy
# The .app folder (/Applications/Fused Render.app) and CFBundleName. Matches
# the display name; the space is what Finder, the Dock and the System Settings
# privacy lists show.
_BUNDLE_NAME = "Fused Render"
# Earlier bundle names. Installs from before the rename still sit at
# /Applications/FusedRender.app until an in-app update moves them
# (update/mac.py `apply_staged`).
_LEGACY_BUNDLE_NAMES = ("FusedRender",)
_BUNDLE_ID = "io.fused.render"
_HOME_DIR_NAME = ".fused-render"   # ~/.fused-render
_APP_SUPPORT_NAME = "fused-render"  # ~/Library/Application Support/<name>
_PORT_BASE = 1777
_SCHEME = "fused-render"           # fused-render:// deep links
_RELEASE_PREFIX = "fused-render"   # S3 keys: <prefix>-dmgs/, <prefix>-macos/
_CASK = "fused-render"             # Homebrew cask
_MENUBAR_ICON = "menubar-template.png"

# Ports a branch build must never hash onto: our own baseline, and 2777, the
# baseline of the retired Fused Bot app, which machines that installed it may
# still be running.
RESERVED_PORTS = frozenset({_PORT_BASE, 2777})


def app_name() -> str:
    """``FusedRender`` — the executable and the release artifacts' file-name
    token, no branch suffix; callers that want it append
    ``_branch.branch_suffix()``. The .app folder is ``bundle_name()``."""
    return _APP_NAME


def display_name() -> str:
    return _DISPLAY_NAME


def bundle_name() -> str:
    """``Fused Render`` — the .app folder's name and the bundle's
    CFBundleName, no branch suffix. Not the executable: that stays
    ``app_name()`` (``Contents/MacOS/FusedRender``), which process probes and
    log predicates match."""
    return _BUNDLE_NAME


def legacy_bundle_names() -> tuple[str, ...]:
    """Names the bundle shipped under before, no branch suffix."""
    return _LEGACY_BUNDLE_NAMES


def bundle_id() -> str:
    return _BUNDLE_ID


def home_dir_name() -> str:
    return _HOME_DIR_NAME


def default_home_dir() -> str:
    return os.path.expanduser(f"~/{home_dir_name()}")


def app_support_name() -> str:
    return _APP_SUPPORT_NAME


def app_support_base() -> str:
    return os.path.expanduser(f"~/Library/Application Support/{app_support_name()}")


def port_base() -> int:
    return _PORT_BASE


def scheme() -> str:
    return _SCHEME


def release_prefix() -> str:
    return _RELEASE_PREFIX


def cask() -> str:
    return _CASK


def menubar_icon() -> str:
    """Path under fused_render/assets/."""
    return _MENUBAR_ICON
