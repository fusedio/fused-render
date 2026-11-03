"""Standalone templates cannot import the shared path helpers, so each carries a
local copy of the drive-root parent rule. These evaluate the REAL snippets out of
the template source with bun, so a drift in one copy fails here."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fused_render.server import templates as _server_templates

T = Path(_server_templates.TEMPLATES_DIR)
pytestmark = pytest.mark.skipif(shutil.which("bun") is None, reason="bun not installed")


def _eval(js: str):
    out = subprocess.run(["bun", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _function(src: str, name: str) -> str:
    m = re.search(r"^[ \t]*function " + name + r"\(.*?^[ \t]*}\n", src, re.S | re.M)
    assert m, name
    return m.group(0)


def test_notebook_dir_of_treats_backslash_as_separator_only_for_windows_paths():
    fn = _function((T / "notebook/template.html").read_text(), "dirOf")
    cases = ["/tmp/a\\b.ipynb", "C:\\notes.ipynb", "C:/x/y.ipynb", "\\\\srv\\share\\n.ipynb", "/n.ipynb"]
    got = _eval(fn + f"console.log(JSON.stringify({json.dumps(cases)}.map(dirOf)))")
    assert got == ["/tmp", "C:/", "C:/x", "\\\\srv\\share", "/"]


def test_excel_start_dir_keeps_the_drive_root_slash():
    src = (T / "excel/template.html").read_text()
    fn = _function(src, "startDir")
    wf = re.search(r"^const winFwd = .*$", src, re.M).group(0)
    js = (wf + fn + 'let currentFile = () => %s;'
          'console.log(JSON.stringify([startDir()]))')
    for file, want in [("C:\\x.xlsx", "C:/"), ("C:/a/x.xlsx", "C:/a"), ("/x.xlsx", "/"), ("", "~")]:
        assert _eval(js % json.dumps(file)) == [want]


def test_canvas_template_drive_root_dir_and_sibling_join():
    src = (T / "canvas/template.html").read_text()
    assert 'if (/^[A-Za-z]:$/.test(fileDir)) fileDir += "/";' in src
    assert 'fileDir.replace(/\\/$/, "") + "/" + n.udfName' in src
