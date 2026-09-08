"""The `claude` template's two host-fact classes for the Tasks page's Cards view.

* ``body.chat-only`` — stamped synchronously from ``?chat_only=1``. The pane
  column used to leave the document only when ``enterNoPane`` ran, which is an
  await into the boot, so a chat-only frame drew the two-column layout first and
  jumped to full width a moment later (Akshil, 2026-09-05, on the card popup:
  "first it opened only on the left side, then it made it full screen"). The
  class hides the column and its divider from the first paint.
* ``body.chat-peek`` — stamped from ``?peek=1``, the card POPUP's cut: the strip
  (← Chats, ⋮) and the top bar go, the composer stays. The popup's own head says
  which task this is and carries the doors, so those would be a second bar of
  the same facts under the first.

Pinned the way tests/test_claude_compact.py pins compact, for the same reasons:
a host fact is read off THIS frame's URL (never ``fused.params``), the hiding is
a stylesheet rule keyed on one class, and nothing else changes.
"""
import os
import re

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def html():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _style_block(html):
    start = html.index("<style>")
    return html[start:html.index("</style>", start)]


def _code(html):
    src = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("//"))


def test_chat_only_is_stamped_synchronously_beside_compact(html):
    code = _code(html)
    assert 'if (CHAT_ONLY) document.body.classList.add("chat-only");' in code
    # In the same synchronous run as compact's stamp — before any boot await —
    # which is what makes it a first-paint fact rather than a later correction.
    assert code.index('classList.add("chat-only")') < code.index("function enterNoPane()")
    assert code.index('classList.add("chat-compact")') < code.index('classList.add("chat-only")')


def test_chat_only_hides_the_pane_column_from_the_stylesheet(html):
    css = _style_block(html)
    rule = re.search(r"body\.chat-only #left,\s*body\.chat-only #divider \{\s*display: none;\s*\}", css)
    assert rule, "the pane column and divider must be hidden by the class, not by script"


def test_peek_is_a_host_fact_read_off_this_frames_url(html):
    code = _code(html)
    assert 'const PEEK = new URLSearchParams(location.search).get("peek") === "1";' in code
    assert 'if (PEEK) document.body.classList.add("chat-peek");' in code
    # Never through fused.params: that would land on the host's URL and outlive
    # the popup that chose it.
    assert 'fused.params.get("peek")' not in code


def test_peek_hides_strip_and_topbar_but_keeps_the_composer(html):
    css = _style_block(html)
    rule = re.search(r"body\.chat-peek #anntools,\s*body\.chat-peek #topbar \{\s*display: none;\s*\}", css)
    assert rule
    # The composer is the whole point of the popup over the card: compact hides
    # #inputbox and #home; peek must not.
    peek_rules = "\n".join(m.group(0) for m in re.finditer(r"body\.chat-peek[^{]*\{[^}]*\}", css))
    assert "#inputbox" not in peek_rules
    assert "#home" not in peek_rules
