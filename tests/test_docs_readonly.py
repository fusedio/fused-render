"""Read-only file support in the docs template (SPEC §13.5).

docs.py is a runPython target, loaded here via importlib like
test_annotate_revert.py. The test venv has no pypandoc/pandoc, so every test
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
import zipfile

import pytest

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


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


@skip_root
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


@skip_root
def test_editability_readonly(docs, docx):
    os.chmod(docx, 0o444)
    editable, message, tooltip = docs._editability(docx)
    assert editable is False
    assert message == "Read-only"
    assert "read-only" in tooltip


def _docx(path, *paragraphs):
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" if p else "<w:p/>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')
    return str(path)


def test_rename_keeps_extension_and_refuses_collisions(docs, tmp_path):
    src = _docx(tmp_path / "old.odt", "x")
    out = docs.main(action="rename", file=src, title='Q3: "plan".docx')
    assert out["name"] == "Q3 plan.odt" and not os.path.exists(src)
    assert docs.main(action="rename", file=out["path"], title="Q3 plan")["name"] == "Q3 plan.odt"
    _docx(tmp_path / "taken.odt", "y")
    with pytest.raises(FileExistsError):
        docs.main(action="rename", file=out["path"], title="taken")


def test_library_lists_drafts_by_first_line(docs, tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS_DIR", str(tmp_path))
    _docx(tmp_path / "Untitled document.docx", "", "Groceries &amp; more", "milk")
    _docx(tmp_path / "Untitled document 2.docx")
    _docx(tmp_path / "Budget.docx", "rent")
    (tmp_path / "Untitled document 3.docx").write_bytes(b"PK\x03\x04 truncated")
    titles = sorted(d["title"] for d in docs.main(action="list")["docs"])
    assert titles == ["Budget", "Groceries & more", "Untitled document 2", "Untitled document 3"]
