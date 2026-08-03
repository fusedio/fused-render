"""Backend for pdf_studio — a local PDF viewer/editor (merge, split, rotate,
compress, in-place text editing).

One bare `main(action=...)` dispatcher (the fused-render contract; bare on
purpose — see the note at the definition). pikepdf (qpdf) handles structural
ops, PyMuPDF handles text extraction/editing and rasterization. Every action
returns JSON so an AI agent can drive the whole app headlessly exactly as the
UI does.

The source of truth is the .pdf files on disk, wherever they live. A flat
library (a single JSON file of absolute paths) just remembers which files the
user added — it never copies them. Edits never touch the original directly:
each open doc gets a working copy under .work/ that mutations (and undo/redo
snapshots) apply to; an explicit save writes the working copy back over the
original. Each call is a fresh process, so no in-memory state survives.

Actions
  health                                   -> {ok, pymupdf, pikepdf}
  list_library                             -> {docs:[...]}
  add_to_library(src)                      -> {name, path}  (references src in place, no copy)
  remove_from_library(doc)                 -> {ok}  (drops the reference; file on disk is kept)
  import_url(url,name)                     -> {name, path}
  open_doc(doc)                            -> docinfo + {work, dirty, has_text, undo_depth, redo_depth}
  listdir(path)                            -> {path, parent, dirs, files}
  rename_doc(doc,name)                     -> {name, path}
  save(doc,force)                          -> {ok, file} or {conflict}
  revert(doc)                              -> mutation contract shape
  save_as(doc,directory,name)              -> {file}
  export(doc,kind,pages,name,directory)    -> {files:[{path,name,size}], dir}
  rotate_pages(doc,pages,degrees,expected_mtime)   \
  delete_pages(doc,pages,...)                       |  mutation contract:
  reorder_pages(doc,order,...)                      |  {ok, mtime, doc:docinfo,
  insert_blank(doc,at,width,height,...)             |   dirty, undo_depth, redo_depth}
  compress(doc,level,...)                           |  or {conflict, mtime}
  edit_text(doc,page,bbox,...,...)                 /
  extract_pages(doc,pages,name)            -> {name, path, size, dir}
  merge(sources,name,directory)            -> {name, path, dir}
  split(doc,mode,ranges,prefix,directory)  -> {files:[...], dir}
  protect(doc,password,owner)              -> {name, path, size, dir}  (encrypted copy)
  unlock(doc,password)                     -> {name, path, dir}  (decrypted copy)
  ocr(doc,pages,language)                  -> {name, path, dir, ocr_pages, copied_pages}
  pdf_to_word(doc,pages)                   -> {name, path, size, dir}
  word_to_pdf(src)                         -> {name, path, dir}
  excel_to_pdf(src)                        -> {name, path, dir}
  images_to_pdf(sources,name,directory)    -> {name, path, dir}  (image file paths)
  save_scan(images_b64,name,directory)     -> {name, path, dir}  (base64 captures)
  inspect(doc,pages)                       -> {doc, verdict, pages, fonts, text, security, rust}
  to_markdown(doc,pages)                   -> {markdown, chars, pages}
  save_markdown(doc,pages,directory,name)  -> {name, path, size, dir}
  reveal(path)                             -> {ok}  (opens the OS file explorer)
  page_text(doc,page)                      -> {width, height, rotation, spans:[...]}
  undo(doc) / redo(doc)                    -> mutation contract shape
"""
import hashlib
import json
import os
import re
import shutil

# NOTE: bare `def main` (no @fused.udf) is deliberate — under the built-in
# executor the worker calls main() by its own signature; @fused.udf hides that
# signature and triggers a hosted-auth flow that times out.

# State lives under the user home dir, never inside the installed template
# package (D76-adjacent). The library index and URL downloads hold primary,
# non-regenerable content so they get their own `data/` root rather than
# sitting under `cache/`, which a future clear-cache action could sweep;
# working copies and undo snapshots are transient and belong under `cache/`.
DATA_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "data", "pdf_studio"))
CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "pdf_studio"))
LIBRARY = os.path.join(DATA_ROOT, "library.json")   # flat list of PDF paths the user added
DOWNLOADS = os.path.join(DATA_ROOT, "downloads")    # PDFs fetched via import_url
EXPORTS = os.path.join(CACHE_ROOT, "exports")
SNAPSHOTS = os.path.join(CACHE_ROOT, "snapshots")   # undo stacks, keyed by doc path
WORKDIR = os.path.join(CACHE_ROOT, "work")          # per-doc working copies (unsaved edits)

UNDO_CAP = 10


# ---------------------------------------------------------------------- helpers
def _safe_name(name, default):
    name = re.sub(r'[\\/:*?"<>|]', "-", (name or "").strip()).strip(". ")
    return name or default


def _fwd(p: str) -> str:
    return p.replace(os.sep, "/")


def _out_dir(directory, default):
    d = os.path.abspath(os.path.expanduser(directory)) if directory else default
    if not os.path.isdir(d):
        raise ValueError(f"no such folder: {d}")
    return d


def _unique_path(directory, name):
    stem, ext = os.path.splitext(name)
    dest = os.path.join(directory, name)
    i = 2
    while os.path.exists(dest):
        dest = os.path.join(directory, f"{stem}-{i}{ext}")
        i += 1
    return dest


def _parse_pages(spec: str, n: int):
    """'all' | '' | '3' | '1,3-5' (1-based) -> sorted unique 0-based indices."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "all":
        return list(range(n))
    out = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if not m:
            raise ValueError(f"bad page spec: {tok!r} (use e.g. 2 or 1,3-5)")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b > n or a > b:
            raise ValueError(f"page range {tok} outside 1-{n}")
        out.update(range(a - 1, b))
    if not out:
        raise ValueError("empty page selection")
    return sorted(out)


def _docinfo(path):
    import fitz

    path = os.path.abspath(path)
    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        return {"path": _fwd(path), "name": os.path.basename(path),
                "size": os.path.getsize(path), "mtime": os.path.getmtime(path),
                "encrypted": True, "page_count": 0, "pages": []}
    pages = [{"n": i + 1, "width": round(p.rect.width, 2),
              "height": round(p.rect.height, 2), "rotation": p.rotation}
             for i, p in enumerate(doc)]
    out = {"path": _fwd(path), "name": os.path.basename(path),
           "size": os.path.getsize(path), "mtime": os.path.getmtime(path),
           "encrypted": False, "page_count": doc.page_count, "pages": pages}
    doc.close()
    return out


# ------------------------------------------------------------- working copies
def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _work_paths(doc):
    key = hashlib.sha1(os.path.realpath(os.path.abspath(doc)).encode()).hexdigest()[:16]
    return os.path.join(WORKDIR, key + ".pdf"), os.path.join(WORKDIR, key + ".json")


def _work_state(doc):
    _, mpath = _work_paths(doc)
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            return json.load(f)
    return None


def _work_save_state(doc, meta):
    _, mpath = _work_paths(doc)
    tmp = mpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, mpath)


def _open_work(doc):
    """The doc's working copy: resumed while it holds unsaved changes (or the
    original is unchanged), refreshed from the original otherwise."""
    src = os.path.abspath(doc)
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {src}")
    os.makedirs(WORKDIR, exist_ok=True)
    wpath, _ = _work_paths(src)
    meta = _work_state(src)
    smt = os.path.getmtime(src)
    if not (meta and os.path.exists(wpath)
            and (meta.get("dirty") or meta.get("base_mtime") == smt)):
        shutil.copyfile(src, wpath)
        meta = {"src": _fwd(src), "base_mtime": smt, "dirty": False}
        _work_save_state(src, meta)
    return wpath, meta


def _mark_dirty(doc):
    meta = _work_state(doc)
    if meta:
        meta["dirty"] = True
        _work_save_state(doc, meta)


def _cur_path(doc):
    """Where the doc's current content lives: the working copy while it has
    unsaved changes, the original otherwise."""
    wpath, _ = _work_paths(doc)
    meta = _work_state(doc)
    if meta and meta.get("dirty") and os.path.exists(wpath):
        return wpath
    return os.path.abspath(doc)


def _work_drop(doc):
    for p in _work_paths(doc):
        try:
            os.remove(p)
        except OSError:
            pass


def _work_rename(old, new):
    meta = _work_state(old)
    if not meta:
        return
    ow, om = _work_paths(old)
    nw, _ = _work_paths(new)
    if os.path.exists(ow):
        os.replace(ow, nw)
    meta["src"] = _fwd(os.path.abspath(new))
    _work_save_state(new, meta)
    os.remove(om)


def _save(doc, force):
    src = os.path.abspath(doc)
    wpath, _ = _work_paths(src)
    meta = _work_state(src)
    if not (meta and os.path.exists(wpath)):
        raise ValueError("open the document first")
    if not meta.get("dirty"):
        return {"ok": True, "dirty": False, "file": _fwd(src), "unchanged": True}
    # RO gate (SPEC §13.5 RO-3) before the conflict check: the write below goes
    # through the parent directory (`os.replace`) and would silently overwrite
    # a chmod -w original — and the conflict dialog's "force" must not either.
    if os.path.isfile(src) and not os.access(src, os.W_OK):
        raise PermissionError(f"{src!r} is read-only")
    if not force and os.path.isfile(src) and os.path.getmtime(src) != meta["base_mtime"]:
        return {"conflict": True}
    tmp = src + ".tmp"
    shutil.copyfile(wpath, tmp)
    os.replace(tmp, src)
    meta["base_mtime"] = os.path.getmtime(src)
    meta["dirty"] = False
    _work_save_state(src, meta)
    return {"ok": True, "dirty": False, "file": _fwd(src)}


def _revert(doc):
    src = os.path.abspath(doc)
    os.makedirs(WORKDIR, exist_ok=True)
    wpath, _ = _work_paths(src)
    shutil.copyfile(src, wpath)
    _work_save_state(src, {"src": _fwd(src), "base_mtime": os.path.getmtime(src),
                           "dirty": False})
    return _mut_result(src)


# --------------------------------------------------------- undo/redo snapshots
def _hist_dir(doc):
    doc = os.path.realpath(os.path.abspath(doc))
    d = os.path.join(SNAPSHOTS, hashlib.sha1(doc.encode()).hexdigest()[:16])
    os.makedirs(d, exist_ok=True)
    return d


def _stack_load(hist):
    p = os.path.join(hist, "stack.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"undo": [], "redo": [], "seq": 0}


def _stack_save(hist, stack):
    p = os.path.join(hist, "stack.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stack, f)
    os.replace(tmp, p)


def _stack_depths(doc):
    stack = _stack_load(_hist_dir(doc))
    return len(stack["undo"]), len(stack["redo"])


def _push_snapshot(doc, op, pre):
    hist = _hist_dir(doc)
    stack = _stack_load(hist)
    stack["seq"] += 1
    name = f"{stack['seq']:04d}-{op}.pdf"
    shutil.move(pre, os.path.join(hist, name))
    stack["undo"].append(name)
    for r in stack["redo"]:
        try:
            os.remove(os.path.join(hist, r))
        except OSError:
            pass
    stack["redo"] = []
    while len(stack["undo"]) > UNDO_CAP:
        old = stack["undo"].pop(0)
        try:
            os.remove(os.path.join(hist, old))
        except OSError:
            pass
    _stack_save(hist, stack)


def _restore(doc, snap):
    tmp = doc + ".tmp"
    shutil.copyfile(snap, tmp)
    os.replace(tmp, doc)


def _undo(doc):
    doc = os.path.abspath(doc)
    wpath, _ = _open_work(doc)
    hist = _hist_dir(doc)
    stack = _stack_load(hist)
    if not stack["undo"]:
        raise ValueError("nothing to undo")
    name = stack["undo"].pop()
    stack["seq"] += 1
    redo_name = f"{stack['seq']:04d}-redo.pdf"
    shutil.copyfile(wpath, os.path.join(hist, redo_name))
    stack["redo"].append(redo_name)
    snap = os.path.join(hist, name)
    _restore(wpath, snap)
    os.remove(snap)
    _stack_save(hist, stack)
    _mark_dirty(doc)
    return _mut_result(doc)


def _redo(doc):
    doc = os.path.abspath(doc)
    wpath, _ = _open_work(doc)
    hist = _hist_dir(doc)
    stack = _stack_load(hist)
    if not stack["redo"]:
        raise ValueError("nothing to redo")
    name = stack["redo"].pop()
    stack["seq"] += 1
    undo_name = f"{stack['seq']:04d}-undo.pdf"
    shutil.copyfile(wpath, os.path.join(hist, undo_name))
    stack["undo"].append(undo_name)
    snap = os.path.join(hist, name)
    _restore(wpath, snap)
    os.remove(snap)
    _stack_save(hist, stack)
    _mark_dirty(doc)
    return _mut_result(doc)


def _mut_result(doc, extra=None):
    doc = os.path.abspath(doc)
    wpath, _ = _work_paths(doc)
    meta = _work_state(doc) or {}
    info = _docinfo(wpath)
    info["path"] = _fwd(doc)
    info["name"] = os.path.basename(doc)
    info["work"] = _fwd(wpath)
    out = {"ok": True, "mtime": os.path.getmtime(wpath), "doc": info,
           "dirty": bool(meta.get("dirty"))}
    out["undo_depth"], out["redo_depth"] = _stack_depths(doc)
    out.update(extra or {})
    return out


def _mutate(doc, expected_mtime, op, fn):
    """Conflict-check -> fn mutates the WORKING copy in place -> fresh docinfo.
    The original file is untouched until an explicit save. The pre-mutation
    copy only lands on the undo stack if fn succeeds, so a failed op never
    pollutes undo. fn may return extra keys for the response."""
    doc = os.path.abspath(doc)
    wpath, _ = _open_work(doc)
    if expected_mtime:
        cur = os.path.getmtime(wpath)
        if abs(cur - float(expected_mtime)) > 1e-6:
            return {"conflict": True, "mtime": cur}
    pre = wpath + ".pre"
    shutil.copyfile(wpath, pre)
    try:
        extra = fn(wpath)
    except BaseException:
        os.remove(pre)
        raise
    _push_snapshot(doc, op, pre)
    _mark_dirty(doc)
    return _mut_result(doc, extra)


# ------------------------------------------------------------------- page ops
def _rotate_pages(doc, pages, degrees):
    import pikepdf

    def fn(path):
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            for i in _parse_pages(pages, len(pdf.pages)):
                pdf.pages[i].rotate(degrees, relative=True)
            pdf.save(path)
    return fn(doc)


def _delete_pages(doc, pages):
    import pikepdf

    def fn(path):
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            idxs = _parse_pages(pages, len(pdf.pages))
            if len(idxs) >= len(pdf.pages):
                raise ValueError("cannot delete every page")
            for i in reversed(idxs):
                del pdf.pages[i]
            pdf.save(path)
    return fn(doc)


def _reorder_pages(doc, order):
    import pikepdf

    def fn(path):
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            n = len(pdf.pages)
            idxs = [int(t) - 1 for t in order.split(",") if t.strip()]
            if sorted(idxs) != list(range(n)):
                raise ValueError(f"order must be a permutation of 1-{n}")
            for i in idxs:
                pdf.pages.append(pdf.pages[i])
            del pdf.pages[0:n]
            pdf.save(path)
    return fn(doc)


def _insert_blank(doc, at, width, height):
    import fitz

    def fn(path):
        d = fitz.open(path)
        pno = min(max(at - 1, 0), d.page_count)
        w = float(width) if width else (d[0].rect.width if d.page_count else 612)
        h = float(height) if height else (d[0].rect.height if d.page_count else 792)
        d.new_page(pno=pno, width=w, height=h)
        tmp = path + ".tmp"
        d.save(tmp, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        d.close()
        os.replace(tmp, path)
    return fn(doc)


def _extract_pages(doc, pages, name):
    import pikepdf

    doc = os.path.abspath(doc)
    with pikepdf.open(_cur_path(doc)) as src:
        idxs = _parse_pages(pages, len(src.pages))
        dst = pikepdf.Pdf.new()
        for i in idxs:
            dst.pages.append(src.pages[i])
        stem = os.path.splitext(os.path.basename(doc))[0]
        name = _safe_name(name, f"{stem}-extract")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = _unique_path(os.path.dirname(doc), name)
        dst.save(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "size": os.path.getsize(dest), "dir": _fwd(os.path.dirname(dest))}


def _merge(sources, name, directory=""):
    import pikepdf

    paths = [os.path.abspath(p) for p in json.loads(sources)]
    if len(paths) < 2:
        raise ValueError("merge needs at least two PDFs")
    out_dir = _out_dir(directory, os.path.dirname(paths[0]))
    dst = pikepdf.Pdf.new()
    for p in paths:
        with pikepdf.open(_cur_path(p)) as src:
            dst.pages.extend(src.pages)
    name = _safe_name(name, "merged")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    dest = _unique_path(out_dir, name)
    dst.save(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest), "dir": _fwd(out_dir)}


def _split(doc, mode, ranges, prefix, directory=""):
    import pikepdf

    doc = os.path.abspath(doc)
    out_dir = _out_dir(directory, os.path.dirname(doc))
    stem = os.path.splitext(os.path.basename(doc))[0]
    prefix = _safe_name(prefix, stem)
    files = []
    with pikepdf.open(_cur_path(doc)) as src:
        n = len(src.pages)
        if mode == "each":
            groups = [[i] for i in range(n)]
            if len(groups) > 200:
                raise ValueError(f"{n} pages is too many to split one-per-file; use ranges")
        else:
            groups = [_parse_pages(r.strip(), n) for r in ranges.split(";") if r.strip()]
            if not groups:
                raise ValueError("split needs ranges like 1-3;4-6")
        for g in groups:
            dst = pikepdf.Pdf.new()
            for i in g:
                dst.pages.append(src.pages[i])
            label = f"p{g[0] + 1}" if len(g) == 1 else f"p{g[0] + 1}-{g[-1] + 1}"
            dest = _unique_path(out_dir, f"{prefix}-{label}.pdf")
            dst.save(dest)
            files.append({"name": os.path.basename(dest), "path": _fwd(dest),
                          "size": os.path.getsize(dest)})
    return {"files": files, "dir": _fwd(out_dir)}


def _compress(doc, level):
    import pikepdf

    before = os.path.getsize(doc)

    def fn(path):
        tmp = path + ".tmp"
        if level == "aggressive":
            import fitz

            if before > 80 * 1024 * 1024:
                raise ValueError("file too large for aggressive compression — use lossless")
            d = fitz.open(path)
            d.rewrite_images(dpi_threshold=200, dpi_target=150, quality=75)
            d.subset_fonts()
            d.save(tmp, garbage=4, deflate=True, clean=True,
                   encryption=fitz.PDF_ENCRYPT_KEEP)
            d.close()
        else:
            with pikepdf.open(path) as pdf:
                pdf.save(tmp, compress_streams=True, recompress_flate=True,
                         object_stream_mode=pikepdf.ObjectStreamMode.generate)
        after = os.path.getsize(tmp)
        # Recompression can GROW a file that is already well packed — keep the
        # working copy untouched instead of recording a pointless mutation.
        if after >= before:
            os.remove(tmp)
            raise ValueError(
                f"already optimized — {level} compression would not shrink the file")
        os.replace(tmp, path)
        return {"before": before, "after": after}
    return fn(doc)


# ------------------------------------------------------- protect / unlock
def _protect(doc, password, owner):
    """Encrypted (AES-256) copy next to the original; the original is untouched."""
    import pikepdf

    doc = os.path.abspath(doc)
    if not password:
        raise ValueError("a password is required")
    stem = os.path.splitext(os.path.basename(doc))[0]
    dest = _unique_path(os.path.dirname(doc), f"{stem}-protected.pdf")
    with pikepdf.open(_cur_path(doc)) as pdf:
        pdf.save(dest, encryption=pikepdf.Encryption(
            user=password, owner=owner or password, R=6))
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "size": os.path.getsize(dest), "dir": _fwd(os.path.dirname(dest))}


def _unlock(doc, password):
    """Decrypted copy next to the original (never strips in place)."""
    import pikepdf

    doc = os.path.abspath(doc)
    stem = os.path.splitext(os.path.basename(doc))[0]
    dest = _unique_path(os.path.dirname(doc), f"{stem}-unlocked.pdf")
    try:
        with pikepdf.open(doc, password=password or "") as pdf:
            pdf.save(dest)
    except pikepdf.PasswordError:
        raise ValueError("wrong password") from None
    _add_to_library(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "dir": _fwd(os.path.dirname(dest))}


# ------------------------------------------------------------------------ ocr
# PyMuPDF wheels ship MuPDF's embedded Tesseract; only the language model file
# is needed. It is fetched once into DATA_ROOT (tessdata_fast, ~4 MB for eng).
TESSDATA = os.path.join(DATA_ROOT, "tessdata")
_TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"


def _ensure_tessdata(lang):
    import urllib.request

    if not re.fullmatch(r"[a-z_]{3,12}", lang or ""):
        raise ValueError(f"bad OCR language code: {lang!r}")
    os.makedirs(TESSDATA, exist_ok=True)
    p = os.path.join(TESSDATA, f"{lang}.traineddata")
    if os.path.exists(p):
        return TESSDATA
    req = urllib.request.Request(_TESSDATA_URL.format(lang=lang),
                                 headers={"User-Agent": "fused-render-pdf/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001 — surface any download failure as one message
        raise ValueError(f"could not download OCR data for {lang!r} ({exc}) — "
                         "check the connection and retry") from None
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return TESSDATA


def _ocr(doc, pages, language):
    """Searchable copy next to the original: pages that already have text are
    copied through; image-only pages get an invisible OCR text layer."""
    import fitz

    doc = os.path.abspath(doc)
    lang = (language or "eng").strip() or "eng"
    tessdata = _ensure_tessdata(lang)
    src = fitz.open(_cur_path(doc))
    idxs = _parse_pages(pages, src.page_count)
    todo = [i for i in idxs if not src[i].get_text().strip()] if not pages else idxs
    if not todo:
        src.close()
        raise ValueError("every selected page already has a text layer — "
                         "pass explicit pages to re-OCR them")
    if len(todo) > 100:
        src.close()
        raise ValueError(f"{len(todo)} pages need OCR — run it in batches (e.g. 1-100)")
    out = fitz.open()
    todo_set = set(todo)
    for i in range(src.page_count):
        if i not in todo_set:
            out.insert_pdf(src, from_page=i, to_page=i)
            continue
        pix = src[i].get_pixmap(dpi=200)
        page_pdf = fitz.open("pdf", pix.pdfocr_tobytes(
            compress=True, language=lang, tessdata=tessdata))
        out.insert_pdf(page_pdf)
        page_pdf.close()
    stem = os.path.splitext(os.path.basename(doc))[0]
    dest = _unique_path(os.path.dirname(doc), f"{stem}-searchable.pdf")
    out.save(dest, garbage=3, deflate=True)
    out.close()
    copied = src.page_count - len(todo)
    src.close()
    _add_to_library(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "dir": _fwd(os.path.dirname(dest)),
            "ocr_pages": len(todo), "copied_pages": copied}


# ----------------------------------------------------------- word / excel
_XML_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _xesc(s):
    return "".join(_XML_ESC.get(c, c) for c in s)


def _pdf_to_word(doc, pages):
    """Text-level .docx next to the original (stdlib OOXML writer — bold/italic/
    size survive, exact layout does not; Word reflows the paragraphs)."""
    import zipfile

    import fitz

    doc = os.path.abspath(doc)
    d = fitz.open(_cur_path(doc))
    idxs = _parse_pages(pages, d.page_count)
    body = []
    for k, i in enumerate(idxs):
        if k:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        for block in d[i].get_text("dict")["blocks"]:
            runs = []
            for line in block.get("lines", []):
                for s in line.get("spans", []):
                    txt = s["text"]
                    if not txt:
                        continue
                    props = []
                    if s["flags"] & 16:
                        props.append("<w:b/>")
                    if s["flags"] & 2:
                        props.append("<w:i/>")
                    props.append(f'<w:sz w:val="{max(2, round(s["size"] * 2))}"/>')
                    runs.append(f'<w:r><w:rPr>{"".join(props)}</w:rPr>'
                                f'<w:t xml:space="preserve">{_xesc(txt)}</w:t></w:r>')
                runs.append('<w:r><w:t xml:space="preserve"> </w:t></w:r>')
            if runs:
                body.append(f"<w:p>{''.join(runs[:-1])}</w:p>")
    d.close()
    if not body:
        raise ValueError("no text found on the selected pages — "
                         "run Make searchable (OCR) first if this is a scan")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>")
    stem = os.path.splitext(os.path.basename(doc))[0]
    dest = _unique_path(os.path.dirname(doc), f"{stem}.docx")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>")
        z.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>")
        z.writestr("word/document.xml", document)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "size": os.path.getsize(dest), "dir": _fwd(os.path.dirname(dest))}


def _find_soffice():
    """LibreOffice, when installed — the max-fidelity converter for office docs."""
    cand = shutil.which("soffice")
    if cand:
        return cand
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice",
              "/usr/bin/soffice"):
        if os.path.isfile(p):
            return p
    return ""


def _soffice_to_pdf(src, out_dir):
    import subprocess

    soffice = _find_soffice()
    if not soffice:
        return ""
    before = set(os.listdir(out_dir))
    subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                    "--outdir", out_dir, src],
                   check=True, timeout=180, capture_output=True)
    made = [f for f in os.listdir(out_dir)
            if f not in before and f.lower().endswith(".pdf")]
    return os.path.join(out_dir, made[0]) if made else ""


_STORY_CSS = ("body{font-family:sans-serif;font-size:11pt;line-height:1.45}"
              "h2{font-size:14pt;margin:14pt 0 6pt}"
              "table{border-collapse:collapse;font-size:9pt}"
              "td,th{border:0.5pt solid #999;padding:2pt 5pt}"
              "th{background-color:#eee;font-weight:bold}")


def _html_to_pdf(html, dest):
    import fitz

    story = fitz.Story(html=html, user_css=_STORY_CSS)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (36, 36, -36, -36)
    writer = fitz.DocumentWriter(dest)
    while True:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        if not more:
            break
    writer.close()


def _word_to_pdf(src):
    """PDF copy next to the .docx: LibreOffice when installed (full fidelity),
    else a stdlib docx parse laid out with fitz.Story (text + tables)."""
    import tempfile
    import zipfile
    import xml.etree.ElementTree as ET

    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {src}")
    out_dir = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    dest = _unique_path(out_dir, f"{stem}.pdf")
    with tempfile.TemporaryDirectory() as td:
        made = _soffice_to_pdf(src, td)
        if made:
            shutil.copyfile(made, dest)
            _add_to_library(dest)
            return {"name": os.path.basename(dest), "path": _fwd(dest),
                    "dir": _fwd(out_dir), "via": "libreoffice"}
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(src) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("not a .docx file (older .doc needs LibreOffice installed)") from None

    def para_html(p):
        style = p.find(f"{W}pPr/{W}pStyle")
        heading = style is not None and "eading" in (style.get(f"{W}val") or "")
        parts = []
        for r in p.iter(f"{W}r"):
            txt = "".join(t.text or "" for t in r.iter(f"{W}t"))
            if not txt:
                continue
            rpr = r.find(f"{W}rPr")
            if rpr is not None and rpr.find(f"{W}b") is not None:
                txt = f"<b>{_xesc(txt)}</b>"
            elif rpr is not None and rpr.find(f"{W}i") is not None:
                txt = f"<i>{_xesc(txt)}</i>"
            else:
                txt = _xesc(txt)
            parts.append(txt)
        inner = "".join(parts) or "&#160;"
        return f"<h2>{inner}</h2>" if heading else f"<p>{inner}</p>"

    chunks = []
    for el in root.find(f"{W}body"):
        if el.tag == f"{W}p":
            chunks.append(para_html(el))
        elif el.tag == f"{W}tbl":
            rows = []
            for tr in el.iter(f"{W}tr"):
                cells = ["<td>" + _xesc(" ".join(
                    "".join(t.text or "" for t in p.iter(f"{W}t"))
                    for p in tc.iter(f"{W}p")).strip()) + "</td>"
                    for tc in tr.findall(f"{W}tc")]
                rows.append(f"<tr>{''.join(cells)}</tr>")
            chunks.append(f"<table>{''.join(rows)}</table>")
    if not chunks:
        raise ValueError("the document has no readable content")
    _html_to_pdf(f"<body>{''.join(chunks)}</body>", dest)
    _add_to_library(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "dir": _fwd(out_dir), "via": "builtin"}


def _excel_to_pdf(src):
    """PDF copy next to the workbook: LibreOffice when installed, else
    openpyxl values rendered as one table per sheet with fitz.Story."""
    import tempfile

    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {src}")
    out_dir = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    dest = _unique_path(out_dir, f"{stem}.pdf")
    with tempfile.TemporaryDirectory() as td:
        made = _soffice_to_pdf(src, td)
        if made:
            shutil.copyfile(made, dest)
            _add_to_library(dest)
            return {"name": os.path.basename(dest), "path": _fwd(dest),
                    "dir": _fwd(out_dir), "via": "libreoffice"}
    try:
        import openpyxl
    except ImportError:
        raise ValueError("Excel conversion needs openpyxl (or LibreOffice) — "
                         "uv pip install openpyxl") from None
    wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
    chunks = []
    for ws in wb.worksheets:
        rows = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= 2000:
                rows.append("<tr><td>… truncated at 2000 rows</td></tr>")
                break
            tag = "th" if r == 0 else "td"
            cells = "".join(f"<{tag}>{_xesc('' if v is None else str(v))}</{tag}>"
                            for v in row)
            rows.append(f"<tr>{cells}</tr>")
        if rows:
            chunks.append(f"<h2>{_xesc(ws.title)}</h2><table>{''.join(rows)}</table>")
    wb.close()
    if not chunks:
        raise ValueError("the workbook has no data to render")
    _html_to_pdf(f"<body>{''.join(chunks)}</body>", dest)
    _add_to_library(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "dir": _fwd(out_dir), "via": "builtin"}


# --------------------------------------------------------- images / scanning
def _build_image_pdf(blobs, name, directory, default_dir):
    import fitz

    if not blobs:
        raise ValueError("no images to save")
    out_dir = _out_dir(directory, default_dir)
    out = fitz.open()
    for data in blobs:
        pix = fitz.Pixmap(data)
        w = 595.0  # A4 width in points; height keeps the capture's aspect
        h = w * pix.height / max(1, pix.width)
        page = out.new_page(width=w, height=h)
        page.insert_image(page.rect, stream=data)
    name = _safe_name(name, "scan")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    dest = _unique_path(out_dir, name)
    out.save(dest, deflate=True)
    out.close()
    _add_to_library(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest), "dir": _fwd(out_dir)}


def _images_to_pdf(sources, name, directory):
    paths = [os.path.abspath(p) for p in json.loads(sources)]
    for p in paths:
        if not os.path.isfile(p):
            raise ValueError(f"no such file: {p}")
    blobs = []
    for p in paths:
        with open(p, "rb") as f:
            blobs.append(f.read())
    default_dir = os.path.dirname(paths[0]) if paths else DOWNLOADS
    if not name:
        name = os.path.splitext(os.path.basename(paths[0]))[0] if paths else "images"
    return _build_image_pdf(blobs, name, directory, default_dir)


def _save_scan(images_b64, name, directory):
    import base64

    blobs = [base64.b64decode(b.split(",", 1)[-1]) for b in json.loads(images_b64)]
    os.makedirs(DOWNLOADS, exist_ok=True)
    return _build_image_pdf(blobs, name or "scan", directory, DOWNLOADS)


# ------------------------------------------------------------------ text edit
_CJK_FONTS = ((0x4E00, 0x9FFF, "china-s"), (0x3040, 0x30FF, "japan"),
              (0xAC00, 0xD7AF, "korea"))


def _pick_font(fontname, flags, text):
    for lo, hi, fam in _CJK_FONTS:
        if any(lo <= ord(c) <= hi for c in text):
            return fam
    name = (fontname or "").lower()
    bold = bool(flags & 16) or "bold" in name
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    if flags & 8 or "mono" in name or "courier" in name:
        base = ["cour", "coit", "cobo", "cobi"]
    elif flags & 4 or "times" in name or "serif" in name:
        base = ["tiro", "tiit", "tibo", "tibi"]
    else:
        base = ["helv", "heit", "hebo", "hebi"]
    return base[(2 if bold else 0) + (1 if italic else 0)]


def _norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _page_text(doc, page):
    import fitz

    d = fitz.open(doc)
    if page < 1 or page > d.page_count:
        raise ValueError(f"no page {page}")
    p = d[page - 1]
    spans = []
    for block in p.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line.get("spans", []):
                txt = s["text"]
                if not txt.strip():
                    continue
                c = s["color"]
                spans.append({
                    "text": txt,
                    "bbox": [round(v, 2) for v in s["bbox"]],
                    "origin": [round(v, 2) for v in s["origin"]],
                    "font": s["font"], "size": round(s["size"], 2),
                    "flags": s["flags"],
                    "color": [(c >> 16) & 255, (c >> 8) & 255, c & 255],
                })
    out = {"page": page, "width": round(p.rect.width, 2),
           "height": round(p.rect.height, 2), "rotation": p.rotation,
           "spans": spans, "mtime": os.path.getmtime(doc)}
    d.close()
    return out


def _edit_text(doc, page, bbox, origin, old_text, new_text, font, size, flags, color):
    import fitz

    def fn(path):
        d = fitz.open(path)
        p = d[page - 1]
        if p.rotation != 0:
            raise ValueError("text editing on rotated pages isn't supported — "
                             "rotate the page to 0° first")
        rect = fitz.Rect(*json.loads(bbox))
        got = _norm_ws(p.get_text("text", clip=rect + (-1, -1, 1, 1)))
        if _norm_ws(old_text) not in got:
            raise ValueError("the page text changed on disk — reload and retry")
        fname = _pick_font(font, int(flags or 0), new_text)
        fsize = float(size or 11)
        if new_text:
            while fsize > 6 and fitz.get_text_length(
                    new_text, fontname=fname, fontsize=fsize) > rect.width + 2:
                fsize -= 0.25
        p.add_redact_annot(rect)
        p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                           graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        if new_text:
            ox, oy = json.loads(origin)
            col = [c / 255 for c in json.loads(color or "[0,0,0]")]
            p.insert_text((ox, oy), new_text, fontname=fname, fontsize=fsize,
                          color=col)
        d.save(path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        d.close()
        return {"used_font": fname, "used_size": round(fsize, 2)}
    return fn(doc)


# -------------------------------------------------------------------- library
def _doc_entry(path):
    entry = {"name": os.path.basename(path), "path": _fwd(path),
             "size": os.path.getsize(path), "mtime": os.path.getmtime(path)}
    try:
        import pikepdf
        with pikepdf.open(_cur_path(path)) as pdf:
            entry["page_count"] = len(pdf.pages)
    except Exception as e:
        entry["page_count"] = None
        entry["error"] = str(e)
    return entry


def _lib_load():
    if os.path.exists(LIBRARY):
        try:
            with open(LIBRARY, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("paths") or []
        except Exception:
            pass
    return []


def _lib_save(paths):
    os.makedirs(DATA_ROOT, exist_ok=True)
    tmp = LIBRARY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"paths": paths}, f)
    os.replace(tmp, LIBRARY)


def _list_library():
    docs = []
    for path in _lib_load():
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            docs.append({"name": os.path.basename(full), "path": _fwd(full),
                         "size": 0, "mtime": 0, "page_count": None, "missing": True})
            continue
        docs.append(_doc_entry(full))
    docs.sort(key=lambda e: e["name"].lower())
    return {"docs": docs}


def _add_to_library(src):
    """Remember src in the library — the file stays where it is, never copied."""
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {src}")
    paths = _lib_load()
    if not any(_same_path(p, src) for p in paths):
        _lib_save(paths + [_fwd(src)])
    return {"name": os.path.basename(src), "path": _fwd(src)}


def _remove_from_library(doc):
    p = os.path.abspath(doc)
    paths = _lib_load()
    kept = [x for x in paths if not _same_path(x, p)]
    if len(kept) != len(paths):
        _lib_save(kept)
    shutil.rmtree(_hist_dir(p), ignore_errors=True)
    _work_drop(p)
    return {"ok": True}


def _import_url(url, name):
    import urllib.request

    os.makedirs(DOWNLOADS, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "fused-render-pdf/1.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read()
    if data[:5] != b"%PDF-":
        raise ValueError("URL did not return a PDF")
    name = _safe_name(name or os.path.basename(url.split("?")[0]), "imported")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    dest = _unique_path(DOWNLOADS, name)
    with open(dest, "wb") as f:
        f.write(data)
    _add_to_library(dest)
    return {"name": os.path.basename(dest), "path": _fwd(dest)}


# --- mount-safe directory listing ------------------------------------------
# A kernel listing (os.listdir/os.scandir/os.walk) on a path under a remote
# rclone NFS mount forces rclone to enumerate the ENTIRE parent S3 prefix and
# can DROP the mount, wedging the server. This template stays mount-AGNOSTIC:
# it never imports shell.mounts and never matches mount paths. Instead the UI
# passes a server origin (as the `src` param on the listdir action) and we ask
# the server whether a path is remote (/api/fs/stat); if so we list it via the
# mount-routed, paginated /api/fs/list — never through the kernel. _server_url +
# _stat are copied verbatim from pyramid/overview_pyramid.py.
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(origin, endpoint, path):
    u = _urlparse.urlsplit(origin)
    return (f"{u.scheme}://{u.netloc}{endpoint}?path="
            + _urlparse.quote(path))


def _stat(origin, path):
    url = _server_url(origin, "/api/fs/stat", path)
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            return ("ok", json.load(r))
    except _urlerr.HTTPError as e:
        if e.code == 404:
            return ("missing", None)
        return ("unreachable", None)
    except Exception:  # noqa: BLE001 — any network error -> fall back to local
        return ("unreachable", None)


def _remote_dir(origin, path):
    """True iff the server says `path` is a remote (mount-backed) directory.
    No origin / unreachable / missing -> False (presume local, kernel OK)."""
    if not origin or not path:
        return False
    status, meta = _stat(origin, path)
    return status == "ok" and bool(meta.get("remote"))


def _list_remote(origin, path, cap=5000):
    """List `path` via the server's mount-routed, paginated /api/fs/list — never
    the kernel. Follows the cursor up to `cap` entries so a huge S3 prefix
    returns a bounded page set instead of tripping the NFS deadman."""
    entries, cursor, truncated = [], "", False
    while True:
        url = _server_url(origin, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + _urlparse.quote(cursor)
        with _urlreq.urlopen(url, timeout=30) as r:
            payload = json.load(r)
        entries.extend(payload.get("entries") or [])
        truncated = bool(payload.get("truncated"))
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not truncated or not cursor:
            break
    return entries, truncated


def _listdir(path, origin="", exts=""):
    want = tuple(e.strip().lower() for e in (exts or ".pdf").split(",") if e.strip())
    path = os.path.abspath(os.path.expanduser(path or "~"))
    # Ask the server once: is this a remote (mount-backed) path, and is it a dir?
    status, meta = _stat(origin, path) if origin else ("", None)
    if status == "ok" and meta.get("remote"):
        # Mount-backed: list via /api/fs/list, never a kernel scan. If `path` is a
        # file (not a dir), descend to its parent with pure string ops — never a
        # kernel os.path call on a remote path (that call wedges the NFS mount).
        if not meta.get("is_dir"):
            path = os.path.dirname(path) or "/"
        parent = (os.path.dirname(path) or path).replace(os.sep, "/")
        fpath = path.replace(os.sep, "/")
        dirs, files = [], []
        try:
            ents, _ = _list_remote(origin, path)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "path": fpath, "parent": parent,
                    "dirs": [], "files": []}
        for ent in ents:
            name = ent["name"]
            if name.startswith("."):
                continue
            if ent.get("is_dir"):
                dirs.append(name)
            elif name.lower().endswith(want):
                files.append({"name": name, "size": ent.get("size") or 0})
        dirs.sort(key=str.lower)
        files.sort(key=lambda f: f["name"].lower())
        return {"path": fpath, "parent": parent, "dirs": dirs, "files": files}
    parent = (os.path.dirname(path) or path).replace(os.sep, "/")  # dirname(root) == root
    if not os.path.isdir(path):
        path = os.path.dirname(path) or "/"
        parent = (os.path.dirname(path) or path).replace(os.sep, "/")
    path = path.replace(os.sep, "/")
    dirs, files = [], []
    try:
        names = os.listdir(path)
    except OSError as e:
        return {"error": str(e), "path": path, "parent": parent, "dirs": [], "files": []}
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append(name)
            elif name.lower().endswith(want):
                files.append({"name": name, "size": os.path.getsize(full)})
        except OSError:
            continue
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    return {"path": path, "parent": parent, "dirs": dirs, "files": files}


def _export(doc, kind, pages, name, directory=""):
    doc = os.path.abspath(doc)
    cur = _cur_path(doc)
    os.makedirs(EXPORTS, exist_ok=True)
    out = _out_dir(directory, EXPORTS)
    stem = os.path.splitext(os.path.basename(doc))[0]
    kind = (kind or "pdf").lower()
    files = []
    if kind == "pdf":
        import pikepdf
        with pikepdf.open(cur) as src:
            idxs = _parse_pages(pages, len(src.pages))
            dst = pikepdf.Pdf.new()
            for i in idxs:
                dst.pages.append(src.pages[i])
            dest = _unique_path(out, _safe_name(name, stem) + ".pdf")
            dst.save(dest)
        files.append(dest)
    elif kind in ("png", "jpg"):
        import fitz
        d = fitz.open(cur)
        idxs = _parse_pages(pages, d.page_count)
        if len(idxs) > 50:
            raise ValueError("image export is capped at 50 pages per call")
        for i in idxs:
            pix = d[i].get_pixmap(dpi=150)
            dest = _unique_path(out, f"{_safe_name(name, stem)}-p{i + 1}.{kind}")
            pix.save(dest)
            files.append(dest)
        d.close()
    elif kind == "txt":
        import fitz
        d = fitz.open(cur)
        idxs = _parse_pages(pages, d.page_count)
        text = "\n\n".join(d[i].get_text() for i in idxs)
        dest = _unique_path(out, _safe_name(name, stem) + ".txt")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        d.close()
        files.append(dest)
    else:
        raise ValueError(f"unsupported export kind: {kind}")
    return {"files": [{"path": _fwd(p), "name": os.path.basename(p),
                       "size": os.path.getsize(p)} for p in files],
            "dir": _fwd(out)}


# ------------------------------------------------------------------ inspection
# Thresholds mirror firecrawl/pdf-inspector's detector, so TextBased / Scanned /
# ImageBased / Mixed mean the same here as there. If `pdf_inspector` happens to
# be installed its own verdict is reported alongside as a cross-check.
TEMPLATE_IMAGE_PX = 500_000       # a placed image this big is a background/scan
MIN_TEXT_OPS = 3                  # per page, before a page counts as having text
MIN_TEXT_OPS_WITH_IMAGES = 10
MIN_UNIQUE_CHARS = 5
TEXT_PAGE_RATIO = 0.6             # share of pages with text to call the doc text-based
TABLE_SCAN_CAP = 40               # find_tables() is the slow part of a scan
PAGE_SCAN_CAP = 200               # beyond this, sample evenly instead of every page
MD_PAGE_CAP = 60                  # markdown runs find_tables() on every page it converts

TYPE_LABELS = {
    "TextBased": "Text-based",
    "Scanned": "Scanned",
    "ImageBased": "Image-based",
    "Mixed": "Mixed",
}

# Keyed by the PDF /S action name. /GoTo and friends must be listed as benign:
# treating unlisted types as suspect turns ordinary internal links into findings.
ACTION_RISK = {
    "/JavaScript": ("danger", "runs JavaScript"),
    "/Launch": ("danger", "launches an external program or file"),
    "/ImportData": ("danger", "reads form data off the local disk"),
    "/SubmitForm": ("warn", "posts form data to a URL"),
    "/GoToR": ("warn", "opens another document"),
    "/GoToE": ("warn", "opens an embedded document"),
    "/Movie": ("warn", "plays embedded video"),
    "/Sound": ("warn", "plays embedded audio"),
    "/Rendition": ("warn", "plays embedded rich media"),
    "/RichMediaExecute": ("warn", "drives embedded Flash/3D content"),
    "/URI": ("info", "opens a web link"),
    "/GoTo": ("info", "jumps to a page in this document"),
    "/Named": ("info", "runs a viewer command"),
    "/Hide": ("info", "shows or hides page content"),
    "/SetOCGState": ("info", "toggles optional-content layers"),
    "/Thread": ("info", "follows an article thread"),
    "/Trans": ("info", "plays a page transition"),
}
EXEC_EXTS = {".exe", ".dll", ".scr", ".bat", ".cmd", ".com", ".ps1", ".vbs",
             ".js", ".jse", ".jar", ".msi", ".lnk", ".hta", ".wsf", ".reg", ".sh"}


def _gutter(xspans, width):
    """Centre x of the widest vertical gap no text crosses, when it is wide
    enough to be a column gutter rather than word spacing. Used both to count
    columns during a scan and to order blocks for markdown."""
    merged = []
    for a, b in sorted(xspans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if len(merged) < 2:
        return None
    gap, i = max((merged[j + 1][0] - merged[j][1], j) for j in range(len(merged) - 1))
    if gap < width * 0.06:
        return None
    return merged[i][1] + gap / 2


def _page_scan(page, tables=False):
    """Per-page metrics. get_texttrace() gives one entry per text-showing
    operator plus every character's (unicode, glyph), which is what makes the
    text/scan call and the broken-encoding call possible without rendering."""
    import fitz

    text_ops = invisible = chars = notdef = undecodable = 0
    unique = set()
    fonts = set()
    for span in page.get_texttrace():
        text_ops += 1
        if span["type"] == 3:            # render mode 3/7 — invisible OCR layer
            invisible += 1
        fonts.add(span["font"])
        for ucs, gid, _origin, _bbox in span["chars"]:
            chars += 1
            if gid == 0:
                notdef += 1
            if ucs in (0, 0xFFFD):
                undecodable += 1
            elif ucs > 32:
                unique.add(ucs)

    tblocks = [(b[0], b[2]) for b in page.get_text("blocks") if b[6] == 0]
    imgs = page.get_image_info()
    area = abs(page.rect.width * page.rect.height) or 1.0
    cover = min(1.0, sum(abs(fitz.Rect(i["bbox"]).get_area()) for i in imgs) / area)
    pixels = [i["width"] * i["height"] for i in imgs]
    drawings = len(page.get_drawings())
    alnum = sum(1 for c in unique if chr(c).isalnum())

    template = max(pixels, default=0) >= TEMPLATE_IMAGE_PX \
        or sum(pixels) >= TEMPLATE_IMAGE_PX * 4
    return {
        "n": page.number + 1,
        "width": round(page.rect.width, 1), "height": round(page.rect.height, 1),
        "rotation": page.rotation,
        "chars": chars, "text_ops": text_ops, "invisible_ops": invisible,
        "unique_chars": len(unique), "notdef": notdef, "undecodable": undecodable,
        "images": len(imgs), "image_cover": round(cover, 3), "drawings": drawings,
        "links": len([l for l in page.get_links() if l.get("uri")]),
        "fonts": sorted(fonts),
        "has_text": (text_ops >= (MIN_TEXT_OPS_WITH_IMAGES if imgs else MIN_TEXT_OPS)
                     and len(unique) >= MIN_UNIQUE_CHARS),
        "vector_text": drawings >= 1000 and drawings > text_ops * 200 and alnum < 30,
        "template_image": template,
        "scan_like": template and len(imgs) <= 1 and text_ops < 50 and alnum < 10,
        "columns": 2 if len(tblocks) >= 4 and _gutter(tblocks, page.rect.width) else 1,
        "tables": len(page.find_tables().tables) if tables else 0,
        "tables_scanned": tables,
    }


def _classify(scans):
    n = len(scans)
    with_text = sum(1 for s in scans if s["has_text"])
    ratio = with_text / n
    total_ops = sum(s["text_ops"] for s in scans)
    templates = sum(1 for s in scans if s["template_image"])
    any_images = any(s["images"] for s in scans)
    any_vector = any(s["vector_text"] for s in scans)

    if templates and with_text:
        kind, conf = "Mixed", 0.5 + 0.3 * (1 - templates / n)
    elif ratio >= TEXT_PAGE_RATIO:
        kind, conf = "TextBased", ratio
    elif not with_text and (any_images or any_vector):
        kind, conf = ("Scanned", 0.95) if total_ops == 0 else ("ImageBased", 0.8)
    elif with_text and (any_images or any_vector):
        kind, conf = "Mixed", 0.7
    elif total_ops == 0:
        kind, conf = "Scanned", 0.9
    else:
        kind, conf = "TextBased", max(ratio, 0.5)

    reasons = {}
    for s in scans:
        why = []
        if kind in ("Scanned", "ImageBased"):
            why.append("no text layer anywhere in the document")
        else:
            if s["scan_like"]:
                why.append("full-page image with almost no text")
            if s["vector_text"]:
                why.append("text is drawn as vector outlines")
            if s["images"] and not s["has_text"]:
                why.append("images but no extractable text")
            if not s["images"] and not s["has_text"] and s["drawings"]:
                why.append("no extractable text on the page")
            if s["chars"] and (s["undecodable"] / s["chars"] > 0.02
                               or s["notdef"] / s["chars"] > 0.2):
                why.append("font encoding does not decode to Unicode")
        if why:
            reasons[str(s["n"])] = why
        s["needs_ocr"] = bool(why)

    return {
        "type": kind, "label": TYPE_LABELS[kind],
        "confidence": round(min(1.0, conf), 2),
        "page_count": len(scans), "pages_with_text": with_text,
        "ocr_recommended": bool(reasons),
        "pages_needing_ocr": [int(k) for k in reasons],
        "reasons": reasons,
    }


def _font_report(d, page_idx):
    seen = {}
    for i in page_idx:
        for xref, ext, ftype, basefont, refname, enc in d.get_page_fonts(i):
            f = seen.get((xref, refname))
            if f is None:
                name = basefont or "(unnamed)"
                subset = len(name) > 7 and name[6] == "+"
                f = seen[(xref, refname)] = {
                    "name": name[7:] if subset else name,
                    "type": ftype, "encoding": enc or "(built-in)",
                    "embedded": ext not in ("n/a", ""), "subset": subset,
                    "tounicode": bool(xref) and d.xref_get_key(xref, "ToUnicode")[0] != "null",
                    "pages": [],
                }
            f["pages"].append(i + 1)
    fonts = sorted(seen.values(), key=lambda f: f["name"].lower())
    for f in fonts:
        f["page_count"] = len(f["pages"])
        f["pages"] = f["pages"][:8]
        # An Identity-H CID font with no ToUnicode map has no path back to
        # characters — extraction produces mojibake and OCR is the only way out.
        f["undecodable"] = f["encoding"] == "Identity-H" and not f["tounicode"]
    return fonts


def _is_int(obj):
    try:
        int(obj)
    except (TypeError, ValueError):
        return False
    return True


def _walk_triggers(obj, where, hits):
    """The /AA additional-actions dict, if the object really has one. Every key
    in it fires on an event (open, page close, field format) with no click."""
    import pikepdf

    aa = obj.get("/AA")
    if isinstance(aa, pikepdf.Dictionary):
        for key, act in aa.items():
            _walk_action(act, f"{where} trigger {key}", hits)


def _note_attachment(name, where, facts, hits):
    ext = os.path.splitext(name)[1].lower()
    executable = ext in EXEC_EXTS
    facts["attachments"].append({"name": name, "executable": executable})
    if executable:
        hits.append({"level": "danger", "kind": "/EmbeddedFile",
                     "what": "carries an executable attachment",
                     "detail": name, "where": where})


def _walk_action(act, where, hits, depth=0):
    """Collect (level, kind, description, where) for an action and its /Next."""
    import pikepdf

    if depth > 4 or not isinstance(act, pikepdf.Dictionary):
        return
    kind = str(act.get("/S", "")) or "(unnamed)"
    level, what = ACTION_RISK.get(kind, ("warn", "an unrecognised action type"))
    detail = ""
    if kind == "/URI":
        detail = str(act.get("/URI", ""))
    elif kind == "/Launch":
        win = act.get("/Win")
        target = act.get("/F") or (win.get("/F") if isinstance(win, pikepdf.Dictionary) else None)
        detail = str(target) if target is not None else ""
    elif kind == "/JavaScript":
        js = act.get("/JS")
        code = bytes(js.read_bytes()).decode("utf-8", "replace") if isinstance(js, pikepdf.Stream) else str(js or "")
        detail = _norm_ws(code)[:400]
    elif kind in ("/GoToR", "/GoToE", "/SubmitForm", "/ImportData"):
        detail = str(act.get("/F", ""))
    hits.append({"level": level, "kind": kind, "what": what,
                 "detail": detail, "where": where})
    nxt = act.get("/Next")
    if isinstance(nxt, pikepdf.Array):
        for a in nxt:
            _walk_action(a, where, hits, depth + 1)
    else:
        _walk_action(nxt, where, hits, depth + 1)


def _security(path):
    """Everything in the file that can act on its own, plus the encryption,
    signature and revision facts that go with deciding whether to trust it."""
    import pikepdf

    hits = []
    urls = {}
    facts = {"attachments": [], "signatures": 0, "xfa": False, "acroform": False,
             "encrypted": False, "permissions": {}, "revisions": 0,
             "js_names": 0, "open_action": "", "layers": False, "linearized": False}

    with pikepdf.open(path) as pdf:
        root = pdf.Root
        facts["encrypted"] = pdf.is_encrypted
        facts["linearized"] = pdf.is_linearized
        if pdf.is_encrypted:
            facts["permissions"] = {k: bool(v) for k, v in pdf.allow._asdict().items()}

        oa = root.get("/OpenAction")
        if isinstance(oa, pikepdf.Dictionary):
            facts["open_action"] = str(oa.get("/S", ""))
            _walk_action(oa, "document open action", hits)
        elif isinstance(oa, pikepdf.Array):
            facts["open_action"] = "/GoTo"      # a destination array, not an action

        _walk_triggers(root, "document", hits)

        names = root.get("/Names")
        js_tree = names.get("/JavaScript") if isinstance(names, pikepdf.Dictionary) else None
        if isinstance(js_tree, pikepdf.Dictionary):
            for label, act in pikepdf.NameTree(js_tree).items():
                facts["js_names"] += 1
                _walk_action(act, f"document script {label}", hits)

        acro = root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary):
            facts["acroform"] = True
            facts["xfa"] = "/XFA" in acro
            flags = acro.get("/SigFlags")
            facts["signatures"] = int(flags) & 1 if _is_int(flags) else 0

        facts["layers"] = "/OCProperties" in root

        for name in pdf.attachments:
            _note_attachment(name, "embedded files", facts, hits)

        for i, page in enumerate(pdf.pages, 1):
            _walk_triggers(page, f"page {i}", hits)
            annots = page.get("/Annots")
            for annot in annots if isinstance(annots, pikepdf.Array) else ():
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                sub = str(annot.get("/Subtype", ""))
                if sub == "/Widget" and str(annot.get("/FT", "")) == "/Sig":
                    facts["signatures"] = max(facts["signatures"], 1)
                if sub in ("/RichMedia", "/Screen", "/Movie", "/3D", "/Sound"):
                    hits.append({"level": "warn", "kind": sub,
                                 "what": "embeds rich media",
                                 "detail": "", "where": f"page {i}"})
                # The /EmbeddedFiles tree is not the only way in: a filespec on
                # an annotation is still openable from the viewer.
                if sub == "/FileAttachment":
                    fs = annot.get("/FS")
                    if isinstance(fs, pikepdf.Dictionary):
                        _note_attachment(str(fs.get("/UF") or fs.get("/F") or "(unnamed)"),
                                         f"page {i} attachment", facts, hits)
                if "/A" in annot:
                    _walk_action(annot["/A"], f"page {i} annotation", hits)
                _walk_triggers(annot, f"page {i} field", hits)

    for h in hits:
        if h["kind"] == "/URI" and h["detail"]:
            urls[h["detail"]] = urls.get(h["detail"], 0) + 1

    # An in-place update appends a revision ending in its own %%EOF — but so does
    # a linearized file's first-page xref, which has to be discounted or every
    # web-optimized PDF looks rewritten. A byte count is a floor, not a proof
    # (%%EOF can occur inside a stream), hence "sections" and not "edits".
    facts["revisions_checked"] = os.path.getsize(path) <= 64 * 1024 * 1024
    if facts["revisions_checked"]:
        with open(path, "rb") as f:
            eofs = f.read().count(b"%%EOF")
        facts["revisions"] = max(0, eofs - 1 - (1 if facts["linearized"] else 0))

    return _security_report(hits, urls, facts)


def _security_report(hits, urls, facts):
    """Fold the evidence into one checklist row per thing a reader wants ruled
    out, plus the findings behind each row."""
    by_kind = {}
    for h in hits:
        by_kind.setdefault(h["kind"], []).append(h)

    def rows(*kinds):
        return [h for k in kinds for h in by_kind.get(k, [])]

    checks = []

    def check(name, state, note):
        checks.append({"name": name, "state": state, "note": note})

    js = rows("/JavaScript")
    check("JavaScript", "fail" if js else "pass",
          f"{len(js)} script action{'' if len(js) == 1 else 's'} in the document"
          if js else "No scripts embedded")

    launch = rows("/Launch", "/ImportData")
    check("Launch / local file access", "fail" if launch else "pass",
          "; ".join(sorted({h["detail"] or h["what"] for h in launch}))
          if launch else "Nothing runs or reads local files")

    # Name-tree scripts run at document open too, so they belong on this row.
    auto = [h for h in hits if h["level"] in ("danger", "warn")
            and (h["where"].endswith("open action") or " trigger " in h["where"]
                 or h["where"].startswith("document script"))]
    check("Automatic actions", "warn" if auto else "pass",
          f"{len(auto)} action{' fires' if len(auto) == 1 else 's fire'} without a click"
          if auto else ("Opens to a page destination" if facts["open_action"] == "/GoTo"
                        else "Nothing fires on open"))

    att = facts["attachments"]
    bad_att = [a for a in att if a["executable"]]
    check("Embedded files", "fail" if bad_att else ("warn" if att else "pass"),
          ", ".join(a["name"] for a in att) if att else "No attachments")

    submit = rows("/SubmitForm")
    check("Forms", "warn" if (submit or facts["xfa"]) else
          ("info" if facts["acroform"] else "pass"),
          "XFA form" if facts["xfa"] else
          (f"{len(submit)} submit action{'' if len(submit) == 1 else 's'}" if submit else
           ("Fillable AcroForm fields" if facts["acroform"] else "No form fields")))

    remote = rows("/GoToR", "/GoToE")
    insecure = sorted(u for u in urls if u.lower().startswith("http://")
                      or re.match(r"^\w+://\d{1,3}(\.\d{1,3}){3}", u.lower()))
    check("External references", "warn" if (remote or insecure) else
          ("info" if urls else "pass"),
          "; ".join(filter(None, [
              f"{len(urls)} link target{'' if len(urls) == 1 else 's'}" if urls else "",
              f"{len(insecure)} not over HTTPS" if insecure else "",
              f"{len(remote)} reference{'' if len(remote) == 1 else 's'} to other files" if remote else "",
          ])) or "No outbound links")

    media = rows("/RichMedia", "/Screen", "/Movie", "/Sound", "/3D", "/Rendition")
    check("Multimedia", "warn" if media else "pass",
          f"{len(media)} rich-media object{'' if len(media) == 1 else 's'}"
          if media else "No audio, video or 3D content")

    check("Encryption", "info" if facts["encrypted"] else "pass",
          "Encrypted — " + ", ".join(k for k, v in facts["permissions"].items() if not v)
          + " not permitted" if facts["encrypted"] and any(
              not v for v in facts["permissions"].values())
          else ("Encrypted, all operations permitted" if facts["encrypted"]
                else "Not encrypted"))

    check("Signatures", "info" if facts["signatures"] else "pass",
          "Contains a signature field — this scan does not verify it"
          if facts["signatures"] else "Unsigned")

    revs = facts["revisions"]
    check("Revisions",
          "info" if not facts["revisions_checked"] else
          "warn" if revs > 2 else "info" if revs else "pass",
          "Not checked — the file is too large to scan for appended revisions"
          if not facts["revisions_checked"] else
          f"{revs} extra cross-reference section{'' if revs == 1 else 's'} —"
          " earlier content may still be recoverable" if revs
          else "Single revision, no hidden history")

    risk = "risky" if any(c["state"] == "fail" for c in checks) else \
           "notable" if any(c["state"] == "warn" for c in checks) else "clean"
    findings = sorted([h for h in hits if h["level"] in ("danger", "warn")],
                      key=lambda h: 0 if h["level"] == "danger" else 1)
    return {
        "risk": risk, "checks": checks,
        "findings": findings[:60], "findings_total": len(findings),
        "urls": [{"url": u, "count": c} for u, c in
                 sorted(urls.items(), key=lambda kv: -kv[1])],
        "facts": facts,
    }


def _rust_engine(path):
    """firecrawl/pdf-inspector's own verdict, when it is installed."""
    try:
        import pdf_inspector
    except ImportError:
        return None
    r = pdf_inspector.process_pdf(path)
    return {
        "version": getattr(pdf_inspector, "__version__", ""),
        "type": r.pdf_type, "confidence": round(float(r.confidence), 2),
        "page_count": r.page_count, "ms": r.processing_time_ms,
        "title": r.title or "",
        "pages_needing_ocr": list(r.pages_needing_ocr),
        "pages_with_tables": list(r.pages_with_tables),
        "pages_with_columns": list(r.pages_with_columns),
        "complex_layout": bool(r.is_complex_layout),
        "encoding_issues": bool(r.has_encoding_issues),
    }


def _inspect(doc, pages):
    import fitz

    cur = _cur_path(doc)
    d = fitz.open(cur)
    idx = _parse_pages(pages, d.page_count)
    if not idx:
        d.close()
        raise ValueError("this document has no pages to inspect")
    # ~70 ms/page, so past the cap sample evenly, keeping the first and last.
    if len(idx) > PAGE_SCAN_CAP:
        step = (len(idx) - 1) / (PAGE_SCAN_CAP - 1)
        idx = sorted({idx[round(i * step)] for i in range(PAGE_SCAN_CAP)})
        sampled = True
    else:
        sampled = False
    scans = [_page_scan(d[i], tables=n < TABLE_SCAN_CAP) for n, i in enumerate(idx)]
    verdict = _classify(scans)
    verdict["sampled"] = sampled
    m = d.metadata or {}
    doc_facts = {
        "path": _fwd(os.path.abspath(doc)), "name": os.path.basename(doc),
        "size": os.path.getsize(cur), "pdf_version": m.get("format", ""),
        "title": m.get("title", ""), "author": m.get("author", ""),
        "subject": m.get("subject", ""), "keywords": m.get("keywords", ""),
        "creator": m.get("creator", ""), "producer": m.get("producer", ""),
        "created": m.get("creationDate", ""), "modified": m.get("modDate", ""),
        "page_count": d.page_count, "objects": d.xref_length() - 1,
        "linearized": bool(d.is_fast_webaccess), "tagged": _is_tagged(d),
        "outline_items": len(d.get_toc()),
        "attachments": d.embfile_count(), "form": bool(d.is_form_pdf),
        "layers": bool(d.get_layers()),
    }
    d.close()

    return {
        "doc": doc_facts,
        "verdict": verdict,
        "pages": scans,
        "fonts": _font_report(fitz.open(cur), idx),
        "text": _text_quality(scans),
        "security": _security(cur),
        "rust": _rust_engine(cur),
        "tables_capped": len(idx) > TABLE_SCAN_CAP,
    }


def _is_tagged(d):
    return d.xref_get_key(-1, "Root/StructTreeRoot")[0] != "null"


def _text_quality(scans):
    chars = sum(s["chars"] for s in scans)
    notdef = sum(s["notdef"] for s in scans)
    bad = sum(s["undecodable"] for s in scans)
    issues = []
    if chars and notdef / chars > 0.02:
        issues.append(f"{notdef} of {chars} glyphs have no outline in their font")
    if chars and bad / chars > 0.02:
        issues.append(f"{bad} characters do not decode to Unicode")
    invisible = sum(s["invisible_ops"] for s in scans)
    if invisible:
        issues.append(f"{invisible} invisible text runs — typically an OCR layer")
    tables = sum(s["tables"] for s in scans)
    return {
        "chars": chars, "notdef": notdef, "undecodable": bad,
        "invisible_runs": invisible, "tables": tables,
        "links": sum(s["links"] for s in scans),
        "column_pages": [s["n"] for s in scans if s["columns"] > 1],
        "issues": issues,
    }


# --------------------------------------------------------- markdown conversion
def _reading_order(blocks, rect):
    """Column-aware order: full-width blocks (headlines, rules) split the page
    into zones, and within a zone the left column is emitted before the right."""
    if len(blocks) < 4:
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    split = _gutter([(b["bbox"][0], b["bbox"][2]) for b in blocks], rect.width)
    if split is None:
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    key = lambda b: (round(b["bbox"][1], 1), b["bbox"][0])   # noqa: E731
    spans_gutter = lambda b: b["bbox"][0] < split < b["bbox"][2]   # noqa: E731
    out, zone = [], []

    def flush():
        left = sorted((b for b in zone if not spans_gutter(b) and b["bbox"][2] <= split), key=key)
        right = sorted((b for b in zone if not spans_gutter(b) and b["bbox"][0] >= split), key=key)
        out.extend(left + right)
        zone.clear()

    for b in sorted(blocks, key=lambda b: b["bbox"][1]):
        if spans_gutter(b):
            flush()
            out.append(b)
        else:
            zone.append(b)
    flush()
    return out


def _inline(spans):
    """Emphasis markers around runs of same-styled spans, not around each span —
    a title split into five bold spans must not become `**F****USED**`."""
    runs = []
    for s in spans:
        style = s["flags"] & (2 | 8 | 16)        # italic | monospace | bold
        if runs and runs[-1][0] == style:
            runs[-1][1] += s["text"]
        else:
            runs.append([style, s["text"]])
    parts = []
    for style, text in runs:
        core = text.strip()
        if not core:
            parts.append(text)
            continue
        if style & 8:
            core = f"`{core}`"
        else:
            if style & 16:
                core = f"**{core}**"
            if style & 2:
                core = f"*{core}*"
        parts.append(text[:len(text) - len(text.lstrip())] + core
                     + text[len(text.rstrip()):])
    return _norm_ws("".join(parts))


BULLETS = "•◦▪▫‣⁃-–*"


def _line_markdown(line, body_size):
    text = _inline(line["spans"])
    if not text:
        return ""
    size = max((s["size"] for s in line["spans"]), default=body_size)
    mono = all(s["flags"] & 8 for s in line["spans"] if s["text"].strip())
    if mono:
        return "    " + text.strip("`")
    m = re.match(rf"^[{re.escape(BULLETS)}]\s+(.*)", text)
    if m:
        return "- " + m.group(1)
    if re.match(r"^\(?\d{1,3}[.)]\s+\S", text):
        return re.sub(r"^\(?(\d{1,3})[.)]\s+", r"\1. ", text)
    # Parenthesised form only: a bare "E. coli" is a sentence, and treating it
    # as a list item deletes the initial.
    if re.match(r"^\([a-zA-Z]\)\s+\S", text):
        return "- " + re.sub(r"^\([a-zA-Z]\)\s+", "", text)
    ratio = size / body_size if body_size else 1
    if len(text.split()) <= 20:
        # A heading carries its own weight — drop emphasis markers wrapping it.
        head = re.sub(r"^\*{1,3}(.*?)\*{1,3}$", r"\1", text).strip()
        if ratio >= 1.8:
            return "# " + head
        if ratio >= 1.45:
            return "## " + head
        if ratio >= 1.2:
            return "### " + head
        if ratio >= 1.05 and all(s["flags"] & 16 for s in line["spans"] if s["text"].strip()):
            return "#### " + head
    return text


def _fold_heading(prev, cur):
    """A title wrapped over two lines arrives as two same-level headings (in one
    block or in two). Fold them into one, or return None if they don't pair."""
    a = re.fullmatch(r"(#{1,4}) (.+)", prev or "")
    b = re.fullmatch(r"(#{1,4}) (.+)", cur)
    if a and b and a.group(1) == b.group(1):
        return f"{a.group(1)} {a.group(2)} {b.group(2)}"
    return None


def _page_links(page):
    """Link annotations paired with the words they sit under. Spans are style
    runs, so a link over one word covers too little of its span to match on."""
    import fitz

    words = page.get_text("words")
    out = []
    for link in page.get_links():
        uri = link.get("uri")
        if not uri:
            continue
        rect = fitz.Rect(link["from"])
        covered = [w[4] for w in words
                   if abs(rect & fitz.Rect(w[:4])) > abs(fitz.Rect(w[:4])) * 0.5]
        if covered:
            out.append((rect, " ".join(covered), uri))
    return out


def _link_text(md, line, links):
    import fitz

    lbox = fitz.Rect(line["bbox"])
    for rect, anchor, uri in links:
        if f"]({uri})" in md or abs(rect & lbox) <= 0:
            continue
        if anchor in md and "[" not in anchor and "]" not in anchor:
            md = md.replace(anchor, f"[{anchor}]({uri})", 1)
    return md


def _page_markdown(page):
    import fitz

    tables = page.find_tables().tables
    trects = [fitz.Rect(t.bbox) for t in tables]
    raw = page.get_text("dict")
    blocks = [b for b in raw["blocks"] if b["type"] == 0 and b.get("lines")]
    sizes = {}
    for b in blocks:
        for line in b["lines"]:
            for s in line["spans"]:
                sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(s["text"])
    body = max(sizes, key=sizes.get) if sizes else 11.0

    links = _page_links(page)
    emitted = set()
    out = []
    for b in _reading_order(blocks, page.rect):
        bbox = fitz.Rect(b["bbox"])
        hit = next((i for i, r in enumerate(trects)
                    if abs(r & bbox) > abs(bbox) * 0.6), None)
        if hit is not None:
            if hit not in emitted:
                emitted.add(hit)
                out.append(tables[hit].to_markdown().strip())
            continue
        para = []
        for line in b["lines"]:
            md = _line_markdown(line, body)
            if not md:
                continue
            md = _link_text(md, line, links)
            folded = _fold_heading(para[-1], md) if para else None
            if folded:
                para[-1] = folded
            else:
                para.append(md)
        if not para:
            continue
        chunk = "\n".join(para)
        folded = _fold_heading(out[-1], chunk) if out else None
        if folded:
            out[-1] = folded
        else:
            out.append(chunk)
    for i, t in enumerate(tables):
        if i not in emitted:
            out.append(t.to_markdown().strip())
    return "\n\n".join(out)


def _markdown(doc, pages):
    import fitz

    cur = _cur_path(doc)
    d = fitz.open(cur)
    idx = _parse_pages(pages, d.page_count)
    # find_tables() runs per page here; a whole large document would blow the
    # 60 s call timeout and return nothing at all.
    if len(idx) > MD_PAGE_CAP:
        d.close()
        raise ValueError(
            f"Markdown conversion is capped at {MD_PAGE_CAP} pages per run "
            f"({len(idx)} selected) — convert a range, e.g. 1-{MD_PAGE_CAP}")
    parts = []
    for i in idx:
        parts.append(f"<!-- page {i + 1} -->\n\n" + _page_markdown(d[i]))
    d.close()
    md = "\n\n---\n\n".join(parts).strip() + "\n"
    # Bare URLs become links. The lookbehind keeps it off URLs that are already
    # the text or the target of a link built above, which would nest them.
    md = re.sub(r"(?<![(\[!])\b(https?://[^\s<>()\[\]]+)", r"[\1](\1)", md)
    return {"markdown": md, "chars": len(md), "pages": len(idx)}


def _save_markdown(doc, pages, directory, name):
    md = _markdown(doc, pages)["markdown"]
    out = _out_dir(directory, os.path.dirname(os.path.abspath(doc)))
    stem = _safe_name(name, os.path.basename(doc))
    dest = _unique_path(out, os.path.splitext(stem)[0] + ".md")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(md)
    return {"name": os.path.basename(dest), "path": _fwd(dest),
            "size": os.path.getsize(dest), "dir": _fwd(out)}


def _health():
    out = {"ok": True, "pymupdf": "", "pikepdf": ""}
    try:
        import fitz
        out["pymupdf"] = fitz.version[0]
    except Exception as e:
        out["ok"], out["pymupdf_error"] = False, str(e)
    try:
        import pikepdf
        out["pikepdf"] = pikepdf.__version__
    except Exception as e:
        out["ok"], out["pikepdf_error"] = False, str(e)
    try:
        import pdf_inspector
        out["pdf_inspector"] = getattr(pdf_inspector, "__version__", "installed")
    except ImportError:
        out["pdf_inspector"] = ""
    out["soffice"] = bool(_find_soffice())
    out["ocr_langs"] = sorted(
        f[:-len(".traineddata")] for f in os.listdir(TESSDATA)
        if f.endswith(".traineddata")) if os.path.isdir(TESSDATA) else []
    return out


# ----------------------------------------------------------------- dispatcher
def main(
    action: str = "list_library",
    doc: str = "",
    src: str = "",
    url: str = "",
    name: str = "",
    path: str = "",
    directory: str = "",
    pages: str = "",
    degrees: int = 90,
    order: str = "",
    at: int = 1,
    width: str = "",
    height: str = "",
    sources: str = "",
    mode: str = "each",
    ranges: str = "",
    prefix: str = "",
    level: str = "lossless",
    kind: str = "",
    page: int = 1,
    bbox: str = "",
    origin: str = "",
    old_text: str = "",
    new_text: str = "",
    font: str = "",
    size: str = "",
    flags: int = 0,
    color: str = "",
    expected_mtime: str = "",
    force: int = 0,
    password: str = "",
    owner: str = "",
    language: str = "",
    images_b64: str = "",
    exts: str = "",
):
    if action == "health":
        return _health()
    if action == "list_library":
        return _list_library()
    if action == "add_to_library":
        return _add_to_library(src)
    if action == "remove_from_library":
        return _remove_from_library(doc)
    if action == "open_doc":
        p = os.path.abspath(doc)
        wpath, wmeta = _open_work(p)
        info = _docinfo(wpath)
        info["path"] = _fwd(p)
        info["name"] = os.path.basename(p)
        info["work"] = _fwd(wpath)
        info["dirty"] = bool(wmeta.get("dirty"))
        if not info["encrypted"]:
            import fitz
            d = fitz.open(wpath)
            info["has_text"] = any(d[i].get_text().strip()
                                   for i in range(min(5, d.page_count)))
            d.close()
        else:
            info["has_text"] = False
        info["undo_depth"], info["redo_depth"] = _stack_depths(p)
        # RO verdict (SPEC §13.5 RO-4): fs writability of the ORIGINAL. Edits
        # keep working (they hit the working copy) — only save/rename back to
        # the original are gated, so the tooltip points at Save a copy.
        info["writable"] = os.access(p, os.W_OK)
        info["readonly_message"] = "" if info["writable"] else "Read-only"
        info["readonly_tooltip"] = "" if info["writable"] else (
            "The file is read-only — edits can't be saved back to it. "
            "Use Save a copy.")
        return info
    if action == "listdir":
        # `src` on the listdir action carries the server ORIGIN (mount-safe
        # routing), distinct from its add_to_library meaning (a file path).
        return _listdir(path, src, exts)
    if action == "import_url":
        return _import_url(url, name)
    if action == "rename_doc":
        p = os.path.abspath(doc)
        n = _safe_name(name, "")
        if not n:
            raise ValueError("rename needs a name")
        if not n.lower().endswith(".pdf"):
            n += ".pdf"
        # RO gate (SPEC §13.5 RO-3): os.rename is a parent-directory op and
        # would silently move a chmod -w file.
        if os.path.isfile(p) and not os.access(p, os.W_OK):
            raise PermissionError(f"{p!r} is read-only")
        dest = _unique_path(os.path.dirname(p), n)
        os.rename(p, dest)
        _work_rename(p, dest)
        paths = _lib_load()
        if any(_same_path(l, p) for l in paths):
            _lib_save([_fwd(dest) if _same_path(l, p) else l for l in paths])
        return {"name": os.path.basename(dest), "path": _fwd(dest)}
    if action == "save":
        return _save(doc, force)
    if action == "revert":
        return _revert(doc)
    if action == "save_as":
        p = os.path.abspath(doc)
        n = _safe_name(name, os.path.basename(p))
        if not n.lower().endswith(".pdf"):
            n += ".pdf"
        dest = os.path.join(os.path.abspath(os.path.expanduser(directory or "~")), n)
        shutil.copyfile(_cur_path(p), dest)
        return {"file": _fwd(dest)}
    if action == "export":
        return _export(doc, kind, pages, name, directory)
    if action == "rotate_pages":
        return _mutate(doc, expected_mtime, "rotate",
                       lambda p: _rotate_pages(p, pages, degrees))
    if action == "delete_pages":
        return _mutate(doc, expected_mtime, "delete-pages",
                       lambda p: _delete_pages(p, pages))
    if action == "reorder_pages":
        return _mutate(doc, expected_mtime, "reorder",
                       lambda p: _reorder_pages(p, order))
    if action == "insert_blank":
        return _mutate(doc, expected_mtime, "insert-blank",
                       lambda p: _insert_blank(p, at, width, height))
    if action == "compress":
        return _mutate(doc, expected_mtime, f"compress-{level}",
                       lambda p: _compress(p, level))
    if action == "edit_text":
        return _mutate(doc, expected_mtime, "edit-text",
                       lambda p: _edit_text(p, page, bbox, origin, old_text,
                                            new_text, font, size, flags, color))
    if action == "extract_pages":
        return _extract_pages(doc, pages, name)
    if action == "merge":
        return _merge(sources, name, directory)
    if action == "split":
        return _split(doc, mode, ranges, prefix, directory)
    if action == "protect":
        return _protect(doc, password, owner)
    if action == "unlock":
        return _unlock(doc, password)
    if action == "ocr":
        return _ocr(doc, pages, language)
    if action == "pdf_to_word":
        return _pdf_to_word(doc, pages)
    if action == "word_to_pdf":
        return _word_to_pdf(src)
    if action == "excel_to_pdf":
        return _excel_to_pdf(src)
    if action == "images_to_pdf":
        return _images_to_pdf(sources, name, directory)
    if action == "save_scan":
        return _save_scan(images_b64, name, directory)
    if action == "reveal":
        p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        if not os.path.exists(p):
            raise ValueError(f"no such path: {p}")
        if not os.path.isdir(p):
            p = os.path.dirname(p)
        if os.name == "nt":
            os.startfile(p)
        else:
            import subprocess
            import sys
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
        return {"ok": True}
    if action == "inspect":
        return _inspect(doc, pages)
    if action == "to_markdown":
        return _markdown(doc, pages)
    if action == "save_markdown":
        return _save_markdown(doc, pages, directory, name)
    if action == "page_text":
        return _page_text(_open_work(doc)[0], page)
    if action == "undo":
        return _undo(doc)
    if action == "redo":
        return _redo(doc)
    raise ValueError(f"unknown action {action!r}")
