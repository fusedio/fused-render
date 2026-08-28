"""One copy of "make these bytes into a picture something here can show".

TWO CALLERS, ONE LADDER. The claude template's `agent.py` has held this code
since D614 (its `image_to_png` action, for a chat attachment the BROWSER cannot
decode); the New task modal's upload endpoint needs the identical answer for the
identical reason, and a second implementation would have been a second place for
the frame cap, the alpha rule and the JPEG ladder to drift. So the ladder lives
here — in `server/` rather than in the template, because the template is a file
we copy into a user's folder and this is server code — and the endpoint imports
it.

`agent.py` KEEPS ITS OWN COPY, and that is not a TODO. SPEC PY-15 / D166 forbid
a template importing `fused_render`: the claude agent runs as a subprocess (it is
deliberately absent from `executor.INPROCESS_HELPERS`), the `sys.path` bootstrap
that once made such imports work is retired (PY-6a), and the fused local backend
strips `PYTHONPATH` from its children — so the import would resolve in a dev
checkout and fail, or silently take a fallback, everywhere else. That is exactly
the class of bug the rule exists to end, and `tests/test_templates_decoupled.py`
pins it shut. So this ladder is WRITTEN TWICE AND TESTED ONCE, the same shape
`_pane_file`, `_ann_notes` and `_app_dir_for` already have in that same template:
`tests/test_image_convert_parity.py` feeds one picture through both halves and
pins them to identical output. **Change one, change the other.**

Its containment check (`_in_shots`) is the half that must NOT be mirrored here:
authorising the path is the caller's job, always. Nothing in this module decides
what it is allowed to read.

WHAT IT DECIDES, in the endpoint's order:
  * `ext_for(mime, filename)` — the extension a server-minted name gets;
  * `is_image` / `browser_blind` — whether these bytes are a picture at all, and
    whether the picture is one no browser engine here can draw (TIFF off a
    scanner, HEIC off an iPhone — the two formats users actually drop);
  * `transcode` — the PNG (or JPEG) copy, capped at `PNG_EDGE`.

It NEVER raises and it deletes nothing: every failure is `{"error": ...}`, and
the caller's whole answer to one is to keep the file the user actually attached.
An attachment lost to a converter's bad day would be a worse bug than the one
this module exists to fix.
"""
import io
import os
import re
import subprocess
import sys
import tempfile

#: The longest edge a transcoded picture is given, and the byte budget it has to
#: land under. Both numbers are the CHAT's (`SHOT_VIEW_EDGE` / D615's downscale
#: trigger), deliberately: a conversion that came back bigger or sharper than
#: the app's own screenshots would be a second, quieter rule for the same thing.
PNG_EDGE = 1600
PNG_MAX_BYTES = 4 * 1024 * 1024
#: What a PNG that missed the budget is re-tried as. Quality before resolution,
#: same order as the page's own encoder ladder: 1600px of a photo at q60 answers
#: every question the agent has of it, where 800px of it may not.
JPEG_QUALITY = (90, 80, 70, 60)
#: `sips` is a one-shot converter on a file the user just handed us.
SIPS_TIMEOUT = 20

#: MIME → extension, for a paste whose `File` has no filename at all (a
#: clipboard screenshot is `image/png` and nothing else). Extended past the four
#: the data-URL endpoint used to take, because ANY file type is accepted now and
#: the MIME is the better answer whenever the browser supplies one.
MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/tiff": ".tif", "image/heic": ".heic",
    "image/heif": ".heif", "image/bmp": ".bmp", "image/avif": ".avif",
    "image/svg+xml": ".svg", "application/pdf": ".pdf", "text/csv": ".csv",
    "text/plain": ".txt", "text/markdown": ".md", "text/html": ".html",
    "application/json": ".json", "application/zip": ".zip",
}

#: What counts as a picture — the switch for a thumbnail, and for whether the
#: transcode is even asked. By EXTENSION and MIME both, since a drop off a NAS
#: often carries no type at all.
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico",
    ".tif", ".tiff", ".heic", ".heif", ".jp2", ".j2k", ".jpf", ".tga",
    ".psd", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2",
    ".pbm", ".pgm", ".ppm", ".pnm",
})

#: Pictures NO browser engine here draws: `URL.createObjectURL` on one renders
#: an empty box — no error, no broken-image glyph, nothing (D613) — and `Read`
#: cannot open them either, so an attachment in one of these formats is a path
#: to bytes nobody in the conversation can look at. These are the ones that get
#: a PNG sibling. `.svg`, `.png`, `.jpg`, `.gif`, `.webp` and `.avif` are
#: deliberately absent: every engine we run in draws all six.
BLIND_EXTS = frozenset({
    ".tif", ".tiff", ".heic", ".heif", ".jp2", ".j2k", ".jpf", ".tga",
    ".psd", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2",
    ".pbm", ".pgm", ".ppm", ".pnm", ".ico",
})

_EXT_OK = re.compile(r"[^a-z0-9]")


def ext_for(mime: str | None, filename: str | None) -> str:
    """The extension a server-minted name should carry: the MIME map first, the
    client's filename second, "" if neither says anything usable.

    The MIME leads because it is the half the browser computes and the filename
    is the half a user can type — and the RESULT is sanitised either way
    (lowercased, alphanumerics only, length-capped): this string is appended to
    a path we are about to `os.open`, so `..`, a separator or a NUL in it would
    be the whole bug."""
    m = (mime or "").split(";", 1)[0].strip().lower()
    if m in MIME_EXT:
        return MIME_EXT[m]
    raw = os.path.splitext(os.path.basename(filename or ""))[1]
    clean = _EXT_OK.sub("", raw.lower().lstrip("."))[:12]
    return ("." + clean) if clean else ""


def is_image(ext: str, mime: str | None = None) -> bool:
    """Whether these bytes are a picture — the switch for a thumbnail."""
    if (ext or "").lower() in IMAGE_EXTS:
        return True
    return (mime or "").split("/", 1)[0].strip().lower() == "image"


def browser_blind(ext: str, mime: str | None = None) -> bool:
    """Whether it is a picture the browser will show as an EMPTY BOX."""
    if (ext or "").lower() in BLIND_EXTS:
        return True
    m = (mime or "").split(";", 1)[0].strip().lower()
    return m in ("image/tiff", "image/heic", "image/heif", "image/x-tga",
                 "image/vnd.adobe.photoshop", "image/jp2")


def sips_to_png(path: str, out_dir: str | None = None) -> str | None:
    """A macOS-only second opinion on bytes Pillow refused: `sips` through
    ImageIO, which decodes what the OS itself can — HEIC/HEIF above all.

    HEIC is the DEFAULT camera format on every iPhone and the one format both
    halves of this feature are blind to (no browser engine decodes it, and
    Pillow needs `pillow-heif`, a compiled wheel we do not ship), so shelling
    out to a binary present on every macOS install is the common path rather
    than the exotic one. None for anything at all going wrong — the caller
    reports the ORIGINAL Pillow failure, which is the honest one."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sips"):
        return None
    try:
        fd, tmp = tempfile.mkstemp(prefix="conv-", suffix=".png",
                                   dir=out_dir or os.path.dirname(path))
        os.close(fd)
    except OSError:
        return None
    try:
        proc = subprocess.run(
            ["/usr/bin/sips", "-s", "format", "png", path, "--out", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=SIPS_TIMEOUT, check=False)
        if proc.returncode == 0 and os.path.getsize(tmp) > 0:
            return tmp
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return None


def transcode(path: str, dest_base: str) -> dict:
    """Write a PNG (or JPEG) copy of the picture at `path` to `dest_base` + the
    extension the format ends up being, and report it.

    `dest_base` is a path WITHOUT an extension and is the caller's to choose —
    the copy is a sibling of the original in every current caller, but the name
    must differ from it, since a 6 MB PNG downscaled to 1600px is written next
    to a `.png` original. The original is never touched: it is what the user
    actually attached, and one deletion policy per directory (the pruner's) is
    the only way a picture does not get lost early.

    PNG FIRST, JPEG ONLY IF PNG MISSES THE BUDGET. A screenshot, a scan and a
    diagram are the common TIFFs and all three are what PNG is good at (and what
    JPEG's ringing ruins); a 12 MP photo is the common HEIC and is where a
    lossless 1600px re-encode blows past 4 MB. So the format is MEASURED rather
    than guessed from the extension.

    Returns `{path, width, height, bytes, source_w, source_h}` or `{error}`.
    Never raises."""
    try:
        if not path or not os.path.isfile(path):
            return {"error": "no such file"}
        try:
            from PIL import Image
        except ImportError:
            return {"error": "pillow is not installed"}

        tmp = None
        try:
            try:
                img = Image.open(path)
                img.load()
            except Exception as first:
                # HEIC without pillow-heif lands here.
                tmp = sips_to_png(path, os.path.dirname(dest_base) or None)
                if tmp is None:
                    return {"error": "could not decode: %s" % first}
                img = Image.open(tmp)
                img.load()
            # A multi-frame TIFF (a fax, a scanned stack) or an animated GIF has
            # one frame the user means by "the picture", and it is the first.
            try:
                if getattr(img, "n_frames", 1) > 1:
                    img.seek(0)
            except Exception:
                pass
            source_w, source_h = img.size
            if not source_w or not source_h:
                return {"error": "the picture has no pixels"}
            # Alpha is kept where it exists (a diagram with a transparent
            # background reads wrong flattened onto black) and dropped where it
            # does not — CMYK, 16-bit grey and palette all have to leave their
            # own mode either way.
            if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            if max(source_w, source_h) > PNG_EDGE:
                img.thumbnail((PNG_EDGE, PNG_EDGE), Image.LANCZOS)
            width, height = img.size

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            data, ext = buf.getvalue(), ".png"
            if len(data) > PNG_MAX_BYTES:
                flat = img
                if flat.mode != "RGB":
                    # JPEG has no alpha. White rather than black because these
                    # are documents and screenshots far more often than they are
                    # neon on dark.
                    bg = Image.new("RGB", flat.size, (255, 255, 255))
                    bg.paste(flat, mask=flat.split()[-1])
                    flat = bg
                for q in JPEG_QUALITY:
                    jbuf = io.BytesIO()
                    flat.save(jbuf, format="JPEG", quality=q, optimize=True,
                              progressive=True)
                    data, ext = jbuf.getvalue(), ".jpg"
                    if len(data) <= PNG_MAX_BYTES:
                        break
                # Past the ladder it goes out anyway: an oversize picture the
                # agent CAN read beats a perfectly sized one it cannot.
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        out = dest_base + ext
        # 0600 on the create itself: the directory is already 0700, and a
        # converted picture of someone's screen or camera roll should never be
        # briefly wider than the original was.
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            try:
                os.unlink(out)
            except OSError:
                pass
            raise
        return {"path": out.replace("\\", "/"), "width": width,
                "height": height, "bytes": len(data),
                "source_w": source_w, "source_h": source_h}
    except Exception as e:  # never raises: see the module docstring
        return {"error": "%s: %s" % (type(e).__name__, e)}


def dimensions(path: str) -> tuple[int, int] | None:
    """`(width, height)` of a picture the browser CAN draw, or None.

    Header-only (Pillow's lazy open never decodes the pixels), because the one
    caller wants it for a chip's aspect and would not pay a full decode for
    that."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
        return (int(w), int(h)) if w and h else None
    except Exception:
        return None
