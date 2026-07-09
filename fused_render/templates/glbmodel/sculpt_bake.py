"""Write a client-baked part GLB to disk (sculpt/paint Save endpoint).

The viewer's sculpt and paint modes bake edits into the part's GLB bytes
**client-side** (sculpt_bake.mjs, a parity-proven port of the reference's
Python bake()); Save ships the patched file here as base64. This `main` only
validates (existing frozen part, GLB magic/declared-length, size cap) and
overwrites <model_dir>/parts/<part>.glb. Stdlib only.

Ported from the reference model-editor's core/sculpt_bake.py, dropping the
bake()/parity-oracle helpers (the JS port owns the actual geometry surgery)
and retargeting from workspace/<model> to an absolute *.glbproj/ dir.
"""

import base64
import os
import struct

try:
    import fused
    _udf = fused.udf
except ImportError:  # headless / plain-python testing
    def _udf(f):
        return f

_MAGIC = 0x46546C67  # "glTF"
_MAX_GLB = 64 * 1024 * 1024


@_udf
def main(model_dir: str = "", part: str = "", data_b64: str = "") -> dict:
    """Validated write of client-baked GLB bytes over an existing frozen part.
    Errors return {"error": ...} (the viewer shows them in-status)."""
    if not model_dir or not model_dir.rstrip("/").endswith(".glbproj"):
        return {"error": f"not a .glbproj directory: {model_dir}"}
    model_dir = model_dir.rstrip("/")
    if os.sep in part or part in ("", ".", ".."):
        return {"error": f"bad part name: {part!r}"}
    path = os.path.join(model_dir, "parts", f"{part}.glb")
    if not os.path.exists(path):
        return {"error": f"not an existing frozen part: {part}"}
    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception:
        return {"error": "data_b64 is not valid base64"}
    if len(data) > _MAX_GLB:
        return {"error": f"GLB too large ({len(data)} bytes > {_MAX_GLB})"}
    if len(data) < 12:
        return {"error": "GLB too small"}
    magic, _, declared = struct.unpack_from("<III", data, 0)
    if magic != _MAGIC or declared != len(data):
        return {"error": "not a valid GLB (bad magic or length)"}
    with open(path, "wb") as fh:
        fh.write(data)
    return {"part": part, "path": path, "bytes": len(data)}
