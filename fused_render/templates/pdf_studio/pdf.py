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

The source of truth is the .pdf files on disk, wherever they live — projects
link imported files instead of copying them. Edits never touch the original
directly: each open doc gets a working copy under .work/ that mutations (and
undo/redo snapshots) apply to; an explicit save writes the working copy back
over the original. Each call is a fresh process, so no in-memory state
survives.

Actions
  health                                   -> {ok, pymupdf, pikepdf}
  list_projects                            -> {projects:[...], dir}
  new_project(title)                       -> {slug}
  rename_project(slug,title)               -> {slug}
  duplicate_project(slug,title)            -> {slug}
  delete_project(slug)                     -> {ok}
  open_project(slug)                       -> {slug, title, docs:[...]}
  open_doc(doc)                            -> docinfo + {work, dirty, has_text, undo_depth, redo_depth}
  which_project(doc)                       -> {slug}  ("" when in no project)
  listdir(path)                            -> {path, parent, dirs, files}
  import_doc(project,src)                  -> {name, path}  (links src in place, no copy)
  import_url(project,url,name)             -> {name, path}
  delete_doc(doc,project)                  -> {ok}  (linked docs are only unlinked)
  rename_doc(doc,name,project)             -> {name, path}
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
  merge(project,sources,name,directory)    -> {name, path, dir}
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
import time

# NOTE: bare `def main` (no @fused.udf) is deliberate — under the built-in
# executor the worker calls main() by its own signature; @fused.udf hides that
# signature and triggers a hosted-auth flow that times out.

# State lives under the user home dir, never inside the installed template
# package (D76-adjacent). The project library holds primary, non-regenerable
# content (merge output, URL imports) so it gets its own `data/` root rather
# than sitting under `cache/`, which a future clear-cache action could sweep;
# working copies and undo snapshots are transient and belong under `cache/`.
DATA_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "data", "pdf_studio"))
CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "pdf_studio"))
PROJECTS = os.path.join(DATA_ROOT, "projects")
EXPORTS = os.path.join(CACHE_ROOT, "exports")
SNAPSHOTS = os.path.join(CACHE_ROOT, "snapshots")   # undo stacks for out-of-library docs
WORKDIR = os.path.join(CACHE_ROOT, "work")          # per-doc working copies (unsaved edits)
DEMO_SLUG = "pdf-studio-demo"

UNDO_CAP = 10


# ---------------------------------------------------------------------- helpers
def _safe_slug(s: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", (s or "").strip())[:64].strip("-")
    return slug or "untitled"


def _safe_name(name, default):
    name = re.sub(r'[\\/:*?"<>|]', "-", (name or "").strip()).strip(". ")
    return name or default


def _project_dir(slug: str) -> str:
    return os.path.join(PROJECTS, _safe_slug(slug))


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
    proj = os.path.realpath(PROJECTS)
    if doc.startswith(proj + os.sep):
        rel = os.path.relpath(doc, proj)
        slug = rel.split(os.sep)[0]
        d = os.path.join(proj, slug, ".history", os.path.basename(doc))
    else:
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


def _merge(project, sources, name, directory=""):
    import pikepdf

    paths = [os.path.abspath(p) for p in json.loads(sources)]
    if len(paths) < 2:
        raise ValueError("merge needs at least two PDFs")
    out_dir = _out_dir(directory,
                       _project_dir(project) if project else os.path.dirname(paths[0]))
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
        if level == "aggressive":
            import fitz

            if before > 80 * 1024 * 1024:
                raise ValueError("file too large for aggressive compression — use lossless")
            d = fitz.open(path)
            d.rewrite_images(dpi_threshold=200, dpi_target=150, quality=75)
            d.subset_fonts()
            tmp = path + ".tmp"
            d.save(tmp, garbage=4, deflate=True, clean=True,
                   encryption=fitz.PDF_ENCRYPT_KEEP)
            d.close()
            os.replace(tmp, path)
        else:
            with pikepdf.open(path, allow_overwriting_input=True) as pdf:
                pdf.save(path, compress_streams=True, recompress_flate=True,
                         object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return {"before": before, "after": os.path.getsize(path)}
    return fn(doc)


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


# ------------------------------------------------------------------- projects
def _demo_pdf(dest):
    import fitz

    blue, ink, ink2 = (0.09, 0.43, 0.88), (0.11, 0.13, 0.15), (0.35, 0.4, 0.46)
    d = fitz.open()
    w, h = 612, 792
    m = 64

    p = d.new_page(width=w, height=h)
    p.draw_rect(fitz.Rect(0, 0, w, 8), color=None, fill=blue)
    p.insert_text((m, 130), "PDF Studio", fontname="hebo", fontsize=34, color=ink)
    p.insert_text((m, 158), "A demo document you can safely mangle", fontname="helv",
                  fontsize=14, color=ink2)
    p.insert_textbox(fitz.Rect(m, 200, w - m, 340),
                     "This three-page PDF was generated locally so the studio has "
                     "something to open on first run. Try the page operations from "
                     "the Page menu, merge it with another file, or switch to Edit "
                     "mode and click any line of text to rewrite it in place.",
                     fontname="tiro", fontsize=12.5, color=ink, lineheight=1.5)
    p.insert_text((m, 380), "What to try", fontname="hebo", fontsize=16, color=ink)
    for i, tip in enumerate(["Rotate or delete pages from the thumbnail rail",
                             "Split this file into one PDF per page",
                             "Compress it and compare the size",
                             "Edit this very line of text"]):
        y = 410 + i * 26
        p.draw_circle(fitz.Point(m + 4, y - 4), 2.5, color=None, fill=blue)
        p.insert_text((m + 18, y), tip, fontname="helv", fontsize=12, color=ink)

    p = d.new_page(width=w, height=h)
    p.insert_text((m, 90), "2. Tables survive page ops", fontname="hebo",
                  fontsize=18, color=ink)
    rows = [("Operation", "Library", "Speed"),
            ("Merge / split / rotate", "pikepdf (qpdf)", "instant"),
            ("Compress", "pikepdf / PyMuPDF", "fast"),
            ("Text editing", "PyMuPDF", "per edit"),
            ("Rendering", "pdf.js (browser)", "local")]
    x0, y0, rh, cw = m, 130, 30, [200, 170, 114]
    for r, row in enumerate(rows):
        y = y0 + r * rh
        if r == 0:
            p.draw_rect(fitz.Rect(x0, y, x0 + sum(cw), y + rh), color=None,
                        fill=(0.91, 0.94, 1.0))
        x = x0
        for c, cell in enumerate(row):
            p.insert_text((x + 10, y + 20), cell,
                          fontname="hebo" if r == 0 else "helv",
                          fontsize=11.5, color=ink if r else (0.06, 0.36, 0.77))
            x += cw[c]
        p.draw_line(fitz.Point(x0, y), fitz.Point(x0 + sum(cw), y), color=(0.88, 0.89, 0.91))
    y = y0 + len(rows) * rh
    p.draw_line(fitz.Point(x0, y), fitz.Point(x0 + sum(cw), y), color=(0.88, 0.89, 0.91))
    p.insert_textbox(fitz.Rect(m, y + 40, w - m, y + 160),
                     "Every mutation snapshots the file first, so undo and redo "
                     "work across sessions. External edits are caught by an mtime "
                     "check before each write.",
                     fontname="tiro", fontsize=12.5, color=ink, lineheight=1.5)

    p = d.new_page(width=w, height=h)
    p.insert_text((m, 90), "3. Vector art is preserved", fontname="hebo",
                  fontsize=18, color=ink)
    vals = [0.35, 0.6, 0.45, 0.8, 0.65, 0.95]
    bw, gap, base_y, max_h = 48, 22, 420, 220
    for i, v in enumerate(vals):
        x = m + i * (bw + gap)
        p.draw_rect(fitz.Rect(x, base_y - v * max_h, x + bw, base_y),
                    color=None, fill=(0.09 + 0.1 * i / 6, 0.43, 0.88 - 0.06 * i))
        p.insert_text((x + 12, base_y + 18), f"Q{i + 1}", fontname="helv",
                      fontsize=10, color=ink2)
    p.draw_line(fitz.Point(m, base_y), fitz.Point(w - m, base_y), color=ink2)
    p.insert_textbox(fitz.Rect(m, 470, w - m, 570),
                     "Text edits use redaction plus re-insertion, scoped so "
                     "drawings and images on the page are left untouched.",
                     fontname="tiro", fontsize=12.5, color=ink, lineheight=1.5)
    d.save(dest, deflate=True)
    d.close()


def _ensure_demo():
    os.makedirs(PROJECTS, exist_ok=True)
    marker = os.path.join(PROJECTS, ".demo_seeded")
    if os.path.exists(marker):
        return
    dest = _project_dir(DEMO_SLUG)
    try:
        if not os.path.exists(dest):
            os.makedirs(dest)
            _demo_pdf(os.path.join(dest, "welcome.pdf"))
            with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"title": "PDF Studio — Demo", "created": time.time(),
                           "demo": True}, f)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception as e:
        print(f"[python] demo seed failed: {e}")


def _meta(d):
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_meta(d, meta):
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _doc_entry(path, linked=False):
    entry = {"name": os.path.basename(path), "path": _fwd(path),
             "size": os.path.getsize(path), "mtime": os.path.getmtime(path)}
    if linked:
        entry["linked"] = True
    try:
        import pikepdf
        with pikepdf.open(_cur_path(path)) as pdf:
            entry["page_count"] = len(pdf.pages)
    except Exception as e:
        entry["page_count"] = None
        entry["error"] = str(e)
    return entry


def _project_docs(d):
    docs = []
    for name in sorted(os.listdir(d), key=str.lower):
        full = os.path.join(d, name)
        if not name.lower().endswith(".pdf") or not os.path.isfile(full):
            continue
        docs.append(_doc_entry(full))
    for link in _meta(d).get("links", []):
        full = os.path.abspath(link)
        if not os.path.isfile(full):
            docs.append({"name": os.path.basename(full), "path": _fwd(full),
                         "size": 0, "mtime": 0, "page_count": None,
                         "linked": True, "missing": True})
            continue
        docs.append(_doc_entry(full, linked=True))
    docs.sort(key=lambda e: e["name"].lower())
    return docs


def _list_projects():
    os.makedirs(PROJECTS, exist_ok=True)
    _ensure_demo()
    out = []
    for name in sorted(os.listdir(PROJECTS)):
        d = os.path.join(PROJECTS, name)
        if not os.path.isdir(d):
            continue
        meta = _meta(d)
        pdfs = [f for f in os.listdir(d) if f.lower().endswith(".pdf")]
        out.append({"slug": name, "title": meta.get("title", name),
                    "ndocs": len(pdfs) + len(meta.get("links", [])),
                    "demo": bool(meta.get("demo")),
                    "mtime": os.path.getmtime(d)})
    out.sort(key=lambda e: -e["mtime"])
    return {"projects": out, "dir": _fwd(PROJECTS)}


def _new_project(title):
    slug = _safe_slug(title or "untitled")
    if os.path.exists(_project_dir(slug)):
        i = 2
        while os.path.exists(_project_dir(f"{slug}-{i}")):
            i += 1
        slug = f"{slug}-{i}"
    d = _project_dir(slug)
    os.makedirs(d)
    _write_meta(d, {"title": title or slug, "created": time.time()})
    return {"slug": slug}


def _open_project(slug):
    d = _project_dir(slug)
    if not os.path.isdir(d):
        raise ValueError(f"no such project: {slug}")
    meta = _meta(d)
    return {"slug": _safe_slug(slug), "title": meta.get("title", slug),
            "docs": _project_docs(d)}


def _import_doc(project, src):
    """Link src into the project — the original file stays where it is."""
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {src}")
    d = _project_dir(project)
    if not os.path.isdir(d):
        raise ValueError(f"no such project: {project}")
    if os.path.realpath(os.path.dirname(src)) == os.path.realpath(d):
        return {"name": os.path.basename(src), "path": _fwd(src)}
    meta = _meta(d)
    links = meta.get("links", [])
    if not any(_same_path(l, src) for l in links):
        meta["links"] = links + [_fwd(src)]
        _write_meta(d, meta)
    return {"name": os.path.basename(src), "path": _fwd(src)}


def _import_url(project, url, name):
    import urllib.request

    d = _project_dir(project)
    if not os.path.isdir(d):
        raise ValueError(f"no such project: {project}")
    req = urllib.request.Request(url, headers={"User-Agent": "fused-render-pdf/1.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read()
    if data[:5] != b"%PDF-":
        raise ValueError("URL did not return a PDF")
    name = _safe_name(name or os.path.basename(url.split("?")[0]), "imported")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    dest = _unique_path(d, name)
    with open(dest, "wb") as f:
        f.write(data)
    return {"name": os.path.basename(dest), "path": _fwd(dest)}


def _listdir(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
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
    return {"files": [{"path": _fwd(p), "name": os.path.basename(p),
                       "size": os.path.getsize(p)} for p in files],
            "dir": _fwd(out)}


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
    action: str = "list_projects",
    slug: str = "",
    title: str = "",
    doc: str = "",
    project: str = "",
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
    os.makedirs(PROJECTS, exist_ok=True)

    if action == "health":
        return _health()
    if action == "list_projects":
        return _list_projects()
    if action == "new_project":
        return _new_project(title)
    if action == "rename_project":
        d = _project_dir(slug)
        if not os.path.isdir(d):
            raise ValueError(f"no such project: {slug}")
        meta = _meta(d)
        meta["title"] = title or slug
        _write_meta(d, meta)
        return {"slug": _safe_slug(slug)}
    if action == "duplicate_project":
        srcd = _project_dir(slug)
        if not os.path.isdir(srcd):
            raise ValueError(f"no such project: {slug}")
        out = _new_project(title or (_meta(srcd).get("title", slug) + " copy"))
        dstd = _project_dir(out["slug"])
        for f in os.listdir(srcd):
            if f.lower().endswith(".pdf"):
                shutil.copyfile(os.path.join(srcd, f), os.path.join(dstd, f))
        links = _meta(srcd).get("links", [])
        if links:
            meta = _meta(dstd)
            meta["links"] = links
            _write_meta(dstd, meta)
        return out
    if action == "delete_project":
        d = _project_dir(slug)
        if not os.path.isdir(d):
            raise ValueError(f"no such project: {slug}")
        shutil.rmtree(d)
        return {"ok": True}
    if action == "open_project":
        return _open_project(slug)
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
        # keep working (they hit the working copy) — only save/rename/delete
        # back to the original are gated, so the tooltip points at Save a copy.
        info["writable"] = os.access(p, os.W_OK)
        info["readonly_message"] = "" if info["writable"] else "Read-only"
        info["readonly_tooltip"] = "" if info["writable"] else (
            "The file is read-only — edits can't be saved back to it. "
            "Use Save a copy.")
        return info
    if action == "which_project":
        p = os.path.normcase(os.path.realpath(os.path.abspath(doc)))
        for name in sorted(os.listdir(PROJECTS)):
            d = os.path.join(PROJECTS, name)
            if not os.path.isdir(d):
                continue
            if p.startswith(os.path.normcase(os.path.realpath(d)) + os.sep):
                return {"slug": name}
            if any(_same_path(l, doc) for l in _meta(d).get("links", [])):
                return {"slug": name}
        return {"slug": ""}
    if action == "listdir":
        return _listdir(path)
    if action == "import_doc":
        return _import_doc(project, src)
    if action == "import_url":
        return _import_url(project, url, name)
    if action == "delete_doc":
        p = os.path.abspath(doc)
        d = _project_dir(project) if project else ""
        meta = _meta(d) if d and os.path.isdir(d) else {}
        links = meta.get("links", [])
        kept = [l for l in links if not _same_path(l, p)]
        if len(kept) != len(links):
            meta["links"] = kept
            _write_meta(d, meta)
        else:
            if not os.path.isfile(p):
                raise ValueError(f"no such file: {p}")
            # RO gate (SPEC §13.5 RO-3): os.remove is a parent-directory op and
            # would silently delete a chmod -w file. Linked docs (branch above)
            # only edit project metadata, so they are not gated.
            if not os.access(p, os.W_OK):
                raise PermissionError(f"{p!r} is read-only")
            os.remove(p)
        shutil.rmtree(_hist_dir(p), ignore_errors=True)
        _work_drop(p)
        return {"ok": True}
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
        if project:
            d = _project_dir(project)
            meta = _meta(d) if os.path.isdir(d) else {}
            links = meta.get("links", [])
            if any(_same_path(l, p) for l in links):
                meta["links"] = [_fwd(dest) if _same_path(l, p) else l for l in links]
                _write_meta(d, meta)
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
        return _merge(project, sources, name, directory)
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
