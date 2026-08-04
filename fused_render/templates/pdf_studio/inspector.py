"""Read-only inspection for pdf_studio: what kind of PDF this is, which pages
need OCR, what it draws with, what in it can act on its own, and the document as
Markdown. Nothing here writes to the PDF.

Loaded by pdf.py through importlib from this file's path — the built-in executor
leaves sys.path alone, so a template may not import a sibling by name. The
coupling is deliberately one-way and tiny: pdf.py resolves the working-copy path
and the page selection, and calls report(path, idx) / markdown(path, idx).
"""
import os
import re

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

# Keyed by the PDF /S action name -> (level, what it does, checklist row). /GoTo
# and friends must be listed as benign: treating unlisted types as suspect turns
# ordinary internal links into findings. The row lives here rather than being
# re-listed against each check, so adding an action type cannot leave it visible
# in the findings but missing from the row that summarises it.
ACTION_RISK = {
    "/JavaScript": ("danger", "runs JavaScript", "script"),
    "/Launch": ("danger", "launches an external program or file", "launch"),
    "/ImportData": ("danger", "reads form data off the local disk", "launch"),
    "/SubmitForm": ("warn", "posts form data to a URL", "submit"),
    "/GoToR": ("warn", "opens another document", "remote"),
    "/GoToE": ("warn", "opens an embedded document", "remote"),
    "/Movie": ("warn", "plays embedded video", "media"),
    "/Sound": ("warn", "plays embedded audio", "media"),
    "/Rendition": ("warn", "plays embedded rich media", "media"),
    "/RichMediaExecute": ("warn", "drives embedded Flash/3D content", "media"),
    "/URI": ("info", "opens a web link", "link"),
    "/GoTo": ("info", "jumps to a page in this document", "nav"),
    "/Named": ("info", "runs a viewer command", "nav"),
    "/Hide": ("info", "shows or hides page content", "nav"),
    "/SetOCGState": ("info", "toggles optional-content layers", "nav"),
    "/Thread": ("info", "follows an article thread", "nav"),
    "/Trans": ("info", "plays a page transition", "nav"),
    "/ResetForm": ("info", "clears form fields", "nav"),
    "/GoToDp": ("info", "jumps to a part of this document", "nav"),
}
UNKNOWN_ACTION = ("warn", "an unrecognised action type", "unknown")

# What counts as an action when found outside /A, /AA or /OpenAction.
ACTION_TYPES = frozenset(ACTION_RISK)

# Subtrees with nothing in them that can fire, skipped to keep the step budget
# for the parts of the file that can. Correctness does not depend on this list —
# what is in them is not an action because it has no /S, whether the walk looks
# or not, which is why an outline's /SE back into the structure tree is harmless.
SKIP_KEYS = frozenset({"/StructTreeRoot", "/Resources", "/Metadata"})

# Containers worth naming in a finding's location; anything else inherits its
# parent's label, so the walk stays complete even for containers not listed.
CONTAINER_LABELS = {
    "/Annots": "annotation",
    "/Outlines": "outline entry",
    "/Fields": "form field",
}
EXEC_EXTS = {".exe", ".dll", ".scr", ".bat", ".cmd", ".com", ".ps1", ".vbs",
             ".js", ".jse", ".jar", ".msi", ".lnk", ".hta", ".wsf", ".wsh", ".vbe",
             ".reg", ".sh", ".msc", ".pif", ".cpl", ".chm", ".url", ".appref-ms",
             ".docm", ".xlsm", ".pptm"}

# /AA events that need the reader to do something. Everything else — a page's
# /O and /C, a field's /K /F /V /C — happens on its own, so an unlisted event is
# assumed automatic: over-reporting a trigger is the safer way to be wrong.
CLICK_EVENTS = frozenset({"/E", "/X", "/D", "/U", "/Fo", "/Bl"})


def _inherited(obj, key, depth=16):
    """A form field key, looked up the way a viewer looks it up: /FT, /Ff and
    friends are inheritable, so a widget often carries none of its own."""
    import pikepdf

    while isinstance(obj, pikepdf.Dictionary) and depth > 0:
        if key in obj:
            return obj.get(key)
        obj, depth = obj.get("/Parent"), depth - 1
    return None


def _filespec(val):
    """The name out of a file specification. It is a string in the simple form
    and a dictionary in the full one — which is what Acrobat writes whenever
    there is a /UF — so a str() over the value put a multi-line dictionary repr
    where the reader needed a filename."""
    import pikepdf

    if isinstance(val, pikepdf.Dictionary):
        val = val.get("/UF") or val.get("/F")
    return str(val) if val is not None else ""


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
            # MuPDF reports "no unicode for this glyph" as 0, 0xFFFD or -1, and
            # ucs is a C int carrying whatever a hostile font maps to — so bound
            # both ends here. A value that is not a code point is exactly what
            # undecodable means, and letting one reach chr() below would take the
            # whole report down over one character.
            if ucs in (0, 0xFFFD) or not 0 <= ucs <= 0x10FFFF:
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
            # One font object listed under two resource names is one font. Only
            # an unembedded font with no xref has to fall back to the name.
            key = xref or refname
            f = seen.get(key)
            if f is None:
                name = basefont or "(unnamed)"
                subset = len(name) > 7 and name[6] == "+"
                f = seen[key] = {
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
        # An Identity CID font (H or V — vertical writing has the same problem)
        # with no ToUnicode map has no path back to characters: extraction gives
        # mojibake and OCR is the only way to read the page.
        f["undecodable"] = f["encoding"].startswith("Identity-") and not f["tounicode"]
    return fonts


def _is_int(obj):
    try:
        int(obj)
    except (TypeError, ValueError):
        return False
    return True


def _actions(pdf, cap=150_000):
    """Every action in the document, found by walking the object graph rather
    than by naming the places an action is allowed to sit.

    Naming them is what kept missing things — annotations, then outline entries,
    then form fields whose scripts live on a parent instead of the widget. A
    graph walk cannot miss a container: the worst an unknown one costs is a less
    specific label. Returns (actions, complete) — a `where` label per action, and
    `automatic` for the sources that need no click (/OpenAction, any /AA event,
    document scripts).

    An action always names its type in /S — the spec requires it — and where it
    was found decides how much benefit of the doubt that name gets: in an action
    slot (/A, /AA, /OpenAction, /Next) any /S counts, which catches unknown and
    obfuscated types; anywhere else it must name a type this scan knows, which
    keeps /S /Transparency on a group dict from becoming a finding. A dictionary
    with no /S is not an action however it was reached, so the keys whose meaning
    is "not an action" — a structure element's attributes, a movie annotation's
    activation dictionary, a destination's page — need no special case.
    """
    import pikepdf

    pages = {}
    for i, page in enumerate(pdf.pages, 1):
        og = page.obj.objgen
        if og != (0, 0):
            pages[og] = f"page {i}"

    found, seen, steps = [], set(), 0
    stack = [(pdf.Root, "document", False, False)]   # obj, label, automatic, forced

    def slot(val, label, auto):
        """An action slot — /A, an /AA event, /OpenAction, /Next — holds an action
        dictionary, or an array of them for a /Next chain. What is in it gets the
        benefit of the doubt about its /S, and nothing more; a destination array's
        page has no /S, so it goes on to be walked as the page it is."""
        for item in (val if isinstance(val, pikepdf.Array) else [val]):
            stack.append((item, label, auto, True))

    while stack and steps < cap:
        obj, label, auto, forced = stack.pop()
        steps += 1
        if isinstance(obj, pikepdf.Array):
            stack.extend((item, label, auto, forced) for item in obj)
            continue
        if not isinstance(obj, pikepdf.Dictionary):
            continue
        og = obj.objgen
        # Arriving at a page starts a new context, and not only for the label: a
        # page is never part of a trigger, so nothing on it fires because of how
        # the walk got here. A document that opens *at* page 5 does not click the
        # buttons on page 5.
        if og in pages:
            label, auto = pages[og], False
        # /S is a required key in an action dictionary, so /S is what makes
        # something an action; where it was found only decides how much benefit of
        # the doubt its value gets. Forcing on the slot alone was wrong because /A
        # is not always an action — a structure element keeps its attribute object
        # there, a movie annotation its playback activation dictionary — and
        # neither has an /S, which is how the spec distinguishes them. Outside a
        # slot a /Type that names something else is decisive: /S /Transparency on a
        # /Group, /S /JavaScript on a /CollectionSort are that dictionary's own
        # field, not an action.
        kind = str(obj.get("/S", ""))
        action = bool(kind) and (forced or (kind in ACTION_TYPES
                                            and str(obj.get("/Type", "/Action")) == "/Action"))
        if og != (0, 0):
            # Dedupe per reference, not per object. pikepdf's keys() is a set, so
            # which path reaches a shared object first is hash-randomised per
            # process — keying on the object alone let that decide whether an
            # action was recognised at all, and whether it counted as automatic,
            # so the same file scanned differently between runs. An object really
            # reachable as both an open action and a click is two findings,
            # because it is two triggers.
            key = (og, auto, action)
            if key in seen:
                continue
            seen.add(key)
        if action:
            found.append({"act": obj, "where": label, "automatic": auto})
            # /Next chains more actions onto the same trigger.
            if "/Next" in obj:
                slot(obj.get("/Next"), label, auto)
            continue
        for key in obj.keys():
            if key in SKIP_KEYS:
                continue
            val = obj.get(key)
            if key == "/OpenAction":
                slot(val, "document open action", True)
            elif key == "/A":
                slot(val, label, auto)
            elif key == "/AA" and isinstance(val, pikepdf.Dictionary):
                for event in val.keys():
                    slot(val.get(event), f"{label} trigger {event}",
                         event not in CLICK_EVENTS)
            elif key == "/JavaScript":
                stack.append((val, "document script", True, False))
            elif isinstance(val, (pikepdf.Dictionary, pikepdf.Array)):
                # Names, numbers and strings are leaves — pushing them only to
                # pop them again spends the step budget that keeps a pathological
                # file from walking forever.
                sub = CONTAINER_LABELS.get(key)
                inner = label if not sub else sub if label == "document" else f"{label} {sub}"
                stack.append((val, inner, auto, False))
    # A truncated walk has not ruled anything out, and the caller must say so
    # rather than report the rows it did reach as a clean bill of health.
    return found, not stack


def _note_attachment(name, where, facts, hits, alias=""):
    # The same file is commonly in both the /EmbeddedFiles tree and a
    # /FileAttachment annotation; it is one attachment, not two.
    if any(a["name"] == name for a in facts["attachments"]):
        return
    # Windows drops trailing dots and spaces when it runs a file, so "x.exe "
    # and "x.exe." both execute — strip them before reading the extension.
    ext = os.path.splitext(name.rstrip(" ."))[1].lower()
    executable = ext in EXEC_EXTS
    facts["attachments"].append({"name": name, "executable": executable,
                                 "alias": alias})
    if executable:
        hits.append({"level": "danger", "kind": "/EmbeddedFile",
                     "what": "carries an executable attachment",
                     "detail": f"{name} (filed as {alias})" if alias else name,
                     "where": where, "automatic": False, "row": "attachment"})


def _describe(entry, hits):
    """Turn one discovered action into a finding."""
    import pikepdf

    act, where = entry["act"], entry["where"]
    kind = str(act.get("/S"))
    level, what, row = ACTION_RISK.get(kind, UNKNOWN_ACTION)
    detail = ""
    if kind == "/URI":
        detail = str(act.get("/URI", ""))
    elif kind == "/Launch":
        win = act.get("/Win")
        detail = _filespec(act.get("/F")
                           or (win.get("/F") if isinstance(win, pikepdf.Dictionary) else None))
    elif kind == "/JavaScript":
        js = act.get("/JS")
        code = bytes(js.read_bytes()).decode("utf-8", "replace") if isinstance(js, pikepdf.Stream) else str(js or "")
        detail = " ".join(code.split())[:400]
    elif kind in ("/GoToR", "/GoToE", "/SubmitForm", "/ImportData"):
        detail = _filespec(act.get("/F"))
    # A /URI action is only a web link if it addresses the web. javascript: runs
    # script and file: reaches the local disk, so they belong with the things
    # that do that, not on the "opens a web link" row that never raises a finding.
    if kind == "/URI":
        scheme = detail.split(":", 1)[0].strip().lower() if ":" in detail else ""
        if scheme == "javascript":
            level, what, row = "danger", "runs JavaScript from a link", "script"
        elif scheme == "file":
            level, what, row = "danger", "opens a file on the local disk", "launch"
    hits.append({"level": level, "kind": kind, "what": what, "detail": detail,
                 "where": where, "automatic": entry["automatic"], "row": row})


def _security(path, disk=None):
    """Everything in the file that can act on its own, plus the encryption,
    signature and revision facts that go with deciding whether to trust it.
    `disk` is the document the user opened when `path` is a working copy of it."""
    import pikepdf

    hits = []
    urls = {}
    facts = {"attachments": [], "signatures": 0, "xfa": False, "acroform": False,
             "encrypted": False, "permissions": {}, "revisions": 0,
             "open_action": "", "layers": False, "linearized": False,
             "actions_complete": True}

    with pikepdf.open(path) as pdf:
        root = pdf.Root
        facts["encrypted"] = pdf.is_encrypted
        facts["linearized"] = pdf.is_linearized
        if pdf.is_encrypted:
            facts["permissions"] = {k: bool(v) for k, v in pdf.allow._asdict().items()}

        oa = root.get("/OpenAction")
        if isinstance(oa, pikepdf.Dictionary):
            facts["open_action"] = str(oa.get("/S", ""))
        elif isinstance(oa, pikepdf.Array):
            facts["open_action"] = "/GoTo"      # a destination array, not an action

        entries, facts["actions_complete"] = _actions(pdf)
        for entry in entries:
            _describe(entry, hits)

        acro = root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary):
            facts["acroform"] = True
            facts["xfa"] = "/XFA" in acro
            flags = acro.get("/SigFlags")
            facts["signatures"] = int(flags) & 1 if _is_int(flags) else 0

        facts["layers"] = "/OCProperties" in root

        # The /EmbeddedFiles key is a lookup name, not the file's name — a viewer
        # shows and extracts the filespec's /UF. They are usually the same, and a
        # payload.exe filed under "readme.txt" is exactly the case where they are
        # not, so the real name is what decides whether this is executable.
        for key, spec in pdf.attachments.items():
            name = _filespec(spec.obj) or key
            _note_attachment(name, "embedded files", facts, hits,
                             alias=key if key != name else "")

        # Object kinds rather than actions: what a thing IS, not what it runs.
        for i, page in enumerate(pdf.pages, 1):
            annots = page.get("/Annots")
            for annot in annots if isinstance(annots, pikepdf.Array) else ():
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                sub = str(annot.get("/Subtype", ""))
                # /FT is inheritable: a signature widget commonly carries no /FT
                # of its own, only its parent field does.
                if sub == "/Widget" and str(_inherited(annot, "/FT")) == "/Sig":
                    facts["signatures"] = max(facts["signatures"], 1)
                if sub in ("/RichMedia", "/Screen", "/Movie", "/3D", "/Sound"):
                    hits.append({"level": "warn", "kind": sub,
                                 "what": "embeds rich media", "detail": "",
                                 "where": f"page {i}", "automatic": False,
                                 "row": "media"})
                # The /EmbeddedFiles tree is not the only way in: a filespec on
                # an annotation is still openable from the viewer.
                if sub == "/FileAttachment":
                    _note_attachment(_filespec(annot.get("/FS")) or "(unnamed)",
                                     f"page {i} attachment", facts, hits)

    for h in hits:
        if h["kind"] == "/URI" and h["detail"]:
            urls[h["detail"]] = urls.get(h["detail"], 0) + 1

    # Revisions and web-optimization are properties of the file the reader has,
    # not of the working copy this scan reads: pdf_studio saves text edits
    # incrementally, so a document the user has just typed into would otherwise
    # report the user's own edits as somebody's hidden history.
    if disk and disk != path:
        with pikepdf.open(disk) as on_disk:
            facts["linearized"] = on_disk.is_linearized
    else:
        disk = path

    # An in-place update appends a revision ending in its own %%EOF — but so does
    # a linearized file's first-page xref, which has to be discounted or every
    # web-optimized PDF looks rewritten. A byte count is a floor, not a proof
    # (%%EOF can occur inside a stream), hence "sections" and not "edits".
    facts["revisions_checked"] = os.path.getsize(disk) <= 64 * 1024 * 1024
    if facts["revisions_checked"]:
        with open(disk, "rb") as f:
            eofs = f.read().count(b"%%EOF")
        facts["revisions"] = max(0, eofs - 1 - (1 if facts["linearized"] else 0))

    return _security_report(hits, urls, facts)


def _security_report(hits, urls, facts):
    """Fold the evidence into one checklist row per thing a reader wants ruled
    out, plus the findings behind each row."""
    by_row = {}
    for h in hits:
        by_row.setdefault(h["row"], []).append(h)

    covered = set()

    def rows(name):
        covered.add(name)
        return by_row.get(name, [])

    checks = []

    def check(name, state, note):
        checks.append({"name": name, "state": state, "note": note})

    # First, because it qualifies every row under it.
    if not facts.get("actions_complete", True):
        check("Scan coverage", "warn",
              "This document's object graph is larger than the scan walks —"
              " what follows is what was reached, not the whole file")

    js = rows("script")
    check("JavaScript", "fail" if js else "pass",
          f"{len(js)} script action{'' if len(js) == 1 else 's'} in the document"
          if js else "No scripts embedded")

    launch = rows("launch")
    check("Launch / local file access", "fail" if launch else "pass",
          "; ".join(sorted({h["detail"] or h["what"] for h in launch}))
          if launch else "Nothing runs or reads local files")

    auto = [h for h in hits if h["level"] in ("danger", "warn") and h["automatic"]]
    check("Automatic actions", "warn" if auto else "pass",
          f"{len(auto)} action{' fires' if len(auto) == 1 else 's fire'} without a click"
          if auto else ("Opens to a page destination" if facts["open_action"] == "/GoTo"
                        else "Nothing fires on open"))

    att = facts["attachments"]
    bad_att = [a for a in att if a["executable"]]
    rows("attachment")   # summarised from facts below, which knows more than the hit
    check("Embedded files", "fail" if bad_att else ("warn" if att else "pass"),
          ", ".join(a["name"] for a in att) if att else "No attachments")

    submit = rows("submit")
    check("Forms", "warn" if (submit or facts["xfa"]) else
          ("info" if facts["acroform"] else "pass"),
          "XFA form" if facts["xfa"] else
          (f"{len(submit)} submit action{'' if len(submit) == 1 else 's'}" if submit else
           ("Fillable AcroForm fields" if facts["acroform"] else "No form fields")))

    remote = rows("remote")
    insecure = sorted(u for u in urls if u.lower().startswith("http://")
                      or re.match(r"^\w+://\d{1,3}(\.\d{1,3}){3}", u.lower()))
    check("External references", "warn" if (remote or insecure) else
          ("info" if urls else "pass"),
          "; ".join(filter(None, [
              f"{len(urls)} link target{'' if len(urls) == 1 else 's'}" if urls else "",
              f"{len(insecure)} not over HTTPS" if insecure else "",
              f"{len(remote)} reference{'' if len(remote) == 1 else 's'} to other files" if remote else "",
          ])) or "No outbound links")

    media = rows("media")
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

    # Whatever no row above asked for still gets summarised here, so a finding
    # the reader can see can never sit behind a checklist that says "pass" —
    # including kinds added to ACTION_RISK after this function was written.
    rest = [h for h in hits if h["level"] in ("danger", "warn") and h["row"] not in covered]
    if rest:
        check("Other active content",
              "fail" if any(h["level"] == "danger" for h in rest) else "warn",
              "; ".join(sorted({h["what"] for h in rest})))

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


def _security_or_unreadable(path, disk=None):
    """The one place the scan is allowed to give up. Walking a damaged object
    graph can raise from deep inside pikepdf (an undecodable /JS stream, a
    rotten tree), and a file like that is exactly the kind worth a verdict — so
    the scan degrades to "unreadable" instead of taking the whole report with
    it. A structure this broken is itself the finding.

    What it catches is deliberately what malformed *input* raises. NameError and
    AttributeError are left out: those are bugs in this module, and swallowing
    them here would turn a broken scanner into a plausible-looking report."""
    import pikepdf

    # Ask pikepdf which exceptions it has rather than listing them: 11 of its 13
    # derive from Exception directly, not from PdfError, so a hand-kept list
    # missed DataDecodingError once and PasswordError (a locked file) again.
    from_pikepdf = tuple(v for v in vars(pikepdf).values()
                         if isinstance(v, type) and issubclass(v, Exception))
    try:
        return _security(path, disk)
    except from_pikepdf + (TypeError, ValueError, KeyError, IndexError,
                           RecursionError, UnicodeDecodeError, OSError) as e:
        return {"risk": "unreadable", "error": f"{type(e).__name__}: {e}",
                "checks": [], "findings": [], "findings_total": 0,
                "urls": [], "facts": {}}


def _rust_engine(path):
    """firecrawl/pdf-inspector's own verdict, when it is installed. It is a
    cross-check, never a requirement: absent it reports how to install it, and
    if it is present but chokes on a file that is its problem to report, not a
    reason to lose the whole inspection."""
    try:
        import pdf_inspector
    except ImportError:
        return {"installed": False, "hint": "pip install pdf-inspector"}
    except Exception as e:
        # A broken native wheel can raise more than ImportError on import; that
        # is the package's problem to report, not a reason to lose the whole
        # inspection the bundled engines already produced.
        return {"installed": True, "error": f"{type(e).__name__}: {e}"}
    try:
        r = pdf_inspector.process_pdf(path)
    except Exception as e:
        return {"installed": True, "error": f"{type(e).__name__}: {e}"}
    return {
        "installed": True,
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
    return " ".join("".join(parts).split())


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


# Regions of finished markdown that must be left exactly as they are: an
# existing link, and a code span (an indented code line is skipped whole).
MD_KEEP = re.compile(r"\[[^\]]*\]\([^)]*\)|`[^`]*`")


def _wrap_link(md, anchor, uri):
    """Wrap the first whole-word occurrence of `anchor` that is not already part
    of a link. A plain replace would hit "here" inside "Adherence", the anchor
    of a link placed earlier on the line, or a word inside another link's URL."""
    lead = r"\b" if anchor[:1].isalnum() else ""
    trail = r"\b" if anchor[-1:].isalnum() else ""
    pat = re.compile(lead + re.escape(anchor) + trail)
    # A URL containing brackets or spaces has to go in <> or it ends the link early.
    link = f"[{anchor}](<{uri}>)" if re.search(r"[()<>\s]", uri) else f"[{anchor}]({uri})"
    # A callable replacement, not a string: a backslash in the anchor or URL is a
    # literal here, where re.sub's string form would read it as \1 or \t and
    # either raise or rewrite the link.
    repl = lambda m: link
    out, pos, done = [], 0, False
    for m in MD_KEEP.finditer(md):
        seg = md[pos:m.start()]
        if not done:
            seg, n = pat.subn(repl, seg, count=1)
            done = bool(n)
        out.append(seg)
        out.append(m.group(0))
        pos = m.end()
    tail = md[pos:]
    if not done:
        tail = pat.sub(repl, tail, count=1)
    return "".join(out) + tail


BARE_URL = re.compile(r"(?<![(\[!])\b(https?://[^\s<>()\[\]]+)")


def _linkify(md):
    """Bare URLs become links — but not inside an existing link, a code span or
    an indented code line, where a rewrite would corrupt code rather than
    decorate prose."""
    out = []
    for line in md.split("\n"):
        if line.startswith("    "):
            out.append(line)
            continue
        pos, buf = 0, []
        for m in MD_KEEP.finditer(line):
            buf.append(BARE_URL.sub(r"[\1](\1)", line[pos:m.start()]))
            buf.append(m.group(0))
            pos = m.end()
        buf.append(BARE_URL.sub(r"[\1](\1)", line[pos:]))
        out.append("".join(buf))
    return "\n".join(out)


def _link_text(md, line, links):
    import fitz

    lbox = fitz.Rect(line["bbox"])
    for rect, anchor, uri in links:
        # No "already linked this URI" check: two annotations on one line can
        # point at the same target, and _wrap_link only ever touches text that
        # is not already inside a link, so calling it per annotation is right.
        if abs(rect & lbox) <= 0:
            continue
        if "[" not in anchor and "]" not in anchor:
            md = _wrap_link(md, anchor, uri)
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


# ------------------------------------------------------------------ public API
# pdf.py owns paths and page selection; both entry points take a resolved file
# path and a list of 0-based page indices.
def report(path, idx, disk=None):
    import fitz

    if not idx:
        raise ValueError("this document has no pages to inspect")
    d = fitz.open(path)
    # ~70 ms/page, so past the cap sample evenly, keeping the first and last.
    sampled = len(idx) > PAGE_SCAN_CAP
    if sampled:
        step = (len(idx) - 1) / (PAGE_SCAN_CAP - 1)
        idx = sorted({idx[round(i * step)] for i in range(PAGE_SCAN_CAP)})
    scans = [_page_scan(d[i], tables=n < TABLE_SCAN_CAP) for n, i in enumerate(idx)]
    verdict = _classify(scans)
    verdict["sampled"] = sampled
    m = d.metadata or {}
    doc_facts = {
        "size": os.path.getsize(path), "pdf_version": m.get("format", ""),
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
    fonts = _font_report(d, idx)
    d.close()
    return {
        "doc": doc_facts,
        "verdict": verdict,
        "pages": scans,
        "fonts": fonts,
        "text": _text_quality(scans),
        "security": _security_or_unreadable(path, disk),
        "rust": _rust_engine(path),
        "tables_capped": len(idx) > TABLE_SCAN_CAP,
    }


def markdown(path, idx):
    import fitz

    # find_tables() runs per page here; a whole large document would blow the
    # 60 s call timeout and return nothing at all.
    if len(idx) > MD_PAGE_CAP:
        raise ValueError(
            f"Markdown conversion is capped at {MD_PAGE_CAP} pages per run "
            f"({len(idx)} selected) — convert a range, e.g. 1-{MD_PAGE_CAP}")
    d = fitz.open(path)
    parts = [f"<!-- page {i + 1} -->\n\n" + _page_markdown(d[i]) for i in idx]
    d.close()
    md = _linkify("\n\n---\n\n".join(parts).strip() + "\n")
    return {"markdown": md, "chars": len(md), "pages": len(idx)}
