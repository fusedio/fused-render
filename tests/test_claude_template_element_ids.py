"""Every `getElementById("...")` in the claude template names an id that exists.

The bug this guards: an id can be removed from the markup while a
`getElementById` lookup for it survives elsewhere in the file (the two are
thousands of lines apart and nothing ties them together). The failure mode is
not a null-check miss somewhere deep in a handler - `ResizeObserver.observe`,
`addEventListener`, and plenty of DOM APIs throw on `null` immediately, at
whatever scope the lookup happens to run in. When that scope is top level, the
throw aborts the rest of the script's top-level execution, silently disabling
every handler wired up after it - the send button falls back to a native form
submit, an unrelated part of the page stops updating, and so on, with no
console-visible connection back to the missing id.

This is a single HTML file with inline `<script>`, so a plain regex scan of
the whole thing is enough: collect every `getElementById("...")` argument and
every `id="..."` in the markup, and assert the first set is a subset of the
second.
"""
import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "fused_render" / "templates" / "claude" / "template.html"
)

_GET_BY_ID_RE = re.compile(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)')
_ID_ATTR_RE = re.compile(r'\bid=["\']([^"\']+)["\']')


def test_every_get_element_by_id_target_exists_in_the_markup():
    html = TEMPLATE.read_text(encoding="utf-8")
    looked_up = set(_GET_BY_ID_RE.findall(html))
    present = set(_ID_ATTR_RE.findall(html))

    missing = sorted(looked_up - present)
    assert not missing, (
        "getElementById(...) looks up an id that no longer appears anywhere "
        f"in the markup (id=\"...\"): {missing}. That call returns null and "
        "whatever DOM API it feeds (ResizeObserver.observe, "
        "addEventListener, ...) throws - if the lookup runs at script top "
        "level, that throw silently aborts every statement after it, "
        "including composer wiring further down the file."
    )
