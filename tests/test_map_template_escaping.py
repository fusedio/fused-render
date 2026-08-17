"""The map template escapes untrusted strings before they reach innerHTML.

SECURITY.md's D3/D4 concession — no output sanitization in the render path —
is about the CONTENT OF A FILE YOU CHOSE TO OPEN. It does not extend to
strings that arrive incidentally and land in this template's own chrome:

  * `?dir=` and `?open=`, read at boot, so a crafted link to the app's own
    built-in template is reflected XSS with no click;
  * backend error text that echoes those params back (discover.py returns
    f"Not a directory: {base}"), rendered by the toast;
  * filenames, directory names and absolute paths from a listing — a shared
    folder, an unzipped archive or a mounted bucket can name an entry
    anything, and `"` in a path breaks out of a title="..." attribute;
  * sub-layer names read out of a GPKG/KML;
  * geocoder results from a third-party API.

Injected script here is same-origin with the shell, so it can set X-Fused and
POST /api/run — it walks around the D36 guard rather than breaking it.

The escaping behaviour is checked by running the template's REAL `esc` under
node (the `_js_block` approach of test_calls.py / test_log_studio_detail.py —
a copy would keep passing after the shipping code regressed). The second test
is the regression guard that actually matters: it re-derives every `${...}`
reaching an innerHTML sink and fails on a new one that is neither escaped nor
a known-constant fragment.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import fused_render


TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(fused_render.__file__)),
                        "templates", "map", "template.html")


def _src():
    return open(TEMPLATE, encoding="utf-8").read()


def _esc_decl(src):
    """The shipping `const esc = ...;` declaration, verbatim."""
    m = re.search(r"^const esc = .*?;$", src, re.S | re.M)
    assert m, "map template no longer declares `esc` — did the escaping go away?"
    return m.group(0)


def _run_esc(values, tmp_path):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        _esc_decl(_src())
        + f"\nconsole.log(JSON.stringify({json.dumps(values)}.map(esc)));\n",
        encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ---------------------------------------------------------------- behaviour

def test_esc_neutralizes_markup_and_attribute_breaks(tmp_path):
    payloads = [
        '<img src=x onerror=alert(1)>',            # element-text injection
        'x" onmouseover="alert(1)',                # title="..." attribute break
        "x' onmouseover='alert(1)",                # single-quoted attribute
        '</span><script>alert(1)</script>',        # tag close then script
        'a & b',                                   # ampersand stays a literal
    ]
    got = _run_esc(payloads, tmp_path)
    for out in got:
        assert "<" not in out and ">" not in out
        assert '"' not in out and "'" not in out
    assert got[0] == "&lt;img src=x onerror=alert(1)&gt;"
    assert got[1] == "x&quot; onmouseover=&quot;alert(1)"
    assert got[-1] == "a &amp; b"


def test_esc_handles_null_and_undefined(tmp_path):
    """Sinks pass optional fields straight through; esc must not print
    "undefined" or throw."""
    assert _run_esc([None], tmp_path) == [""]


# ------------------------------------------------- the regression guard

def _innerhtml_interpolations(src):
    """Every `${...}` inside a statement that writes HTML, re-derived from the
    source rather than listed, so a NEW sink is covered automatically."""
    exprs = set()
    for m in re.finditer(r"innerHTML\s*=|insertAdjacentHTML\(", src):
        i, depth, instr = m.end(), 0, None
        while i < len(src):
            c = src[i]
            if instr:
                if c == "\\":
                    i += 2
                    continue
                if c == instr:
                    instr = None
            elif c in "\"'`":
                instr = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == ";" and depth == 0:
                break
            i += 1
        stmt = src[m.start():i]
        exprs.update(e.strip() for e in
                     re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", stmt))
    return exprs


# Fragments that are safe WITHOUT esc() and why. Anything else interpolated
# into HTML must go through esc().
_SAFE = {
    # Inline SVG/icon constants defined in this template.
    "CHEV", "DOTS", "KIND_ICON.dir", "L.visible ? EYE : EYEOFF",
    "anyVisible ? EYE : EYEOFF",
    # Literal class-name / CSS switches — both branches are string literals.
    'L.visible ? "" : "off"', 'anyVisible ? "" : "off"',
    'collapsed ? "" : "open"',
    'd.status === "error" ? "var(--bad)" : "var(--warn)"',
    # Numbers and number formatters.
    "members.length", "m.pct || 0", "m.count.toLocaleString()",
    "st.bands", "st.width", "st.height", "d.timing_ms",
    "fmt(st.p2 ?? st.min)", "fmt(st.p98 ?? st.max)",
    "c.lat.toFixed(4)", "c.lng.toFixed(4)", "map.getZoom().toFixed(1)",
    "e.lngLat.lat.toFixed(4)", "e.lngLat.lng.toFixed(4)",
    "(i * 0.045).toFixed(2)", "(i * 0.08).toFixed(2)",
    "(i * 0.08 + 0.05).toFixed(2)", "42 - i * 6", "64 - i * 9", "w",
    # Locally generated markup / derived styles, no external string in them.
    "gutter", "lines", "swatchStyle(L)", "cmapGradient(s.colormap||'viridis')",
    "cmapGradient(s.colormap||'gray')",
    # The browser-read COG's contrast legend: both ends go through Number(),
    # so neither can carry a string out of the file or the URL.
    "Number(lo)", "Number(hi)",
    "encodeURIComponent(q)",
    # Backend enums and control labels: descriptor kinds and the literal
    # strings the style dock passes to ctlShell ("Opacity", "Colormap", ...).
    "m.phase", "st.dtype", "m.geometry_type || d.geometry_type || \"vector\"",
    'preparing ? "tiles " + (L.mvt.pct || 0) + "%" : kindBadge(d)',
    'm.count ? ` · ${m.count.toLocaleString()} features` : ""',
    "label", 'valTxt||""',
}


def test_every_html_interpolation_is_escaped_or_a_known_constant():
    unescaped = sorted(
        e for e in _innerhtml_interpolations(_src())
        if not e.startswith("esc(") and e not in _SAFE
        and "esc(" not in e  # nested, e.g. the toast's optional <span>
    )
    assert not unescaped, (
        "unescaped value(s) interpolated into innerHTML in the map template: "
        f"{unescaped}. Wrap each in esc(), or — if it is genuinely a constant "
        "or a number — add it to _SAFE with a note saying why."
    )


def test_the_known_untrusted_sinks_go_through_esc():
    """Belt and braces on the specific sinks the review found, so a refactor
    that reintroduces one is named in the failure rather than only counted."""
    src = _src()
    for needle in (
        "name.textContent = entry.name",                     # directory listing
        "row.title = entry.path",                            # file path attribute
        '<span class="lc-nm" title="${esc(L.target)}">${esc(L.name)}</span>',
        "${esc(d.message || (d.error && d.error.message)",   # backend error
        '<b>${esc(title)}</b>',                              # toast title
        '${esc(String(msg).slice(0,300))}',                  # toast message
        '<div class="g1">${esc(name)}</div>',                # geocoder result
        "${esc(path)}</div>",                                # code viewer path
    ):
        assert needle in src, f"map template no longer escapes: {needle}"
