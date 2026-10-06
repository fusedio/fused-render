"""Product flavor: which app this package is running as.

One package, two macOS apps. ``render`` is Fused Render (the default,
everything unchanged); ``bot`` is Fused Bot — the same code with the Bots
page as its only front door, its own bundle id, name, icon, home dir, port
base, deep-link scheme and update feed. The flavor is orthogonal to the
branch ref in ``_branch.py``: a bot build off a feature branch still gets the
branch suffix nested inside the bot identity (``io.fused.bot.<ref>``,
``~/.fused-bot/branches/<ref>``).

Resolution priority (cached on first access within a process):
1. ``FUSED_RENDER_FLAVOR`` env var, if set (``render`` or ``bot``).
2. Baked value written at build time (``fused_render/_baked_flavor.py``,
   gitignored, written by scripts/hatch_build.py from the same env var).
3. ``render``.

Stdlib only, no package imports: ``_branch.py`` imports this for the port
base, and ``fused_render/__init__`` calls ``apply_env()`` before anything
else in the package loads, so the ~20 modules that re-derive
``FUSED_RENDER_HOME or ~/.fused-render`` at import time see the bot home.
"""
import os
import sys

RENDER = "render"
BOT = "bot"
_FLAVORS = (RENDER, BOT)

ENV = "FUSED_RENDER_FLAVOR"

# Per-flavor identity. Every branded constant in the package should come from
# here (or be derived from one of these) rather than spelling the brand out.
_IDENTITY = {
    RENDER: {
        "app_name": "FusedRender",          # bundle, binary, DMG, /Applications
        "display_name": "Fused Render",     # window title, copy
        "bundle_id": "io.fused.render",
        "home_dir_name": ".fused-render",   # ~/.fused-render
        "app_support_name": "fused-render",  # ~/Library/Application Support/<name>
        "port_base": 1777,
        "scheme": "fused-render",           # fused-render:// deep links
        "release_prefix": "fused-render",   # S3 keys: <prefix>-dmgs/, <prefix>-macos/
        "cask": "fused-render",             # Homebrew cask, "" = none
        "menubar_icon": "menubar-template.png",
    },
    BOT: {
        "app_name": "FusedBot",
        "display_name": "Fused Bot",
        "bundle_id": "io.fused.bot",
        "home_dir_name": ".fused-bot",
        "app_support_name": "fused-bot",
        "port_base": 2777,                  # FusedBot's historical port
        "scheme": "fused-bot",
        "release_prefix": "fused-bot",
        "cask": "",
        "menubar_icon": "bot/menubar.png",
    },
}


def _baked() -> str:
    try:
        from fused_render import _baked_flavor

        return _baked_flavor._BAKED_FLAVOR
    except ImportError:
        return ""


def _resolve() -> str:
    raw = os.environ.get(ENV)
    if raw is None:
        raw = _baked()
    raw = (raw or "").strip().lower()
    if not raw:
        return RENDER
    if raw not in _FLAVORS:
        raise RuntimeError(f"{ENV} must be one of {_FLAVORS}, got {raw!r}")
    return raw


_CACHED = None


def flavor() -> str:
    global _CACHED
    if _CACHED is None:
        _CACHED = _resolve()
    return _CACHED


def is_bot() -> bool:
    return flavor() == BOT


def _get(key: str) -> str:
    return _IDENTITY[flavor()][key]


def app_name() -> str:
    """``FusedRender`` / ``FusedBot`` — no branch suffix; callers that want it
    append ``_branch.branch_suffix()``."""
    return _get("app_name")


def display_name() -> str:
    return _get("display_name")


def bundle_id() -> str:
    return _get("bundle_id")


def home_dir_name() -> str:
    return _get("home_dir_name")


def default_home_dir() -> str:
    return os.path.expanduser(f"~/{home_dir_name()}")


def app_support_name() -> str:
    return _get("app_support_name")


def app_support_base() -> str:
    return os.path.expanduser(f"~/Library/Application Support/{app_support_name()}")


def port_base() -> int:
    return _IDENTITY[flavor()]["port_base"]


def all_port_bases() -> frozenset[int]:
    """Every flavor's baseline port, whichever flavor is running — the ports a
    branch build of any flavor must never hash onto."""
    return frozenset(v["port_base"] for v in _IDENTITY.values())


def scheme() -> str:
    return _get("scheme")


def release_prefix() -> str:
    return _get("release_prefix")


def cask() -> str:
    return _get("cask")


def menubar_icon() -> str:
    """Path under fused_render/assets/."""
    return _get("menubar_icon")


def apply_env() -> None:
    """Point ``FUSED_RENDER_HOME`` at the flavor's home when the caller has not
    set it. Called from ``fused_render/__init__`` so it runs before any module
    computes a home-rooted constant at import; children inherit it."""
    if flavor() == RENDER:
        return
    os.environ.setdefault("FUSED_RENDER_HOME", default_home_dir())


if __name__ == "__main__":
    field = sys.argv[1]
    fields = {
        "name": flavor,
        "app_name": app_name,
        "display_name": display_name,
        "bundle_id": bundle_id,
        "scheme": scheme,
        "release_prefix": release_prefix,
        "cask": cask,
        "port_base": lambda: str(port_base()),
    }
    if field not in fields:
        raise SystemExit(f"unknown field: {field}")
    print(fields[field]())
