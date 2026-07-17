"""Read-only file support in the docs template (SPEC §13.5).

docs.py is a runPython target, loaded here via importlib like
test_annotate_comments.py. The test venv has no pypandoc/pandoc, so every test
exercises only code paths that raise or return BEFORE any pandoc call:

- RO-3: the "save" action must gate on os.access(file, W_OK) and raise
  PermissionError before writing anything (its tmp + os.replace pipeline goes
  through the parent directory and would silently bypass a chmod -w file bit).
- RO-4: the reader verdict — extracted as the _editability() helper used by the
  "import" action — folds fs writability into editable/readonly_message/
  readonly_tooltip.
"""

import importlib.util
import os

import pytest


def _load_docs():
    path = os.path.join("fused_render", "templates", "docs", "docs.py")
    spec = importlib.util.spec_from_file_location("docs_target", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def docs():
    return _load_docs()


@pytest.fixture
def docx(tmp_path):
    """A fake .docx target; chmod is restored on teardown so tmp cleanup works."""
    f = tmp_path / "doc.docx"
    f.write_bytes(b"original-docx-bytes")
    yield str(f)
    os.chmod(str(f), 0o644)


def test_save_readonly_file_raises_permission_error(docs, docx):
    os.chmod(docx, 0o444)
    with pytest.raises(PermissionError):
        docs.main(action="save", file=docx, html="<p>hello</p>")
    # The guard fired before any tmp-write/os.replace: bytes untouched, no tmp left.
    assert open(docx, "rb").read() == b"original-docx-bytes"
    assert not os.path.exists(docx + ".tmp")


def test_editability_writable(docs, docx):
    editable, message, tooltip = docs._editability(docx)
    assert editable is True
    assert message == ""
    assert tooltip == ""


def test_editability_readonly(docs, docx):
    os.chmod(docx, 0o444)
    editable, message, tooltip = docs._editability(docx)
    assert editable is False
    assert message == "Read-only"
    assert "read-only" in tooltip
