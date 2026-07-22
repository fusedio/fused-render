# /// script
# dependencies = ["pymupdf>=1.25", "pikepdf>=9"]
# ///
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
LIBRARY = os.path.join(DATA_ROOT, "library.json")  # flat list of PDF paths the user added
DOWNLOADS = os.path.join(DATA_ROOT, "downloads")  # PDFs fetched via import_url
EXPORTS = os.path.join(CACHE_ROOT, "exports")
SNAPSHOTS = os.path.join(CACHE_ROOT, "snapshots")  # undo stacks, keyed by doc path
WORKDIR = os.path.join(CACHE_ROOT, "work")  # per-doc working copies (unsaved edits)

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
        return {
            "path": _fwd(path),
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
            "mtime": os.path.getmtime(path),
            "encrypted": True,
            "page_count": 0,
            "pages": [],
        }
    pages = [
        {
            "n": i + 1,
            "width": round(p.rect.width, 2),
            "height": round(p.rect.height, 2),
            "rotation": p.rotation,
        }
        for i, p in enumerate(doc)
    ]
    out = {
        "path": _fwd(path),
        "name": os.path.basename(path),
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
        "encrypted": False,
        "page_count": doc.page_count,
        "pages": pages,
    }
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
    if not (
        meta and os.path.exists(wpath) and (meta.get("dirty") or meta.get("base_mtime") == smt)
    ):
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
    _work_save_state(src, {"src": _fwd(src), "base_mtime": os.path.getmtime(src), "dirty": False})
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
    out = {
        "ok": True,
        "mtime": os.path.getmtime(wpath),
        "doc": info,
        "dirty": bool(meta.get("dirty")),
    }
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
    return {
        "name": os.path.basename(dest),
        "path": _fwd(dest),
        "size": os.path.getsize(dest),
        "dir": _fwd(os.path.dirname(dest)),
    }


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
            files.append(
                {"name": os.path.basename(dest), "path": _fwd(dest), "size": os.path.getsize(dest)}
            )
    return {"files": files, "dir": _fwd(out_dir)}


def _compress(doc, level):
    import pikepdf

    before = os.path.getsize(doc)

    def fn(path):
        if level == "aggressive":
            import fitz

            if before > 80 * 1024 * 1024:
                raise ValueError("file too large for aggressive compression — use lossless")
            d = fitz.open(path)
            d.rewrite_images(dpi_threshold=200, dpi_target=150, quality=75)
            d.subset_fonts()
            tmp = path + ".tmp"
            d.save(tmp, garbage=4, deflate=True, clean=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            d.close()
            os.replace(tmp, path)
        else:
            with pikepdf.open(path, allow_overwriting_input=True) as pdf:
                pdf.save(
                    path,
                    compress_streams=True,
                    recompress_flate=True,
                    object_stream_mode=pikepdf.ObjectStreamMode.generate,
                )
        return {"before": before, "after": os.path.getsize(path)}

    return fn(doc)


# ------------------------------------------------------------------ text edit
_CJK_FONTS = ((0x4E00, 0x9FFF, "china-s"), (0x3040, 0x30FF, "japan"), (0xAC00, 0xD7AF, "korea"))


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
                spans.append(
                    {
                        "text": txt,
                        "bbox": [round(v, 2) for v in s["bbox"]],
                        "origin": [round(v, 2) for v in s["origin"]],
                        "font": s["font"],
                        "size": round(s["size"], 2),
                        "flags": s["flags"],
                        "color": [(c >> 16) & 255, (c >> 8) & 255, c & 255],
                    }
                )
    out = {
        "page": page,
        "width": round(p.rect.width, 2),
        "height": round(p.rect.height, 2),
        "rotation": p.rotation,
        "spans": spans,
        "mtime": os.path.getmtime(doc),
    }
    d.close()
    return out


def _edit_text(doc, page, bbox, origin, old_text, new_text, font, size, flags, color):
    import fitz

    def fn(path):
        d = fitz.open(path)
        p = d[page - 1]
        if p.rotation != 0:
            raise ValueError(
                "text editing on rotated pages isn't supported — rotate the page to 0° first"
            )
        rect = fitz.Rect(*json.loads(bbox))
        got = _norm_ws(p.get_text("text", clip=rect + (-1, -1, 1, 1)))
        if _norm_ws(old_text) not in got:
            raise ValueError("the page text changed on disk — reload and retry")
        fname = _pick_font(font, int(flags or 0), new_text)
        fsize = float(size or 11)
        if new_text:
            while (
                fsize > 6
                and fitz.get_text_length(new_text, fontname=fname, fontsize=fsize) > rect.width + 2
            ):
                fsize -= 0.25
        p.add_redact_annot(rect)
        p.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE
        )
        if new_text:
            ox, oy = json.loads(origin)
            col = [c / 255 for c in json.loads(color or "[0,0,0]")]
            p.insert_text((ox, oy), new_text, fontname=fname, fontsize=fsize, color=col)
        d.save(path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        d.close()
        return {"used_font": fname, "used_size": round(fsize, 2)}

    return fn(doc)


# -------------------------------------------------------------------- library
def _doc_entry(path):
    entry = {
        "name": os.path.basename(path),
        "path": _fwd(path),
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
    }
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
            docs.append(
                {
                    "name": os.path.basename(full),
                    "path": _fwd(full),
                    "size": 0,
                    "mtime": 0,
                    "page_count": None,
                    "missing": True,
                }
            )
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
    return f"{u.scheme}://{u.netloc}{endpoint}?path=" + _urlparse.quote(path)


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


def _listdir(path, origin=""):
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
            return {"error": str(exc), "path": fpath, "parent": parent, "dirs": [], "files": []}
        for ent in ents:
            name = ent["name"]
            if name.startswith("."):
                continue
            if ent.get("is_dir"):
                dirs.append(name)
            elif name.lower().endswith(".pdf"):
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
            elif name.lower().endswith(".pdf"):
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
    return {
        "files": [
            {"path": _fwd(p), "name": os.path.basename(p), "size": os.path.getsize(p)}
            for p in files
        ],
        "dir": _fwd(out),
    }


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
            info["has_text"] = any(d[i].get_text().strip() for i in range(min(5, d.page_count)))
            d.close()
        else:
            info["has_text"] = False
        info["undo_depth"], info["redo_depth"] = _stack_depths(p)
        # RO verdict (SPEC §13.5 RO-4): fs writability of the ORIGINAL. Edits
        # keep working (they hit the working copy) — only save/rename back to
        # the original are gated, so the tooltip points at Save a copy.
        info["writable"] = os.access(p, os.W_OK)
        info["readonly_message"] = "" if info["writable"] else "Read-only"
        info["readonly_tooltip"] = (
            ""
            if info["writable"]
            else ("The file is read-only — edits can't be saved back to it. Use Save a copy.")
        )
        return info
    if action == "listdir":
        # `src` on the listdir action carries the server ORIGIN (mount-safe
        # routing), distinct from its add_to_library meaning (a file path).
        return _listdir(path, src)
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
        return _mutate(doc, expected_mtime, "rotate", lambda p: _rotate_pages(p, pages, degrees))
    if action == "delete_pages":
        return _mutate(doc, expected_mtime, "delete-pages", lambda p: _delete_pages(p, pages))
    if action == "reorder_pages":
        return _mutate(doc, expected_mtime, "reorder", lambda p: _reorder_pages(p, order))
    if action == "insert_blank":
        return _mutate(
            doc, expected_mtime, "insert-blank", lambda p: _insert_blank(p, at, width, height)
        )
    if action == "compress":
        return _mutate(doc, expected_mtime, f"compress-{level}", lambda p: _compress(p, level))
    if action == "edit_text":
        return _mutate(
            doc,
            expected_mtime,
            "edit-text",
            lambda p: _edit_text(
                p, page, bbox, origin, old_text, new_text, font, size, flags, color
            ),
        )
    if action == "extract_pages":
        return _extract_pages(doc, pages, name)
    if action == "merge":
        return _merge(sources, name, directory)
    if action == "split":
        return _split(doc, mode, ranges, prefix, directory)
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
    if action == "page_text":
        return _page_text(_open_work(doc)[0], page)
    if action == "undo":
        return _undo(doc)
    if action == "redo":
        return _redo(doc)
    raise ValueError(f"unknown action {action!r}")
