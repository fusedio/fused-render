"""Read-only-file gating for the pdf_studio template (SPEC §13.5, RO-3).

pdf.py is a stdlib-at-import runPython target (pikepdf/pymupdf are lazy
imports), so — like test_annotate_comments.py — these load it via importlib and
drive `_save`/`main` directly. The write model: edits go to a working copy
under WORKDIR; only save/rename touch the ORIGINAL, and both do it via
parent-directory-level ops (`os.replace`, `os.rename`) that silently succeed on
a chmod -w file — hence the explicit `os.access(..., W_OK)` gate must raise
PermissionError BEFORE the operation. (Removing a doc only drops its library
reference and never touches the file on disk, so it needs no gate.)

The module keeps state dirs under ~/.fused-render at module level; every test
repoints those attributes at tmp_path so nothing touches the real home.
"""

import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PDF_PY = os.path.join(HERE, os.pardir, "fused_render", "templates", "pdf_studio", "pdf.py")


def _load_pdf(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pdf_studio_target", PDF_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Repoint every module-level state path into tmp_path (never the real home).
    data_root = str(tmp_path / "data")
    cache_root = str(tmp_path / "cache")
    monkeypatch.setattr(mod, "DATA_ROOT", data_root)
    monkeypatch.setattr(mod, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(mod, "LIBRARY", os.path.join(data_root, "library.json"))
    monkeypatch.setattr(mod, "DOWNLOADS", os.path.join(data_root, "downloads"))
    monkeypatch.setattr(mod, "EXPORTS", os.path.join(cache_root, "exports"))
    monkeypatch.setattr(mod, "SNAPSHOTS", os.path.join(cache_root, "snapshots"))
    monkeypatch.setattr(mod, "WORKDIR", os.path.join(cache_root, "work"))
    return mod


def _original(tmp_path, content=b"%PDF-original"):
    f = tmp_path / "docs" / "sample.pdf"
    f.parent.mkdir()
    f.write_bytes(content)
    return f


def test_save_readonly_original_raises_and_leaves_file(tmp_path, monkeypatch):
    mod = _load_pdf(tmp_path, monkeypatch)
    f = _original(tmp_path)
    src = str(f)
    # Fabricate an opened, dirty doc without pikepdf: working copy + state.
    os.makedirs(mod.WORKDIR, exist_ok=True)
    wpath, _ = mod._work_paths(src)
    with open(wpath, "wb") as out:
        out.write(b"%PDF-edited-working-copy")
    mod._work_save_state(
        src, {"src": src.replace(os.sep, "/"), "base_mtime": os.path.getmtime(src), "dirty": True}
    )
    os.chmod(src, 0o444)
    try:
        with pytest.raises(PermissionError):
            mod._save(src, force=0)
        assert f.read_bytes() == b"%PDF-original"  # untouched
        assert not os.path.exists(src + ".tmp")  # gated before the tmp copy
    finally:
        os.chmod(src, 0o644)


def test_save_readonly_beats_conflict_force(tmp_path, monkeypatch):
    # Even force=1 (the conflict dialog's override) can't write a read-only
    # file — the gate sits before the conflict check.
    mod = _load_pdf(tmp_path, monkeypatch)
    f = _original(tmp_path)
    src = str(f)
    os.makedirs(mod.WORKDIR, exist_ok=True)
    wpath, _ = mod._work_paths(src)
    with open(wpath, "wb") as out:
        out.write(b"%PDF-edited")
    mod._work_save_state(
        src,
        {
            "src": src.replace(os.sep, "/"),
            "base_mtime": 0.0,  # stale on purpose
            "dirty": True,
        },
    )
    os.chmod(src, 0o444)
    try:
        with pytest.raises(PermissionError):
            mod._save(src, force=1)
        assert f.read_bytes() == b"%PDF-original"
    finally:
        os.chmod(src, 0o644)


def test_rename_doc_readonly_raises_and_keeps_name(tmp_path, monkeypatch):
    mod = _load_pdf(tmp_path, monkeypatch)
    f = _original(tmp_path)
    os.chmod(f, 0o444)
    try:
        with pytest.raises(PermissionError):
            mod.main(action="rename_doc", doc=str(f), name="x")
        assert f.exists()
        assert not (f.parent / "x.pdf").exists()
    finally:
        os.chmod(f, 0o644)


def test_remove_from_library_readonly_keeps_file(tmp_path, monkeypatch):
    # Removing a doc only drops the library reference — the file on disk is
    # kept regardless of its permissions, so no RO gate is needed.
    mod = _load_pdf(tmp_path, monkeypatch)
    f = _original(tmp_path)
    mod.main(action="add_to_library", src=str(f))
    os.chmod(f, 0o444)
    try:
        out = mod.main(action="remove_from_library", doc=str(f))
        assert out["ok"] is True
        assert f.exists()
        assert mod._list_library()["docs"] == []
    finally:
        os.chmod(f, 0o644)


def test_rename_doc_writable_file_still_works(tmp_path, monkeypatch):
    # Sanity: the guard isn't over-broad — a 0o644 file renames fine.
    mod = _load_pdf(tmp_path, monkeypatch)
    f = _original(tmp_path)
    out = mod.main(action="rename_doc", doc=str(f), name="x")
    assert out["name"] == "x.pdf"
    assert (f.parent / "x.pdf").exists()
    assert not f.exists()
