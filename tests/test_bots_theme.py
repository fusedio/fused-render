"""The appearance theme reaches every rendered page (docs/bots.md §5).

The shell's pre-paint bootstrap (frontend/bots.html, frontend/lite.html) and
the runtime injected into every /render and /embed page (static/runtime.js)
must agree on the localStorage key and the attribute, or a built app that
opts in with `data-fused-theme` stays dark while the shell is light — the
gap a bot's own build reported live ("the runtime ignores the theme
attribute"). These pins keep the spellings together.
"""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import os
import re

import pytest

pytestmark = pytest.mark.skip(
    reason="pins FusedBot's frontend/bots.html + lite.html bootstraps and its runtime.js; "
           "neither is ported (the Bots page is a route in fused-render's own shell)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "fused_render_app", "static", "runtime.js")
BOOTSTRAPS = [os.path.join(ROOT, "frontend", "bots.html"), os.path.join(ROOT, "frontend", "lite.html")]

THEME_KEY = "fused-render:theme"


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_runtime_and_shell_bootstraps_share_the_theme_key():
    rt = _read(RUNTIME)
    assert f'var THEME_KEY = "{THEME_KEY}";' in rt
    for p in BOOTSTRAPS:
        assert f'localStorage.getItem("{THEME_KEY}")' in _read(p), p


def test_runtime_theme_block_runs_before_everything_else():
    """Parser-blocking at the top of the IIFE: the attribute lands before the
    page's own stylesheet, so there is no flash and no late flip."""
    rt = _read(RUNTIME)
    iife = rt.index('(function () {\n  "use strict";')
    theme = rt.index("// --- Appearance (SPEC §30, D134)")
    call = rt.index("startTheme();")
    assert iife < theme < call
    # nothing of the runtime's own API is installed before the theme has been applied
    # (the header comment above the IIFE names `window.fused` too; look inside it)
    assert call < rt.index("window.fused", iife)


def test_runtime_theme_contract():
    rt = _read(RUNTIME)
    # opt-in pages get data-theme; every page gets color-scheme
    assert 'root.hasAttribute("data-fused-theme")' in rt
    assert 'root.setAttribute("data-theme", theme)' in rt
    assert "root.style.colorScheme = theme" in rt
    # a framed page inherits the shell's resolved theme rather than asking matchMedia
    assert re.search(r"function inheritedTheme\(\)", rt)
    assert 'el.getAttribute("data-theme") || el.style.colorScheme' in rt
    # cross-window convergence: the storage event and the OS flip
    assert 'event.key === null || event.key === THEME_KEY' in rt
    assert 'matchMedia(DARK_QUERY).addEventListener("change", apply)' in rt


def test_shell_bootstrap_sets_data_theme_pre_paint():
    for p in BOOTSTRAPS:
        html = _read(p)
        head = html.index("</head>")
        assert 'document.documentElement.setAttribute("data-theme", theme)' in html[:head], p
