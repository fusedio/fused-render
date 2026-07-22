"""Reader backing canvas/template.html — parses a Fused `canvas.toml` (v2) into
a JSON-native layout the viewer draws (SPEC §28).

One `tomllib` pass turns the canvas definition into
`{name, nodes, folders, edges, viewport, viewportBounds, siblings}`:

  * nodes   — UDF nodes (positioned rects), optional fields defaulted per the
              §28 table (title = udfName, visible = true).
  * folders — `type = "udf-folder"` group boxes (folderName, folderColor,
              childUdfOrder, isLocked).
  * edges   — [[srcUdfName, dstUdfName], …] pairs, malformed ones dropped.
  * viewport / viewportBounds — the stored camera, when present.
  * siblings — udfName → the sibling file extensions (`.py`/`.json`/`.md`/
              `.html`) present next to the toml, from one listing of its dir
              (kernel os.listdir locally; /api/fs/list when `src` marks the dir
              mount-backed, so a remote listing never wedges the NFS mount).

Malformed node/edge entries are skipped, never fatal — a hand-edited canvas
with one broken node still previews the rest. A whole-file parse failure is
allowed to propagate so the page's traceback overlay shows it (§28).

Called by `fused.runPython("./reader.py", {file})`.
"""


# ENGINE ISOLATION (SPEC PY / §28): under the fused engine the UDF body runs
# re-exec'd in isolation from this module's globals, so EVERY helper and
# constant lives INSIDE main() and every import is done inside main(). Nothing
# is referenced at module level except the entrypoint and its registration shim.
def main(file: str = "", src: str = "") -> dict:
    import os

    import tomllib

    SIBLING_EXTS = (".py", ".json", ".md", ".html")

    # ---- mount-safe directory listing (mirrors pyramid/overview_pyramid.py) --
    # Files under a read-only rclone NFS mount stall or DROP the mount on a
    # kernel directory listing (os.listdir/os.scandir enumerates the entire
    # parent S3 prefix). This template stays mount-AGNOSTIC: it never imports
    # shell.mounts and never matches mount paths. Instead the browser passes a
    # `src` = server-origin + /api/fs/raw?path=; we use ONLY its scheme+host and
    # ask the server whether a path is `remote`. When it is, we list via
    # /api/fs/list (the server routes that through rclone's rc, never the
    # kernel) rather than os.listdir. `_server_url` and `_stat` are copied
    # verbatim from that template's _SHARED block.
    import json as _json
    import urllib.error as _urlerr
    import urllib.parse as _urlparse
    import urllib.request as _urlreq

    def _server_url(src, endpoint, path):
        """Server URL built from `src`'s ORIGIN and our own normalized `path`.
        src is trusted only for scheme+netloc; we quote OUR path onto the
        endpoint, ignoring src's ?path."""
        u = _urlparse.urlsplit(src)
        return f"{u.scheme}://{u.netloc}{endpoint}?path=" + _urlparse.quote(path)

    def _stat(src, path):
        """Ask /api/fs/stat about `path`. Returns:
        ("ok", payload)      — payload has bool `remote`
        ("missing", None)    — server says the path does not exist (404)
        ("unreachable", None)— server unreachable/errored; caller falls back
                               to a local kernel probe (presumed local)."""
        url = _server_url(src, "/api/fs/stat", path)
        try:
            with _urlreq.urlopen(url, timeout=10) as r:
                return ("ok", _json.load(r))
        except _urlerr.HTTPError as e:
            if e.code == 404:
                return ("missing", None)
            return ("unreachable", None)
        except Exception:  # noqa: BLE001 — any network error -> local fallback
            return ("unreachable", None)

    def _remote_listdir(src, path):
        """First page of /api/fs/list for a remote dir; returns the set of entry
        names. NEVER kernel-lists. The page is capped server-side; if it is
        `truncated`, a matching sibling may hide beyond the cap for a huge
        remote dir — we accept a missing badge over wedging the mount, so the
        `truncated`/`cursor` fields are intentionally not followed here."""
        url = _server_url(src, "/api/fs/list", path)
        try:
            with _urlreq.urlopen(url, timeout=10) as r:
                payload = _json.load(r)
        except Exception:  # noqa: BLE001 — treat any error as an empty listing
            return set()
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return {e.get("name") for e in (entries or []) if isinstance(e, dict) and e.get("name")}

    def is_num(v):
        # bool is an int subclass in Python — reject it as a coordinate.
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def num(v, default=0):
        return v if is_num(v) else default

    def text(v, default=""):
        return v if isinstance(v, str) else default

    def flag(v, default):
        return v if isinstance(v, bool) else default

    def node_common(entry):
        # Fields shared by UDF nodes and folder nodes; position/size defaulted
        # so a node missing them still lands somewhere paintable (§28).
        name = text(entry.get("udfName"))
        return {
            "udfName": name,
            "x": num(entry.get("x")),
            "y": num(entry.get("y")),
            "zIndex": int(num(entry.get("zIndex"))),
            "width": num(entry.get("width"), 240),
            "height": num(entry.get("height"), 140),
        }

    if not file:
        # Raise (don't return an error dict the viewer would render as an
        # empty canvas) — a reader failure surfaces via the page's traceback
        # overlay (§28), same as a whole-file parse failure.
        raise ValueError("no file (missing _file param)")

    # A whole-file parse failure propagates -> the page's traceback overlay.
    with open(file, "rb") as f:
        doc = tomllib.load(f)
    if not isinstance(doc, dict):
        raise ValueError("canvas.toml top level is not a table")

    canvas = doc.get("canvas")
    if not isinstance(canvas, dict):
        canvas = {}

    # ---- nodes / folders --------------------------------------------------
    nodes = []
    folders = []
    raw_nodes = canvas.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, dict):
                continue  # skip a malformed node entry, never crash
            base = node_common(entry)
            if not base["udfName"] and entry.get("type") != "udf-folder":
                # A UDF node with no name can't badge siblings or wire edges.
                continue
            if entry.get("type") == "udf-folder":
                child_order = entry.get("childUdfOrder")
                base.update(
                    {
                        "folderName": text(entry.get("folderName"), base["udfName"] or "folder"),
                        "folderColor": text(entry.get("folderColor")),
                        "childUdfOrder": [text(c) for c in child_order if isinstance(c, str)]
                        if isinstance(child_order, list)
                        else [],
                        "isLocked": flag(entry.get("isLocked"), False),
                    }
                )
                folders.append(base)
            else:
                base.update(
                    {
                        "title": text(entry.get("title"), base["udfName"]),
                        "description": text(entry.get("description")),
                        "visible": flag(entry.get("visible"), True),
                        "type": text(entry.get("type")),
                        "textBoxColor": text(entry.get("textBoxColor")),
                    }
                )
                nodes.append(base)

    # ---- edges ------------------------------------------------------------
    edges = []
    raw_edges = canvas.get("edges")
    if isinstance(raw_edges, list):
        for pair in raw_edges:
            if (
                isinstance(pair, list)
                and len(pair) == 2
                and isinstance(pair[0], str)
                and isinstance(pair[1], str)
            ):
                edges.append([pair[0], pair[1]])

    # ---- viewport ---------------------------------------------------------
    # A viewport only counts when x and y are actually present — an empty
    # [canvas.viewport] table must fall through to fit-to-bounds, not pin the
    # camera at a fabricated origin.
    viewport = None
    raw_vp = canvas.get("viewport")
    if isinstance(raw_vp, dict) and is_num(raw_vp.get("x")) and is_num(raw_vp.get("y")):
        viewport = {
            "x": num(raw_vp.get("x")),
            "y": num(raw_vp.get("y")),
            "zoom": num(raw_vp.get("zoom"), 1),
        }
    raw_vb = canvas.get("viewportBounds")
    viewport_bounds = raw_vb if isinstance(raw_vb, dict) else None

    # ---- siblings ---------------------------------------------------------
    # For each UDF node report which known sibling extensions exist as
    # `{udfName}<ext>` next to the toml. This needs one listing of the toml's
    # dir. os.path.abspath/dirname are pure string ops (no kernel I/O) so they
    # are safe on a mount path; os.listdir is NOT — a kernel listing of a
    # remote rclone NFS dir enumerates the whole S3 prefix and can drop the
    # mount. So when `src` says the dir is remote, list via /api/fs/list; only
    # a local (or presumed-local) dir is listed through the kernel.
    parent_dir = os.path.dirname(os.path.abspath(file))
    present = set()
    kernel_listing = True
    if src:
        status, payload = _stat(src, parent_dir)
        if status == "ok" and payload.get("remote"):
            present = _remote_listdir(src, parent_dir)
            kernel_listing = False  # NEVER kernel-list a remote dir
        # remote False -> kernel listdir below; missing/unreachable ->
        # presume local and fall back to the kernel probe.
    if kernel_listing:
        try:
            present = set(os.listdir(parent_dir))
        except OSError:
            present = set()
    siblings = {}
    for node in nodes:
        name = node["udfName"]
        if not name:
            continue
        found = [ext for ext in SIBLING_EXTS if (name + ext) in present]
        if found:
            siblings[name] = found

    return {
        "name": text(doc.get("name")) or None,
        "version": doc.get("version"),
        "previewImageUrl": text(doc.get("previewImageUrl")) or None,
        "nodes": nodes,
        "folders": folders,
        "edges": edges,
        "viewport": viewport,
        "viewportBounds": viewport_bounds,
        "siblings": siblings,
    }


# The fused-render engine / app runner only invoke a @fused.udf-registered
# entrypoint; a bare main() returns null under them. Register main via the shim
# (the house pattern — las/usd/pdf readers) so it runs under the engine, while
# `main` stays a plain callable for the built-in executor and for tests. (A
# directly-decorated `main` would sandbox its __call__ against the local
# filesystem, which reading canvas.toml needs.)
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
