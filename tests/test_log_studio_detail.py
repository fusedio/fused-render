"""log_studio's expanded-row detail and level facets (owner-reported, D153).

Both behaviours live in `template.html`'s JavaScript, so both are driven by
extracting the shipping functions and running them under node — a copy would
keep passing after the real code regressed, which is the one thing these must
not do (the same `_js_block` approach `test_calls.py` uses for the calls view).
"""
import json
import os
import shutil
import subprocess

import pytest

import fused_render


TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(fused_render.__file__)),
                        "templates", "log_studio", "template.html")


def _src():
    return open(TEMPLATE, encoding="utf-8").read()


def _js_block(src, header):
    """`header` plus its brace-balanced body, verbatim from the template."""
    start = src.index(header)
    open_brace = src.index("{", start)
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _run(harness_body, tmp_path, prelude=""):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(prelude + harness_body, encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------- the structured detail view

def _detail_prelude():
    src = _src()
    return (
        "function escapeHtml(s) { return String(s == null ? '' : s)"
        ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')"
        ".replace(/\"/g,'&quot;'); }\n"
        + _js_block(src, "function parseStructured(raw)") + "\n"
        + _js_block(src, "function isBlockText(value)") + "\n"
        + _js_block(src, "function scalarHtml(value)") + "\n"
        + _js_block(src, "function fieldRows(value, depth)") + "\n"
        + _js_block(src, "function structuredHtml(parsed)") + "\n"
    )


def test_a_json_line_is_parsed_into_fields(tmp_path):
    """The point of the feature: a structured line renders as fields, not as one
    long escaped string with the interesting value buried in the middle."""
    line = json.dumps({"level": "ERROR", "call_id": "c1", "server_ms": 12,
                       "truncated": False, "err_id": None})
    result = _run(
        f"const parsed = parseStructured({json.dumps(line)});\n"
        "const html = structuredHtml(parsed);\n"
        "console.log(JSON.stringify({ parsed: !!parsed, prefix: parsed.prefix, html }));\n",
        tmp_path, _detail_prelude())

    assert result["parsed"] is True
    assert result["prefix"] == ""
    html = result["html"]
    # Every key is a field label, and the types are distinguishable.
    for key in ("level", "call_id", "server_ms", "truncated", "err_id"):
        assert f">{key}</span>" in html, key
    assert 'class="field-number"' in html, "12 renders as a number"
    assert 'class="field-boolean"' in html, "false renders as a boolean"
    assert 'class="field-null"' in html, "null renders as null, not as empty"
    # Scalars are filter chips, reusing the [data-query] contract.
    assert 'data-query="ERROR"' in html


def test_a_plain_text_line_is_left_alone(tmp_path):
    """This is an addition for lines that ARE structured, never a
    reinterpretation of lines that are not: a plain log line must not be
    coerced into a field view."""
    result = _run(
        "const lines = ['2026-07-27 10:11:12 INFO server started',\n"
        "  'Traceback (most recent call last):', '', '[1, 2, 3]', '\"just a string\"'];\n"
        "console.log(JSON.stringify(lines.map((l) => parseStructured(l) !== null)));\n",
        tmp_path, _detail_prelude())
    assert result == [False, False, False, False, False], \
        "arrays and scalars are not field sets; only an object earns the view"


def test_a_prefixed_json_line_keeps_the_prefix_visible(tmp_path):
    """`<timestamp> <level> {json}` is a real and common shape. The JSON gets
    the field view, and the text before it is SHOWN as its own row rather than
    silently dropped — dropping part of the line would be worse than not
    parsing it at all."""
    line = '2026-07-27 10:11:12 INFO {"event": "run", "ms": 4}'
    result = _run(
        f"const parsed = parseStructured({json.dumps(line)});\n"
        "console.log(JSON.stringify({ prefix: parsed.prefix, "
        "keys: Object.keys(parsed.value), html: structuredHtml(parsed) }));\n",
        tmp_path, _detail_prelude())

    assert result["prefix"] == "2026-07-27 10:11:12 INFO"
    assert result["keys"] == ["event", "ms"]
    assert "(line prefix)" in result["html"]
    assert "2026-07-27 10:11:12 INFO" in result["html"]


def test_nested_objects_are_indented_under_their_key(tmp_path):
    line = json.dumps({"error": {"type": "ValueError", "message": "bad"}, "n": 1})
    result = _run(
        f"const parsed = parseStructured({json.dumps(line)});\n"
        "console.log(JSON.stringify({ html: structuredHtml(parsed) }));\n",
        tmp_path, _detail_prelude())
    html = result["html"]
    assert "--depth:0" in html and "--depth:1" in html, "the nested pair is indented"
    assert "2 field(s)" in html, "the parent row says what is inside"
    assert ">type</span>" in html and ">message</span>" in html


def test_a_multiline_value_becomes_a_block_not_a_cell(tmp_path):
    """A traceback's newlines ARE its content, so it spans the row as a block.
    Crushed into a value cell it is unreadable, which is the whole complaint
    about the flat view."""
    traceback = "Traceback (most recent call last):\n  File \"a.py\", line 1\nValueError"
    line = json.dumps({"msg": "boom", "traceback": traceback})
    result = _run(
        f"const parsed = parseStructured({json.dumps(line)});\n"
        "console.log(JSON.stringify({ html: structuredHtml(parsed), "
        f"block: isBlockText({json.dumps(traceback)}), short: isBlockText('boom') }}));\n",
        tmp_path, _detail_prelude())

    assert result["block"] is True and result["short"] is False
    assert 'class="field-block"' in result["html"]
    assert "ValueError" in result["html"]


def test_field_values_are_html_escaped(tmp_path):
    """The values come from a log file, so they are untrusted text: a line
    carrying markup must render as text, in the value AND in the data-query
    attribute the filter chip carries."""
    line = json.dumps({"msg": '<img src=x onerror="alert(1)">'})
    result = _run(
        f"const parsed = parseStructured({json.dumps(line)});\n"
        "console.log(JSON.stringify({ html: structuredHtml(parsed) }));\n",
        tmp_path, _detail_prelude())
    html = result["html"]
    assert "<img" not in html
    assert "&lt;img" in html
    assert 'onerror="alert(1)"' not in html


def test_deep_nesting_terminates(tmp_path):
    """A pathological record must not recurse without bound."""
    deep = {"a": None}
    node = deep
    for _ in range(40):
        node["a"] = {"a": None}
        node = node["a"]
    result = _run(
        f"const parsed = parseStructured({json.dumps(json.dumps(deep))});\n"
        "console.log(JSON.stringify({ html: structuredHtml(parsed).length }));\n",
        tmp_path, _detail_prelude())
    assert result["html"] > 0, "renders, bounded, instead of blowing the stack"


def test_the_row_defaults_to_fields_and_keeps_the_raw_text(tmp_path):
    """Structured by default when the line is structured — that is why this
    exists — with the raw text one click away and never removed."""
    src = _src()
    row = _js_block(src, "function lineRow(fields)")
    assert 'data-view="structured"' in row and 'data-view="raw"' in row
    assert "<pre hidden>" in row, "raw text is present, just hidden"
    assert 'data-detail-view="raw"' in row, "and reachable through a toggle"


# ------------------------------------------------------- level facets (D153)

def _levels_prelude():
    src = _src()
    return (
        "const LEVEL_ORDER = ['FATAL','ERROR','WARN','INFO','DEBUG','TRACE','OTHER'];\n"
        "function levelName(v) { return String(v || 'UNKNOWN').toUpperCase(); }\n"
        "function number(v, d = 0) { const n = Number(v); "
        "return Number.isFinite(n) ? n : d; }\n"
        + _js_block(src, "function overviewLevels(data)") + "\n"
    )


def _facet_body(levels, selected):
    """The facet-list filter, lifted from renderOverview so the test exercises
    the shipping expression rather than a restatement of it."""
    src = _src()
    start = src.index("    const selected = new Set(state.levels);")
    end = src.index("const active =", start)
    return (
        f"const state = {{ levels: {json.dumps(selected)} }};\n"
        f"const data = {{ levels: {json.dumps(levels)} }};\n"
        + src[start:end]
        + "console.log(JSON.stringify(entries.map(([l, c]) => [l, c])));\n"
    )


def test_levels_absent_from_the_file_are_not_offered(tmp_path):
    """The reader seeds its count map with every level it knows (right for
    histogram buckets, which need a zero to stack), so the facet list used to
    offer all seven on every file — six dead filters on a log that only writes
    INFO."""
    levels = {"TRACE": 0, "DEBUG": 0, "INFO": 42, "WARN": 3,
              "ERROR": 0, "FATAL": 0, "OTHER": 0}
    result = _run(_facet_body(levels, []), tmp_path, _levels_prelude())
    assert result == [["WARN", 3], ["INFO", 42]], \
        "only the levels present, in LEVEL_ORDER"


def test_a_selected_level_survives_at_zero(tmp_path):
    """The trap in filtering by count: a SELECTED level at zero is reachable —
    a narrowed window, or a rotated file — and hiding its chip removes the only
    control that undoes an active constraint, leaving a filter the user can
    neither see nor clear."""
    levels = {"INFO": 42, "ERROR": 0, "DEBUG": 0, "TRACE": 0,
              "WARN": 0, "FATAL": 0, "OTHER": 0}
    result = _run(_facet_body(levels, ["ERROR"]), tmp_path, _levels_prelude())
    assert result == [["ERROR", 0], ["INFO", 42]], \
        "the active ERROR filter stays visible so it can be cleared"


def test_an_empty_file_offers_no_levels(tmp_path):
    levels = {level: 0 for level in
              ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "OTHER")}
    result = _run(_facet_body(levels, []), tmp_path, _levels_prelude())
    assert result == [], "the renderer's 'No level data' branch takes over"
