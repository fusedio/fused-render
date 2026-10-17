"""Behavioural tests for the code editor template's helpers (SPEC §51).

These execute the template's own functions against the SHIPPED CodeMirror
bundle through scripts/vendor-codemirror/code-editor-probe.mjs, rather than
asserting about the template's source. Everything here is a contract whose
breakage is invisible in a diff: which language a path opens as, what
indentation a file is edited with, whether a CRLF file is saved back as CRLF,
and that "Format" never rewrites a JSON value.

The probe needs only node — it evaluates the committed bundle, not
scripts/vendor-codemirror/node_modules — so it skips only where node is absent.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "vendor-codemirror", "code-editor-probe.mjs")
TEMPLATE = os.path.join(ROOT, "fused_render", "templates", "code", "template.html")
BUNDLE = os.path.join(ROOT, "fused_render", "templates", "vendor", "codemirror.bundle.js")


def probe(*cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, PROBE, TEMPLATE, BUNDLE, json.dumps(list(cases))],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def ok(*cases):
    results = probe(*cases)
    for case, result in zip(cases, results):
        assert "ok" in result, (case, result)
    return [r["ok"] for r in results]


def test_every_listed_language_builds_from_the_shipped_bundle():
    (built,) = ok(["everyLanguageBuilds"])
    assert [b for b in built if b[1] != "ok"] == []
    ids = {b[0] for b in built}
    # The languages this editor was asked to support, at minimum.
    assert {"python", "javascript", "typescript", "tsx", "jsx", "json", "html"} <= ids


@pytest.mark.parametrize("path,lang", [
    ("/x/a.py", "python"),
    ("/x/a.js", "javascript"),
    ("/x/a.mjs", "javascript"),
    ("/x/a.jsx", "jsx"),
    ("/x/a.ts", "typescript"),
    ("/x/a.tsx", "tsx"),
    ("/x/a.json", "json"),
    ("/x/A.JSON", "json"),
    ("/x/run.calls.jsonl", "jsonl"),
    ("/x/a.html", "html"),
    ("/x/a.css", "css"),
    ("/x/a.svg", "xml"),
    ("/x/a.yml", "yaml"),
    ("/x/a.sql", "sql"),
    ("/x/a.rs", "rust"),
    ("/x/a.go", "go"),
    ("/x/notes.txt", "plain"),
    ("/x/no-extension", "plain"),
    # Whole-basename matches outrank the extension.
    ("/x/Dockerfile", "dockerfile"),
    ("/x/CMakeLists.txt", "cmake"),
    ("/x/.env", "ini"),
    ("/x/.zshrc", "shell"),
    # Conventionally-commented JSON is never handed to the strict linter.
    ("/x/tsconfig.json", "jsonc"),
    ("/x/tsconfig.base.json", "jsonc"),
    ("/x/.vscode/settings.json", "jsonc"),
])
def test_language_detection(path, lang):
    assert ok(["language", path]) == [lang]


@pytest.mark.parametrize("text,unit", [
    ("def f():\n    if x:\n        y\n    return 1\n", "4"),
    ("{\n  \"a\": {\n    \"b\": 1\n  }\n}\n", "2"),
    ("func f() {\n\tif x {\n\t\ty()\n\t}\n}\n", "tab"),
    # One odd line does not decide it: the most common step does.
    ("a:\n  b: 1\n  c: 2\n  d:\n    e: 3\n   odd\n", "2"),
    # JSDoc's one-space " * " continuation is not an indent step.
    ("/**\n * doc\n */\nfunction f() {\n    return 1;\n}\n", "4"),
    ("flat\nfile\n", None),
])
def test_indent_detection(text, unit):
    assert ok(["indent", text]) == [unit]


def test_crlf_file_saves_back_as_crlf():
    # CodeMirror's default separator handling joins with "\n", which silently
    # rewrote a Windows file's every line break on its first save (CE-5).
    raw = "a\r\nb\r\n\r\nc\r\n"
    assert ok(["eol", raw], ["roundTrip", raw]) == ["crlf", raw]


def test_lf_file_saves_back_as_lf():
    raw = "a\nb\n"
    assert ok(["eol", raw], ["roundTrip", raw]) == ["lf", raw]


def test_mixed_line_endings_settle_on_the_majority():
    raw = "a\r\nb\r\nc\nd\r\n"
    assert ok(["eol", raw], ["roundTrip", raw]) == ["crlf", "a\r\nb\r\nc\r\nd\r\n"]


def test_format_json_changes_whitespace_only():
    # JSON.parse → JSON.stringify would print 1.0 as 1, 1e3 as 1000 and round
    # the big integer; every literal must come through byte for byte.
    src = '{"a":1.0,"big":12345678901234567890,"e":[],"o":{},"n":[1e3,true,null,"q\\"x"]}'
    (pretty,) = ok(["reformat", src, "  ", "\n"])
    assert pretty == (
        '{\n'
        '  "a": 1.0,\n'
        '  "big": 12345678901234567890,\n'
        '  "e": [],\n'
        '  "o": {},\n'
        '  "n": [\n'
        '    1e3,\n'
        '    true,\n'
        '    null,\n'
        '    "q\\"x"\n'
        '  ]\n'
        '}'
    )
    assert json.loads(pretty) == json.loads(src)


def test_format_json_compact_and_crlf():
    src = '{\n  "a" : [ 1 , 2 ],\n  "b": "x y"\n}'
    assert ok(["reformat", src, None, "\n"], ["reformat", src, "\t", "\r\n"]) == [
        '{"a":[1,2],"b":"x y"}',
        '{\r\n\t"a": [\r\n\t\t1,\r\n\t\t2\r\n\t],\r\n\t"b": "x y"\r\n}',
    ]


def test_format_refuses_invalid_json():
    (result,) = probe(["reformat", '{"a": }', "  ", "\n"])
    assert "error" in result


def test_jsonl_linter_flags_only_the_broken_records():
    doc = '{"a": 1}\n\n{bad}\n[1, 2]\n'
    (diags,) = ok(["lintJsonl", doc])
    assert len(diags) == 1
    assert diags[0]["from"] == doc.index("{bad}")
    assert diags[0]["to"] == doc.index("{bad}") + len("{bad}")
    assert diags[0]["message"].startswith("Line 3: ")
