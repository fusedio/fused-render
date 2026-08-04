"""Appearance — System / Light / Dark (SPEC §30, D134).

The theme is entirely browser-side: there is no endpoint and no server-side
store (deliberately — D134). What this suite guards are the source-level
invariants the feature is built on, each of which is easy to break silently and
impossible to notice from a unit test of something else.

Source-level is NOT the whole story, though, and assuming it was is what let
this feature ship broken. Every assertion below reads a *repo* file, but the
server serves built-in templates from a staged copy under
`~/.fused-render/.core-templates` (fused_render/core_templates.py). A retheme'd
template can therefore be perfect in the repo and never reach a browser. The
delivery half of the contract — that `/render` actually sends a theme-aware
template — is pinned in tests/test_core_templates.py; keep the two together.

What this suite pins:

* the resolved theme is applied by an **inline** script in the shell's
  `index.html`, i.e. before first paint — the no-flash requirement;
* the one storage key is spelled identically in all three places that read it
  (`lib/theme.ts`, `index.html`, `static/runtime.js`);
* `shell.css` carries a light palette that redefines *every* token the dark
  `:root` defines, and no colour literal survives outside those two blocks
  (the tokenization is what makes light mode possible at all);
* pushing the theme into a view document is **opt-in** — user-authored `.html`
  views must receive no theme signal;
* every tier-1 template carries the identical dual-palette structure, and
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
    SHARED_PALETTE_SELECTORS,
    THEME_KEY,
    TIER_ONE_TEMPLATES,
    TOKENIZED_SHARED_ASSETS,
    UNMIGRATED_SHARED_ASSETS,
    _selector_block,
    all_shared_asset_names,
    all_template_names,
    palette_blocks,
    read_repo_file,
    read_shared_asset,
    read_template,
    scoped_palette_blocks,
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


def test_the_bootstrap_still_checks_the_os_when_storage_throws():
    # A private-mode localStorage throws on getItem. With the read and the
    # matchMedia fallback sharing one try block, that throw skipped the OS check
    # entirely and silently pinned dark for a light-mode user. Separate blocks
    # keep the fallback reachable — the structure resolvedTheme() in
    # static/runtime.js already uses.
    html = read_repo_file("frontend/index.html")
    boot = "\n".join(
        body
        for attrs, body in re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", html)
        if "src=" not in attrs
    )
    blocks = re.findall(r"try\s*\{([\s\S]*?)\}\s*catch", boot)
    assert len(blocks) >= 2, "the storage read and the OS check need their own try blocks"
    for block in blocks:
        assert not ("localStorage" in block and "matchMedia" in block), (
            "a throwing localStorage must not skip the prefers-color-scheme check"
        )


# ---------------------------------------------------------------- one key


def test_the_storage_key_is_spelled_identically_everywhere():
    # Three independent readers, no shared module between them (the bootstrap
    # is inline in the HTML, and runtime.js ships into a different document).
    # Deliberately three and not more: a template that wants the theme opts in
    # with `data-fused-theme` and lets runtime.js resolve it, rather than
    # becoming a fourth place this key is spelled.
    for path in (
        "frontend/src/lib/theme.ts",
        "frontend/index.html",
        "fused_render/static/runtime.js",
    ):
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


# ---------------------------------------------------------------- runtime push


def test_runtime_themes_only_documents_that_opt_in():
    src = read_repo_file("fused_render/static/runtime.js")
    assert OPT_IN_ATTR in src, (
        f"runtime.js must gate theming on the {OPT_IN_ATTR!r} opt-in so "
        "user-authored .html views receive no theme signal"
    )
    # Live follow: an OS flip and another window's override must both land.
    assert "prefers-color-scheme" in src
    assert '"storage"' in src


def test_runtime_never_remounts_or_reloads_to_apply_a_theme():
    # The standing rule (Panel.tsx / PaneModeMenu.tsx): a re-render must never
    # touch a live iframe, so the theme is pushed as an attribute write.
    src = read_repo_file("fused_render/static/runtime.js")
    theme_section = src[src.index(OPT_IN_ATTR) - 2000 : src.index(OPT_IN_ATTR) + 2000]
    assert "location.reload" not in theme_section


# ---------------------------------------------------------------- tier 1


@pytest.mark.parametrize("name", TIER_ONE_TEMPLATES)
def test_tier_one_template_opts_in(name):
    html = read_template(name)
    open_tag = re.search(r"<html\b[^>]*>", html, re.I)
    assert open_tag, f"{name}: no <html> tag"
    assert OPT_IN_ATTR in open_tag.group(0), (
        f"{name}: the opt-in must sit on <html> — runtime.js runs from the top "
        "of <head>, before a <meta> further down has been parsed"
    )


@pytest.mark.parametrize("name", TIER_ONE_TEMPLATES)
def test_tier_one_template_declares_both_palettes(name):
    dark, light = palette_blocks(read_template(name))
    assert dark.pop("color-scheme", None) == "dark", f"{name}: dark :root"
    assert light.pop("color-scheme", None) == "light", f"{name}: light :root"
    assert dark, f"{name}: the dark :root must define palette tokens"
    assert set(dark) == set(light), (
        f"{name}: dark/light token sets differ — "
        f"dark-only {sorted(set(dark) - set(light))}, "
        f"light-only {sorted(set(light) - set(dark))}"
    )


@pytest.mark.parametrize("name", TIER_ONE_TEMPLATES)
def test_tier_one_template_has_no_colour_literals_outside_its_palettes(name):
    literals = style_color_literals(read_template(name))
    assert not literals, (
        f"{name}: colours outside the palette blocks cannot follow the theme — "
        f"found {sorted(set(literals))}"
    )


# ------------------------------------------------------- shared template assets
# The layer this suite used to miss entirely. `templates/shared/*.js` injects CSS
# into the HOST template's document, so its colours are bound by the same rule as
# the template's own <style> — but nothing scanned those files, and
# folder-picker.js shipped a hardcoded dark palette (plus `var()` names from only
# one of the two vocabularies templates use) that ignored the toggle.


def test_shared_asset_lists_partition_the_shared_script_set():
    classified = list(TOKENIZED_SHARED_ASSETS) + list(UNMIGRATED_SHARED_ASSETS)
    assert sorted(classified) == all_shared_asset_names(), (
        "every templates/shared script must be classified tokenized or "
        f"unmigrated — classified {sorted(classified)}, on disk "
        f"{all_shared_asset_names()}"
    )
    assert len(set(classified)) == len(classified), "a script is in both lists"


@pytest.mark.parametrize("name", TOKENIZED_SHARED_ASSETS)
def test_tokenized_shared_asset_has_no_colour_literals_outside_its_palettes(name):
    literals = style_color_literals(
        read_shared_asset(name), palette_selectors=SHARED_PALETTE_SELECTORS
    )
    assert not literals, (
        f"shared/{name}: this CSS lands in a themed template document, so a "
        f"literal outside a palette block cannot follow the theme — found "
        f"{sorted(set(literals))}"
    )


def test_the_folder_picker_declares_both_palettes_scoped_to_itself():
    # Scoped, not `:root`: a shared script that wrote `:root` rules would
    # redefine its HOST template's palette. Same dual-block contract otherwise.
    dark, light = scoped_palette_blocks(
        read_shared_asset("folder-picker.js"), *SHARED_PALETTE_SELECTORS
    )
    assert dark, "folder-picker.js must define its own dark palette tokens"
    assert set(dark) == set(light), (
        "folder-picker.js dark/light token sets differ — "
        f"dark-only {sorted(set(dark) - set(light))}, "
        f"light-only {sorted(set(light) - set(dark))}"
    )


def test_the_folder_picker_paints_only_through_its_own_tokens():
    # Every colour a rule uses must be one of the picker's OWN --fp-* tokens.
    # Reaching straight for a host token (`var(--surface)`) is the bug this
    # replaces: half the templates spell that `--bg-alt`, so the reference
    # resolved in three views and fell through to a dark literal in nineteen.
    # The mapping to host names belongs in the palette blocks and nowhere else.
    src = read_shared_asset("folder-picker.js")
    for selector in SHARED_PALETTE_SELECTORS:
        src = _selector_block(selector).sub("", src)
    referenced = set(re.findall(r"var\((--[\w-]+)", src))
    assert referenced, "no tokens referenced at all — is the CSS still there?"
    assert all(name.startswith("--fp-") for name in referenced), (
        "folder-picker.js must paint through its own --fp-* tokens; found "
        f"{sorted(n for n in referenced if not n.startswith('--fp-'))}"
    )


# ---------------------------------------------------------------- untouched


@pytest.mark.parametrize(
    "name", list(EXEMPT_TEMPLATES) + list(SELF_TOGGLING_TEMPLATES) + list(DEFERRED_TEMPLATES)
)
def test_non_tier_one_templates_do_not_opt_in(name):
    # Light-by-design views ignore the setting entirely; excel/log_studio/
    # tableau keep their own in-view toggle; the deferred groups keep today's
    # appearance until a later pass.
    #
    # Checked on the <html> TAG, mirroring test_tier_one_template_opts_in — the
    # opt-in is only an opt-in there (AP-8: runtime.js runs from the top of
    # <head>). A bare substring search over the file also matched the attribute
    # NAMED in a comment, which is how log_studio's own explanation of why it
    # does not opt in read as opting in.
    html = read_template(name)
    open_tag = re.search(r"<html\b[^>]*>", html, re.I)
    assert open_tag, f"{name}: no <html> tag"
    assert OPT_IN_ATTR not in open_tag.group(0)
