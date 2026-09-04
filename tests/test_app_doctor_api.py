"""App Doctor's fused API misuse check (fused_render/app_doctor.py): a page
calling a `fused.*` member the runtime does not expose will fail the moment
it runs, and nothing else catches that before the author does.

THE PARITY TEST BELOW IS THE WHOLE REASON THIS CHECK CAN BE TRUSTED. The
doctor's `KNOWN_FUSED_MEMBERS` / `KNOWN_NAMESPACED_MEMBERS` are a hand-copied
list — app_doctor.py cannot import the runtime (it is JS, and the module is
stdlib-only besides), so the list has to be maintained by hand. A hand-copied
list rots silently the first time the runtime gains a member, in the
direction of false alarms: a real, working call gets flagged as unknown.
`test_the_known_members_match_the_real_runtime` closes that gap by parsing
`fused_render/static/runtime.js` itself — the one real source — and failing
loudly the moment the two drift apart, so drift is a broken test to fix, not
a bug report from a confused app author.
"""
import os
import re

from fused_render import app_doctor

RUNTIME_JS = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "static", "runtime.js")


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _rules(findings):
    return {f["rule"] for f in findings}


def _matching_brace(text: str, open_index: int) -> int:
    """Index of the `}` that closes the `{` at `open_index`, ignoring braces
    inside `'...'`/`"..."` string literals — everything these object
    literals contain is identifiers, numbers and short strings, never a
    brace-carrying template literal."""
    depth = 0
    i = open_index
    in_str = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError("unbalanced braces in runtime.js object literal")


def _object_literal_keys(text: str, open_index: int) -> set:
    """Member names of the object literal opening at `open_index`: `key:` and
    bare shorthand (`daemon,`) entries at the literal's OWN depth, skipping
    line comments, string contents, and anything inside a nested `{}` or a
    `()` (a value can be an arrow function whose body calls another function
    with its own comma-separated arguments — those commas are not entry
    separators)."""
    close_index = _matching_brace(text, open_index)
    body = text[open_index + 1:close_index]
    body = re.sub(r"//[^\n]*", "", body)

    keys = set()
    depth = 0
    token = []
    in_str = None
    i = 0
    while i <= len(body):
        c = body[i] if i < len(body) else ","
        if in_str:
            token.append(c) if depth == 0 else None
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
            if depth == 0:
                token.append(c)
        elif c in "{(":
            depth += 1
        elif c in "})":
            depth -= 1
        elif c == "," and depth == 0:
            piece = "".join(token).strip()
            if piece:
                name = re.match(r"[A-Za-z_$][\w$]*", piece)
                if name:
                    keys.add(name.group(0))
            token = []
        elif depth == 0:
            token.append(c)
        i += 1
    return keys


def _object_literal_after(text: str, marker: str) -> set:
    idx = text.index(marker)
    brace = text.index("{", idx)
    return _object_literal_keys(text, brace)


def test_the_known_members_match_the_real_runtime():
    text = open(RUNTIME_JS, encoding="utf-8").read()

    top_level = _object_literal_after(text, "window.fused = {")
    assert top_level == app_doctor.KNOWN_FUSED_MEMBERS

    namespaced = {
        "ai": _object_literal_after(text, "const ai = {"),
        "fileIndex": _object_literal_after(text, "const fileIndex = {"),
        "capture": _object_literal_after(text, "const capture = {"),
    }
    # `params` has no top-level `const` — it is written inline as
    # `params: { get, getAll, set, onChange }` inside window.fused itself.
    params_idx = text.index("params: {", text.index("window.fused = {"))
    namespaced["params"] = _object_literal_keys(text, text.index("{", params_idx))

    assert namespaced == dict(app_doctor.KNOWN_NAMESPACED_MEMBERS)


# --------------------------------------------------------------- the check


def _page(tmp_path, body, name="index.html"):
    _write(tmp_path, name, (
        '<html><head><meta name="fused-app" /></head><body>'
        f"<script>{body}</script></body></html>\n"
    ))


def test_an_unknown_member_is_flagged(tmp_path):
    _page(tmp_path, "fused.doesNotExist();")
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "api-misuse:unknown-member")
    assert hit["severity"] == "high"
    assert "fused.doesNotExist" in hit["excerpt"]


def test_every_real_top_level_member_is_clean(tmp_path):
    calls = "\n".join(f"fused.{name};" for name in app_doctor.KNOWN_FUSED_MEMBERS)
    _page(tmp_path, calls)
    findings = app_doctor.check(str(tmp_path))
    assert "api-misuse:unknown-member" not in _rules(findings)


def test_a_namespaced_call_under_params_is_resolved(tmp_path):
    _page(tmp_path, "fused.params.get('x');")
    findings = app_doctor.check(str(tmp_path))
    assert "api-misuse:unknown-member" not in _rules(findings)


def test_an_unknown_namespaced_member_is_flagged(tmp_path):
    _page(tmp_path, "fused.ai.notAMethod();")
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "api-misuse:unknown-member")
    assert "fused.ai.notAMethod" in hit["excerpt"]


def test_a_real_namespaced_call_is_clean(tmp_path):
    _page(tmp_path, "fused.ai.text('hi'); fused.fileIndex.search('x');")
    findings = app_doctor.check(str(tmp_path))
    assert "api-misuse:unknown-member" not in _rules(findings)


def test_only_direct_child_pages_are_scanned(tmp_path):
    _page(tmp_path, "fused.env;")
    _write(tmp_path, "sub/nested.html", "<script>fused.bogus();</script>\n")
    findings = app_doctor.check(str(tmp_path))
    assert "api-misuse:unknown-member" not in _rules(findings)
