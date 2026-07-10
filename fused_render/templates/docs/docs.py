# /// script
# dependencies = ["pypandoc-binary", "typst"]
# ///
"""Backend for the docs preview template (fused-render).

The document itself is one user file — a `.doc.json` sidecar-free JSON file
holding the ProseMirror doc plus named paragraph styles — read and written
directly by the page via fused.readFile/writeFile. Comments, version history
and signatures live in the file's JSON sidecar (<file>.json), also read and
written by the page (read-modify-write under the "docs" key). This script
only holds what genuinely needs Python: converting the current document to
other formats via pandoc/typst, and browsing the filesystem for "Save a
copy…". Params arrive as strings; annotate.
"""
import hashlib
import os
import re
import subprocess

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    import sys
    __file__ = os.path.join(sys.path[0], "docs.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "docs")

# pandoc target format per requested extension (typst/pdf handled specially).
PANDOC_TO = {
    "docx": "docx",
    "md": "gfm",
    "markdown": "gfm",
    "html": "html",
    "latex": "latex",
    "tex": "latex",
    "epub": "epub3",
    "odt": "odt",
    "rtf": "rtf",
}


def _pandoc(args, input_text=None):
    """Run the bundled pandoc. Returns stdout bytes; raises on failure."""
    import pypandoc
    exe = pypandoc.get_pandoc_path()
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if input_text is not None:
        kw["input"] = input_text.encode("utf-8")
    p = subprocess.run([exe, *args], **kw)
    if p.returncode != 0:
        raise RuntimeError("pandoc failed: " + p.stderr.decode("utf-8", "replace")[:800])
    return p.stdout


def _cache_dir(file: str) -> str:
    # One subfolder per document (keyed by its own path) so exports from
    # different documents never collide; lives outside the template folder.
    digest = hashlib.sha256(os.path.abspath(file).encode("utf-8")).hexdigest()[:16]
    d = os.path.join(CACHE_ROOT, digest)
    os.makedirs(d, exist_ok=True)
    return d


# -------------------------------------------------------------------- dispatcher
def main(action: str = "export", file: str = "", html: str = "", title: str = "",
         fmt: str = "pdf", path: str = "", directory: str = ""):
    if action == "warmup":
        import pypandoc
        return {"pandoc": pypandoc.get_pandoc_version()}

    # ---- directory listing for the "Save a copy…" browser
    if action == "listdir":
        base = os.path.abspath(os.path.expanduser(path)) if path else os.path.expanduser("~")
        if not os.path.isdir(base):
            base = os.path.dirname(base) or os.path.expanduser("~")
        dirs, files = [], []
        try:
            for nm in sorted(os.listdir(base), key=str.lower):
                if nm.startswith("."):
                    continue
                if os.path.isdir(os.path.join(base, nm)):
                    dirs.append(nm)
                elif nm.lower().endswith(".docx"):
                    files.append(nm)
        except PermissionError:
            pass
        parent = os.path.dirname(base) or base   # dirname(root) == root, so "up" stops there
        # forward slashes on every platform: the browser's crumb/join logic is "/"-based
        return {"path": base.replace(os.sep, "/"), "parent": parent.replace(os.sep, "/"),
                "dirs": dirs, "files": files,
                "home": os.path.expanduser("~").replace(os.sep, "/")}

    # ---- open an existing .docx: convert to HTML for the editor
    if action == "import":
        if not file or not os.path.isfile(file):
            return {"error": f"file not found: {file}"}
        try:
            out = _pandoc(["-f", "docx", "-t", "html+tex_math_dollars",
                           "--wrap=none", file])
        except Exception as e:
            return {"error": f"could not read {os.path.basename(file)}: {e}"}
        return {"html": out.decode("utf-8", "replace")}

    # ---- export/convert: browser sends serialized HTML, we fan out to formats
    if action == "export":
        os.makedirs(CACHE_ROOT, exist_ok=True)
        if not html:
            return {"error": "no html to export"}
        out_dir = _cache_dir(file)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", (title or "document")).strip("_") or "document"
        ext = fmt.lower()
        try:
            if ext == "pdf":
                import typst
                typ = _pandoc(["-f", "html+tex_math_dollars", "-t", "typst",
                               "--wrap=none"], input_text=html)
                typ_path = os.path.join(out_dir, stem + ".typ")
                with open(typ_path, "wb") as f:
                    f.write(typ)
                out_path = os.path.join(out_dir, stem + ".pdf")
                typst.compile(typ_path, output=out_path)
            elif ext in PANDOC_TO:
                out_ext = {"latex": "tex", "markdown": "md"}.get(ext, ext)
                out_path = os.path.join(out_dir, f"{stem}.{out_ext}")
                data = _pandoc(["-f", "html+tex_math_dollars", "-t", PANDOC_TO[ext],
                                "--wrap=none", "--standalone", "-o", out_path],
                               input_text=html)
                if not os.path.exists(out_path):  # some writers go to stdout
                    with open(out_path, "wb") as f:
                        f.write(data)
            else:
                return {"error": f"unsupported format: {fmt}"}
        except Exception as e:
            return {"error": f"export to {fmt} failed: {e}"}
        return {"path": out_path, "name": os.path.basename(out_path), "size": os.path.getsize(out_path)}

    # ---- "Save a copy…": write a .docx to a location the user browsed to
    if action == "save_as":
        if not html:
            return {"error": "nothing to save"}
        # os.path.join resolves it: a full path in `path` wins, a bare name joins
        # onto `directory` — handles absolute/relative and either separator.
        raw = os.path.join(directory, path) if directory else path
        dest = os.path.abspath(os.path.expanduser(raw))
        if not dest.lower().endswith(".docx"):
            dest += ".docx"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            _pandoc(["-f", "html+tex_math_dollars", "-t", "docx", "--wrap=none",
                     "--standalone", "-o", dest], input_text=html)
        except Exception as e:
            return {"error": f"save to {dest} failed: {e}"}
        return {"path": dest.replace(os.sep, "/"), "name": os.path.basename(dest)}

    return {"error": f"unknown action: {action}"}
