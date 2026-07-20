# /// script
# dependencies = ["python-pptx", "Pillow", "fpdf2"]
# ///
"""Backend dispatcher for the slides template — an AI-native .pptx viewer/editor.

One bare `main(action=...)` function (the fused-render contract). It owns
everything that needs Python; the browser owns the live canvas UI. `engine.py`
holds the DOM-free parse/build/model-ops core so the AI agent and the UI edit
through *identical* canonical semantics.

State model
-----------
The document IS the target file (`_file`, a .pptx). Its canonical, AI-addressable
model — parsed once from the pptx and edited from then on — is cached by
content hash under ``~/.fused-render/cache/slides/<hash>/``:
  * ``model.json``  – canonical slide model (SOURCE OF TRUTH while editing)
  * ``media/``      – extracted / uploaded images referenced by element ``src``
The hash folds in ``engine.ENGINE_V`` so a parser change auto-invalidates old
caches. The original .pptx is never mutated except by an explicit ``save``
(overwrite) or ``save_as`` (new file) action, both of which write atomically.
Since the hash is content-derived, every ``save`` moves the doc to a new
cache dir; ``save`` removes the prior dir once the new one lands so re-saves
don't leak one orphaned ``model.json``/``media/`` folder per save.
A per-document display-name override (renaming the deck without renaming the
file) lives in the shared JSON sidecar next to the file (``<file>.json``),
namespaced under the "slides" key, alongside whatever other templates keep
there (e.g. claudeSessions).

AI-native surface (call these directly to edit a deck without the browser):
  get_model, update_element, set_text, add_text, add_image, delete_element,
  order_element, align_elements, set_slide_bg,
  add_slide, delete_slide, duplicate_slide, move_slide, export.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

# Rebuild __file__ under the openfused exec path (harmless under builtin).
if "__file__" not in globals():
    import sys
    __file__ = os.path.join(sys.path[0], "slides.py")

import engine  # sibling module; cwd is set to the .py's dir

CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "slides"))
UPLOADS = os.path.join(tempfile.gettempdir(), "fused_render_slides_uploads")
EXPORTS = os.path.join(tempfile.gettempdir(), "fused_render_slides_exports")

# Translate a stored path (may be WSL /mnt/c/... or native C:\...) to this OS's convention.
_WSL_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _to_native_path(p):
    if not p:
        return p
    if os.name == "nt":
        m = _WSL_MOUNT_RE.match(p)
        if m:
            drive, rest = m.group(1).upper(), (m.group(2) or "").replace("/", "\\")
            return f"{drive}:{rest or chr(92)}"
    else:
        m = _WIN_DRIVE_RE.match(p)
        if m:
            drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
    return p


# --------------------------------------------------------------------------- #
#  content-hash cache                                                         #
# --------------------------------------------------------------------------- #
def _content_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(f"|engine{engine.ENGINE_V}".encode())
    return h.hexdigest()[:16]


def _cache_dir(doc):
    return os.path.join(CACHE_ROOT, doc)


def _model_path(doc):
    return os.path.join(_cache_dir(doc), "model.json")


def _media_dir(doc):
    return os.path.join(_cache_dir(doc), "media")


def _load_model(doc):
    with open(_model_path(doc), "r", encoding="utf-8") as f:
        return json.load(f)


def _save_model(doc, model, expected_mtime=None):
    """Write model.json with an optimistic mtime lock. Returns a result dict."""
    mp = _model_path(doc)
    if expected_mtime not in (None, "", "0"):
        cur = os.path.getmtime(mp) if os.path.exists(mp) else 0
        if abs(cur - float(expected_mtime)) > 1e-6:
            return {"conflict": True, "mtime": cur}
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False)
    os.replace(tmp, mp)   # atomic
    return {"ok": True, "mtime": os.path.getmtime(mp), "model": model}


# --------------------------------------------------------------------------- #
#  sidecar store (shared with other templates — see templates/claude/agent.py) #
# --------------------------------------------------------------------------- #
def _sidecar_path(file):
    return file + ".json"


def _load_sidecar(file):
    try:
        with open(_sidecar_path(file), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = {}
    return data


def _save_sidecar(file, data):
    path = _sidecar_path(file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sidecar_writable(file):
    """True iff _save_sidecar would succeed (SPEC RO-3, annotate's rule): an
    existing sidecar needs W_OK on itself (the mkstemp + os.replace above would
    otherwise bypass its read-only bit via the directory), a fresh one needs
    W_OK on the directory (mkstemp and replace both land there)."""
    path = _sidecar_path(os.path.abspath(file))
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(os.path.dirname(path), os.W_OK)


_READONLY_TOOLTIP = ("The file is read-only — its permissions don't allow "
                     "writing, so it can't be edited here.")


def _editability(file):
    """RO-4 verdict folded into the open response:
    (editable, readonly_message, readonly_tooltip). Read-only gates only the
    explicit save-overwrite of the .pptx — model edits live in the cache and
    stay allowed — but the UI surfaces the verdict up front."""
    if os.path.exists(file) and not os.access(file, os.W_OK):
        return False, "Read-only", _READONLY_TOOLTIP
    return True, "", ""


def _get_title(file):
    return _load_sidecar(file).get("slides", {}).get("title") or None


def _set_title(file, title):
    data = _load_sidecar(file)
    ns = data.get("slides")
    if not isinstance(ns, dict):
        ns = {}
    if title:
        ns["title"] = title
    else:
        ns.pop("title", None)
    data["slides"] = ns
    _save_sidecar(file, data)


# --------------------------------------------------------------------------- #
#  main dispatcher                                                            #
# --------------------------------------------------------------------------- #
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


def main(action: str = "open",
         file: str = "", doc: str = "",
         slide: str = "", el: str = "",
         text: str = "", patch: str = "", ids: str = "",
         mode: str = "", src: str = "", name: str = "",
         index: int = -1, z: str = "",
         x: float = 0, y: float = 0, w: float = 0, h: float = 0,
         size: float = 0, align: str = "", color: str = "",
         background: str = "",
         model_json: str = "", expected_mtime: str = "",
         token: str = "", data: str = "", fmt: str = "pptx",
         row: int = -1, col: int = -1, path: str = "", directory: str = "",
         title: str = ""):

    os.makedirs(CACHE_ROOT, exist_ok=True)

    # -------------------------------------------------------------------- open
    if action == "open":
        if not file:
            raise ValueError("open requires `file` (path to a .pptx)")
        file = _to_native_path(os.path.abspath(os.path.expanduser(file)))
        if not os.path.exists(file):
            raise FileNotFoundError(file)
        d = _content_hash(file)
        mp = _model_path(d)
        if os.path.exists(mp):
            model = _load_model(d)
        else:
            media_dir = _media_dir(d)
            os.makedirs(media_dir, exist_ok=True)
            model = engine.parse_pptx(file, media_dir, "media")
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(model, f, ensure_ascii=False)
        editable, ro_msg, ro_tip = _editability(file)
        return {"doc": d, "model": model, "mtime": os.path.getmtime(mp),
                "media_dir": _media_dir(d).replace(os.sep, "/"),
                "title": _get_title(file),
                "editable": editable, "readonly_message": ro_msg,
                "readonly_tooltip": ro_tip,
                "sidecar_writable": _sidecar_writable(file)}

    # ----------------------------------------- directory browser (Save as)
    if action == "listdir":
        path = _to_native_path(path)
        base = os.path.abspath(os.path.expanduser(path)) if path else os.path.expanduser("~")
        dirs, files = [], []
        # `src` on the listdir action carries the server ORIGIN for mount-safe
        # routing (distinct from its add-image meaning, an image src).
        if _remote_dir(src, base):
            # Mount-backed dir: list via /api/fs/list, never a kernel scan.
            try:
                ents, _ = _list_remote(src, base)
            except Exception:  # noqa: BLE001
                ents = []
            for ent in ents:
                nm = ent["name"]
                if nm.startswith("."):
                    continue
                if ent.get("is_dir"):
                    dirs.append(nm)
                elif nm.lower().endswith(".pptx"):
                    files.append(nm)
        else:
            if not os.path.isdir(base):
                base = os.path.dirname(base) or "/"
            try:
                for nm in sorted(os.listdir(base), key=str.lower):
                    if nm.startswith("."):
                        continue
                    full = os.path.join(base, nm)
                    if os.path.isdir(full):
                        dirs.append(nm)
                    elif nm.lower().endswith(".pptx"):
                        files.append(nm)
            except PermissionError:
                pass
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        parent = os.path.dirname(base) or base   # dirname(root) == root, so "up" stops there
        # forward slashes on every platform: the browser's crumb/join logic is "/"-based
        return {"path": base.replace(os.sep, "/"), "parent": parent.replace(os.sep, "/"),
                "dirs": dirs, "files": files, "home": os.path.expanduser("~").replace(os.sep, "/")}

    # --------------------------------------------------- describe (AI manifest)
    if action == "describe":
        # A single self-documenting call so an agent is productive immediately:
        # the schema, the callable action signatures, and a compact element index.
        out = {"schema": SCHEMA_DOC, "actions": ACTIONS}
        if doc and os.path.exists(_model_path(doc)):
            model = _load_model(doc)
            out["size"] = {"width": model["width"], "height": model["height"]}
            out["slides"] = [{
                "id": s["id"], "index": i, "background": s.get("background"),
                "elements": [{
                    "id": e["id"], "type": e["type"],
                    "text": _element_text(e)[:60],
                    "box": {"x": round(e.get("x", 0)), "y": round(e.get("y", 0)),
                            "w": round(e.get("w", 0)), "h": round(e.get("h", 0))},
                } for e in sorted(s.get("elements", []), key=lambda e: e.get("z", 0))],
            } for i, s in enumerate(model.get("slides", []))]
        return out

    # ------------------------------------------------------------- get / save
    if action == "get_model":
        return {"model": _load_model(doc), "mtime": os.path.getmtime(_model_path(doc)),
                "media_dir": _media_dir(doc).replace(os.sep, "/")}

    if action == "save_model":
        model = json.loads(model_json)
        return _save_model(doc, model, expected_mtime)

    # =========================================================== AI-native ops
    #  Each loads the model, mutates via the engine's canonical helpers, saves.
    def _mutate(fn, emt=expected_mtime, ret_el=None):
        model = _load_model(doc)
        fn(model)
        res = _save_model(doc, model, emt)
        # read-after-write: agents chain edits off the returned element state
        if res.get("ok") and ret_el:
            _, e = engine.find_element(res["model"], ret_el)
            res["element"] = e
        return res

    if action == "update_element":
        p = json.loads(patch) if patch else {}
        _validate_patch(p)

        def op(model):
            _, e = engine.find_element(model, el)
            if not e:
                raise ValueError(f"no element with id '{el}' (call action=describe "
                                 "to list valid element ids)")
            _apply_patch(e, p)
        return _mutate(op, ret_el=el)

    if action == "patch_element":
        # generic recursive deep-merge escape hatch: set ANY canonical field,
        # including nested paragraphs/runs, without a per-property action.
        p = json.loads(patch) if patch else {}

        def op(model):
            _, e = engine.find_element(model, el)
            if not e:
                raise ValueError(f"no element with id '{el}' (call action=describe "
                                 "to list valid element ids)")
            _deep_merge(e, p)
        return _mutate(op, ret_el=el)

    if action == "set_text":
        def op(model):
            _, e = engine.find_element(model, el)
            if not e:
                raise ValueError(f"no element {el}")
            _set_plain_text(e, text)
        return _mutate(op, ret_el=el)

    if action == "add_text":
        created = {}

        def op(model):
            s = engine.find_slide(model, slide)
            if not s:
                raise ValueError(f"no slide {slide}")
            e = engine.new_text_element(
                x=x or 120, y=y or 120, w=w or 480, h=h or 90,
                text=text or "Text", size=size or 24, align=align or "left")
            e["z"] = engine._next_z(s)
            s["elements"].append(e)
            created["id"] = e["id"]
        r = _mutate(op)
        r["created"] = created.get("id")
        if r.get("ok"):
            r["element"] = engine.find_element(r["model"], created["id"])[1]
        return r

    if action == "add_image":
        created = {}

        def op(model):
            s = engine.find_slide(model, slide)
            if not s:
                raise ValueError(f"no slide {slide}")
            e = engine.new_image_element(src, x=x or 120, y=y or 120,
                                         w=w or 400, h=h or 300)
            e["z"] = engine._next_z(s)
            s["elements"].append(e)
            created["id"] = e["id"]
        r = _mutate(op)
        r["created"] = created.get("id")
        if r.get("ok"):
            r["element"] = engine.find_element(r["model"], created["id"])[1]
        return r

    if action == "delete_element":
        def op(model):
            s, e = engine.find_element(model, el)
            if e:
                s["elements"].remove(e)
        return _mutate(op)

    if action == "order_element":
        # mode: front | back | forward | backward
        def op(model):
            s, e = engine.find_element(model, el)
            if not e:
                return
            zs = sorted(s["elements"], key=lambda x: x.get("z", 0))
            if mode == "front":
                e["z"] = engine._next_z(s)
            elif mode == "back":
                e["z"] = min([x.get("z", 0) for x in zs], default=0) - 1
            for i, x in enumerate(sorted(s["elements"], key=lambda x: x.get("z", 0))):
                x["z"] = i
        return _mutate(op)

    if action == "align_elements":
        id_list = json.loads(ids) if ids else []

        def op(model):
            s = engine.find_slide(model, slide)
            if not s:
                raise ValueError(f"no slide {slide}")
            targets = [e for e in s["elements"] if e["id"] in id_list]
            if not targets:
                return
            _align(targets, mode, model["width"], model["height"], len(id_list) > 1)
        return _mutate(op)

    if action == "table_op":
        # mode: row_above|row_below|row_del|col_left|col_right|col_del|set_cell
        def op(model):
            _, e = engine.find_element(model, el)
            if not e or e.get("type") != "table":
                raise ValueError(f"element '{el}' is not a table")
            rows = e.setdefault("rows", [[""]])
            nc = max((len(r) for r in rows), default=1)
            for r in rows:
                while len(r) < nc:
                    r.append("")
            ri = int(row) if row is not None and row >= 0 else len(rows) - 1
            ci = int(col) if col is not None and col >= 0 else nc - 1
            blank = [""] * nc
            if mode == "row_above":
                rows.insert(max(0, ri), list(blank))
            elif mode == "row_below":
                rows.insert(ri + 1, list(blank))
            elif mode == "row_del" and len(rows) > 1:
                rows.pop(ri)
            elif mode == "col_left":
                for r in rows:
                    r.insert(ci, "")
            elif mode == "col_right":
                for r in rows:
                    r.insert(ci + 1, "")
            elif mode == "col_del" and nc > 1:
                for r in rows:
                    r.pop(ci)
            elif mode == "set_cell":
                rows[ri][ci] = text
            else:
                raise ValueError(f"unknown table op '{mode}'")
        return _mutate(op, ret_el=el)

    if action == "set_slide_bg":
        def op(model):
            s = engine.find_slide(model, slide)
            if s:
                s["background"] = background or "#ffffff"
        return _mutate(op)

    # ------------------------------------------------------------ slide ops
    if action == "add_slide":
        created = {}

        def op(model):
            s = engine.new_slide()
            at = int(index) if index is not None and int(index) >= 0 else len(model["slides"])
            model["slides"].insert(at, s)
            created["id"] = s["id"]
        r = _mutate(op)
        r["created"] = created.get("id")
        return r

    if action == "delete_slide":
        def op(model):
            model["slides"] = [s for s in model["slides"] if s["id"] != slide]
            if not model["slides"]:
                model["slides"] = [engine.new_slide()]
        return _mutate(op)

    if action == "duplicate_slide":
        created = {}

        def op(model):
            for i, s in enumerate(model["slides"]):
                if s["id"] == slide:
                    import copy
                    dup = copy.deepcopy(s)
                    dup["id"] = engine._nid("s")
                    for e in dup["elements"]:
                        e["id"] = engine._nid("e")
                    model["slides"].insert(i + 1, dup)
                    created["id"] = dup["id"]
                    break
        r = _mutate(op)
        r["created"] = created.get("id")
        return r

    if action == "move_slide":
        def op(model):
            arr = model["slides"]
            idx = next((i for i, s in enumerate(arr) if s["id"] == slide), None)
            if idx is None:
                return
            s = arr.pop(idx)
            to = max(0, min(int(index), len(arr)))
            arr.insert(to, s)
        return _mutate(op)

    # --------------------------------------------------------------- uploads
    if action == "upload_begin":
        os.makedirs(UPLOADS, exist_ok=True)
        tok = "u_" + hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:12]
        open(os.path.join(UPLOADS, tok), "wb").close()
        return {"token": tok}

    if action == "upload_chunk":
        with open(os.path.join(UPLOADS, token), "ab") as f:
            f.write(base64.b64decode(data))
        return {"ok": True}

    if action == "upload_end":
        raw = os.path.join(UPLOADS, token)
        ext = (os.path.splitext(name)[1] or ".png").lower()
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(name)) or "image"
        fname = f"{engine._nid('e')}_{safe}"
        if not fname.lower().endswith(ext):
            fname += ext
        media_dir = _media_dir(doc)
        os.makedirs(media_dir, exist_ok=True)
        dst = os.path.join(media_dir, fname)
        shutil.move(raw, dst)
        # report intrinsic size so the UI can place it at a sane aspect ratio
        dims = None
        try:
            from PIL import Image
            with Image.open(dst) as im:
                dims = list(im.size)
        except Exception:
            pass
        return {"src": f"media/{fname}", "dims": dims}

    # ------------------------------------------------- download / export as fmt
    if action == "export":
        model = _load_model(doc)
        base = _get_title(file) or (os.path.splitext(os.path.basename(file))[0] if file else "deck")
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", base)
        os.makedirs(EXPORTS, exist_ok=True)
        media_dir = _media_dir(doc)
        f_ = (fmt or "pptx").lower()
        if f_ == "pdf":
            out = os.path.join(EXPORTS, f"{safe}.pdf")
            engine.build_pdf(model, out, media_dir)
        elif f_ == "html":
            out = os.path.join(EXPORTS, f"{safe}.html")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(engine.build_html(model, media_dir))
        elif f_ in ("md", "markdown"):
            out = os.path.join(EXPORTS, f"{safe}.md")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(engine.build_md(model))
        elif f_ == "json":
            out = os.path.join(EXPORTS, f"{safe}.json")
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(model, fh, ensure_ascii=False, indent=1)
        else:
            out = os.path.join(EXPORTS, f"{safe}.pptx")
            engine.build_pptx(model, out, media_dir)
        return {"path": out, "name": os.path.basename(out)}

    # --------- Save = materialize model -> `file` itself (atomic overwrite)
    if action == "save":
        file = _to_native_path(file)
        # RO-3 fs gate FIRST (before _load_model and the mkstemp below): the
        # atomic tempfile-in-parent-dir + os.replace would otherwise silently
        # overwrite a chmod -w file via the directory's write bit.
        if os.path.exists(file) and not os.access(file, os.W_OK):
            raise PermissionError(f"{file!r} is read-only")
        model = _load_model(doc)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(file) or ".", suffix=".pptx.tmp")
        os.close(tmp_fd)
        engine.build_pptx(model, tmp_path, _media_dir(doc))
        os.replace(tmp_path, file)   # atomic
        new_doc = _content_hash(file)
        new_dir = _cache_dir(new_doc)
        if not os.path.exists(_model_path(new_doc)):
            os.makedirs(new_dir, exist_ok=True)
            shutil.copytree(_media_dir(doc), os.path.join(new_dir, "media"), dirs_exist_ok=True)
            with open(_model_path(new_doc), "w", encoding="utf-8") as f2:
                json.dump(model, f2, ensure_ascii=False)
        if new_doc != doc:
            # every save re-hashes to a new cache dir; the old one is now
            # unreachable (the client switches to new_doc on this response).
            shutil.rmtree(_cache_dir(doc), ignore_errors=True)
        return {"path": file, "doc": new_doc, "mtime": os.path.getmtime(_model_path(new_doc))}

    # --------- Save as = write a NEW .pptx elsewhere; the open document is unchanged
    if action == "save_as":
        model = _load_model(doc)
        default = _get_title(file) or (os.path.splitext(os.path.basename(file))[0] if file else "deck")
        base = re.sub(r"[^a-zA-Z0-9._ -]", "_", (name or f"{default} copy")).strip()
        if not base.lower().endswith(".pptx"):
            base += ".pptx"
        dstdir = (os.path.abspath(os.path.expanduser(_to_native_path(directory))) if directory
                  else (os.path.dirname(_to_native_path(file)) or os.path.expanduser("~")))
        os.makedirs(dstdir, exist_ok=True)
        dst = os.path.join(dstdir, base)
        engine.build_pptx(model, dst, _media_dir(doc))
        return {"path": dst, "name": base}

    # ---------------------------------------------------------------- sidecar
    if action == "set_title":
        file = _to_native_path(file)
        # RO-3 gate on the actual write target (RO-6): the title lives in the
        # <file>.json sidecar, so it's the sidecar's writability that counts.
        if not _sidecar_writable(file):
            raise PermissionError(f"{_sidecar_path(file)!r} is read-only")
        _set_title(file, title)
        return {"ok": True, "title": title or None}

    raise ValueError(f"unknown action: {action}")


# --------------------------------------------------------------------------- #
#  AI manifest (returned by action=describe)                                  #
# --------------------------------------------------------------------------- #
SCHEMA_DOC = {
    "coordinates": "slide-pixel space (EMU/9525 == 96dpi); 16:9 deck = 1280x720",
    "deck": {"schema": 1, "name": "str", "width": "px", "height": "px",
             "slides": "[slide]"},
    "slide": {"id": "s_* (stable)", "background": "#rrggbb", "elements": "[element]"},
    "element": {
        "id": "e_* (stable across save/reopen)", "type": "text|image|table",
        "name": "str", "x": "px", "y": "px", "w": "px", "h": "px",
        "rot": "deg", "z": "int stacking order",
        "text-only": {"fill": "#rrggbb|null", "valign": "top|middle|bottom",
                      "paragraphs": [{"align": "left|center|right|justify",
                                      "level": "int", "runs": [{"text": "str",
                                      "bold": "bool", "italic": "bool",
                                      "underline": "bool", "size": "pt",
                                      "color": "#rrggbb", "font": "str"}]}]},
        "image-only": {"src": "media/<file> (relative to the doc's cache dir)"},
        "table-only": {"rows": "[[cell str]]"},
    },
}
ACTIONS = {
    "open": "file -> {doc, model, mtime, media_dir, title}: parse (or reuse the "
           "cached) model for a .pptx",
    "describe": "doc? -> schema + action list + compact per-slide element index",
    "get_model": "doc -> {model, mtime, media_dir}",
    "update_element": "doc, el, patch(json) -> semantic patch: geometry keys, "
                      "run-style keys (bold/italic/underline/size/color/font) "
                      "applied to all runs, align/valign, fill, text. Returns element.",
    "patch_element": "doc, el, patch(json) -> recursive deep-merge of ANY "
                     "canonical field (incl. nested paragraphs/runs). Returns element.",
    "set_text": "doc, el, text -> replace text (newlines split paragraphs). Returns element.",
    "add_text": "doc, slide, text?, x?,y?,w?,h?,size?,align? -> new text box. Returns created id.",
    "add_image": "doc, slide, src, x?,y?,w?,h? -> place an uploaded image. Returns created id.",
    "delete_element": "doc, el -> remove element",
    "order_element": "doc, el, mode(front|back) -> restack",
    "align_elements": "doc, slide, ids(json list), mode(left|center|right|top|"
                      "middle|bottom|dist-h|dist-v) -> align/distribute",
    "table_op": "doc, el, mode(row_above|row_below|row_del|col_left|col_right|"
                "col_del|set_cell), row?, col?, text?(set_cell) -> edit a table",
    "set_slide_bg": "doc, slide, background(#rrggbb)",
    "add_slide": "doc, index? -> new blank slide (returns id)",
    "delete_slide": "doc, slide", "duplicate_slide": "doc, slide (returns id)",
    "move_slide": "doc, slide, index",
    "export": "doc, file, fmt(pptx|pdf|html|md|json) -> writes a temp file, returns {path,name}",
    "save": "doc, file -> materialize model to `file` itself (atomic overwrite). Returns new doc id.",
    "save_as": "doc, file, name, directory? -> write a NEW .pptx (default: file's dir), doesn't repoint `file`",
    "listdir": "path? -> {path,parent,dirs,files(.pptx),home} for the Save-as browser",
    "set_title": "file, title -> rename the deck's display title (stored in the file's .json sidecar)",
}


def _element_text(e):
    if e.get("type") == "text":
        return " ".join(r.get("text", "") for p in e.get("paragraphs", [])
                        for r in p.get("runs", [])).strip()
    if e.get("type") == "table":
        return " ".join(c for row in e.get("rows", []) for c in row)[:60]
    return e.get("type", "")


def _deep_merge(dst, src):
    """Recursively merge src into dst (in place). Lists replace wholesale."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_patch(p):
    """Actionable validation so agents recover from mistakes instead of flailing."""
    if not isinstance(p, dict):
        raise ValueError("patch must be a JSON object")
    for key in ("x", "y", "w", "h", "rot", "size"):
        if key in p and p[key] is not None and not isinstance(p[key], (int, float)):
            raise ValueError(f"'{key}' expects a number, got {type(p[key]).__name__}")
    for key in ("color", "fill"):
        if p.get(key) and not _HEX.match(str(p[key])):
            raise ValueError(f"'{key}' expects a #rrggbb hex color, got {p[key]!r}")
    if "align" in p and p["align"] not in ("left", "center", "right", "justify"):
        raise ValueError("'align' expects one of left|center|right|justify")
    if "valign" in p and p["valign"] not in ("top", "middle", "bottom"):
        raise ValueError("'valign' expects one of top|middle|bottom")


# --------------------------------------------------------------------------- #
#  patch / text / align helpers (pure, canonical)                             #
# --------------------------------------------------------------------------- #
_GEOM_KEYS = ("x", "y", "w", "h", "rot", "z")
_RUN_STYLE = ("bold", "italic", "underline", "size", "color", "font")


def _apply_patch(el, p):
    """Apply an AI/UI patch to a single element.

    Geometry keys set directly. Run-style keys (bold/size/color/...) apply to
    EVERY run of every paragraph. `align`/`valign` set paragraph/box alignment.
    `text` replaces the plain text (first-run formatting preserved). `fill` sets
    the box fill. Unknown keys are ignored so partial patches are safe.
    """
    for k in _GEOM_KEYS:
        if k in p and p[k] is not None:
            el[k] = p[k]
    if "fill" in p:
        el["fill"] = p["fill"]
    if "valign" in p:
        el["valign"] = p["valign"]
    if el.get("type") == "text":
        if "text" in p and p["text"] is not None:
            _set_plain_text(el, p["text"])
        style = {k: p[k] for k in _RUN_STYLE if k in p}
        if style or "align" in p:
            for para in el.get("paragraphs", []):
                if "align" in p:
                    para["align"] = p["align"]
                for r in para.get("runs", []):
                    for k, v in style.items():
                        r[k] = v


def _set_plain_text(el, text):
    """Replace a text element's content with `text`, keeping the first run's
    formatting as the template and splitting on newlines into paragraphs."""
    paras = el.get("paragraphs") or []
    template = dict(paras[0]["runs"][0]) if paras and paras[0].get("runs") else \
        {"bold": False, "italic": False, "underline": False, "size": 18,
         "color": "#202124", "font": None}
    align = paras[0].get("align") if paras else "left"
    new = []
    for line in str(text).split("\n"):
        run = dict(template)
        run["text"] = line
        new.append({"align": align, "level": 0, "runs": [run]})
    el["paragraphs"] = new or [{"align": align, "level": 0,
                                "runs": [dict(template, text="")]}]


def _align(targets, mode, W, H, multi):
    """Align a set of elements. With one target, align to the slide; with many,
    align to the group's shared edge (Google-Slides semantics)."""
    if mode in ("left", "center", "right"):
        if multi:
            lo = min(e["x"] for e in targets)
            hi = max(e["x"] + e["w"] for e in targets)
            for e in targets:
                if mode == "left":
                    e["x"] = lo
                elif mode == "right":
                    e["x"] = hi - e["w"]
                else:
                    e["x"] = (lo + hi) / 2 - e["w"] / 2
        else:
            for e in targets:
                if mode == "left":
                    e["x"] = 0
                elif mode == "right":
                    e["x"] = W - e["w"]
                else:
                    e["x"] = (W - e["w"]) / 2
    elif mode in ("top", "middle", "bottom"):
        if multi:
            lo = min(e["y"] for e in targets)
            hi = max(e["y"] + e["h"] for e in targets)
            for e in targets:
                if mode == "top":
                    e["y"] = lo
                elif mode == "bottom":
                    e["y"] = hi - e["h"]
                else:
                    e["y"] = (lo + hi) / 2 - e["h"] / 2
        else:
            for e in targets:
                if mode == "top":
                    e["y"] = 0
                elif mode == "bottom":
                    e["y"] = H - e["h"]
                else:
                    e["y"] = (H - e["h"]) / 2
    elif mode in ("dist-h", "dist-v") and len(targets) > 2:
        key = "x" if mode == "dist-h" else "y"
        dim = "w" if mode == "dist-h" else "h"
        ts = sorted(targets, key=lambda e: e[key])
        lo = ts[0][key]
        hi = ts[-1][key] + ts[-1][dim]
        span = hi - lo
        total = sum(e[dim] for e in ts)
        gap = (span - total) / (len(ts) - 1)
        cur = lo
        for e in ts:
            e[key] = cur
            cur += e[dim] + gap
