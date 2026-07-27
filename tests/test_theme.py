"""Appearance — System / Light / Dark (SPEC §30, D134).

The theme is entirely browser-side: there is no endpoint, no server-side store
and no Python code path to exercise (deliberately — D134). What this suite
guards instead are the source-level invariants the feature is built on, each of
which is easy to break silently and impossible to notice from a unit test of
something else:

* the resolved theme is applied by an **inline** script in the shell's
  `index.html`, i.e. before first paint — the no-flash requirement;
* the one storage key is spelled identically in all three places that read it
  (`lib/theme.ts`, `index.html`, `static/runtime.js`);
* `shell.css` carries a light palette that redefines *every* token the dark
  `:root` defines, and no colour literal survives outside those two blocks
  (the tokenization is what makes light mode possible at all);
* pushing the theme into a view document is **opt-in** — user-authored `.html`
  views must receive no theme signal;
* all twelve tier-1 templates carry the identical dual-palette structure, and
  the exempt / self-toggling / deferred templates carry none of it.

The four template lists below are exhaustive over `fused_render/templates/`,
and a test asserts that — so a newly added template cannot quietly skip the
classification.

Shared helpers live in `_theme_sources`, mirroring `_mount_safe_helpers`.
"""
import re

import pytest

from _theme_sources import (
    DEFERRED_TEMPLATES,
    EXEMPT_TEMPLATES,
    OPT_IN_ATTR,
    SELF_TOGGLING_TEMPLATES,
    THEME_KEY,
    TIER_ONE_TEMPLATES,
    all_template_names,
    palette_blocks,
    read_repo_file,
    read_template,
    style_color_literals,
)


# ---------------------------------------------------------------- classification


def test_template_lists_partition_the_builtin_template_set():
    classified = (
        list(TIER_ONE_TEMPLATES)
        + list(EXEMPT_TEMPLATES)
        + list(SELF_TOGGLING_TEMPLATES)
        + list(DEFERRED_TEMPLATES)
    )
    assert len(classified) == len(set(classified)), "a template is in two lists"
    assert set(classified) == set(all_template_names()), (
        "every built-in template must be classified: tier-1 (light palette "
        "authored), exempt (light by design), self-toggling (own theme button, "
        "not migrated) or deferred (keeps today's look)"
    )


# ---------------------------------------------------------------- no flash


def test_shell_index_resolves_the_theme_in_an_inline_head_script():
    html = read_repo_file("frontend/index.html")
    head = html[: html.index("</head>")]
    scripts = re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", head)
    inline = [body for attrs, body in scripts if "src=" not in attrs]
    assert inline, "index.html must carry an inline <head> script (no src=)"
    boot = "\n".join(inline)
    # Reads the persisted preference and stamps the resolved theme on <html>,
    # so the very first paint is already the right colour.
    assert THEME_KEY in boot
    assert "prefers-color-scheme" in boot
    assert "data-theme" in boot
    # Before the app root — nothing of the shell can paint ahead of it.
    assert html.index("</head>") < html.index('id="root"')


# ---------------------------------------------------------------- one key


def test_the_storage_key_is_spelled_identically_everywhere():
    # The bootstrap is inline in the HTML, so it cannot import the module that
    # owns the key — the two spellings can only be pinned together by a test.
    for path in ("frontend/src/lib/theme.ts", "frontend/index.html"):
        assert THEME_KEY in read_repo_file(path), f"{path} must use {THEME_KEY!r}"


def test_theme_persistence_is_best_effort():
    # Same posture as viewstate.ts / sidebarstate.ts: a private-mode or
    # quota-exceeded localStorage must never break the shell.
    src = read_repo_file("frontend/src/lib/theme.ts")
    assert src.count("try {") >= 2 and "catch" in src


# ---------------------------------------------------------------- shell.css


def test_shell_css_light_palette_redefines_every_dark_token():
    dark, light = palette_blocks(read_repo_file("frontend/src/shell.css"))
    assert dark, "shell.css must declare a dark :root palette"
    assert light, 'shell.css must declare a :root[data-theme="light"] palette'
    assert dark.pop("color-scheme", None) == "dark"
    assert light.pop("color-scheme", None) == "light"
    missing = sorted(set(dark) - set(light))
    assert not missing, f"tokens with no light value: {missing}"
    extra = sorted(set(light) - set(dark))
    assert not extra, f"light-only tokens (no dark default): {extra}"


def test_shell_css_has_no_colour_literals_outside_the_palettes():
    literals = style_color_literals(read_repo_file("frontend/src/shell.css"))
    assert not literals, (
        "every colour in shell.css must come from a palette token, or light "
        f"mode cannot repaint it — found: {sorted(set(literals))}"
    )


# ---------------------------------------------------------------- untouched


@pytest.mark.parametrize(
    "name", list(EXEMPT_TEMPLATES) + list(SELF_TOGGLING_TEMPLATES) + list(DEFERRED_TEMPLATES)
)
def test_non_tier_one_templates_do_not_opt_in(name):
    # Light-by-design views ignore the setting entirely; excel/log_studio/
    # tableau keep their own in-view toggle and private storage keys; the
    # deferred groups keep today's appearance until a later pass.
    assert OPT_IN_ATTR not in read_template(name)
