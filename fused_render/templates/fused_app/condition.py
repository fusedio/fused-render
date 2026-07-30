"""Condition gate for the folder-level `fused_app` template (SPEC CT-12).

Runs on EVERY directory the user opens (the mode is bound on the universal
`/` key), so it must be constant-time and must NEVER enumerate the directory
— the rule `graph/condition.py` and `zarr_aoi/condition.py` document. The
probes here are one `isfile` for the marker, one bounded read of it, and one
`isfile` for the declared entry file; over a mount all three route through
the gate shim (`server.templates._mount_gate_builtins`).

`main(target_path)` is True only when the directory holds a genuine app
manifest: a `fused_app.json` that parses as a JSON object, declares a
`pages` array containing an item with `path: "/"` (the entry page) whose
string `file` actually exists inside the directory. Anything else — missing
marker, oversized file, malformed JSON, wrong shape, no "/" page, dangling
or escaping entry path — fails closed, so the app view never shows for an
ordinary folder.
"""
import os

# A manifest is a small metadata document, never megabytes. Anything larger
# is not ours to preview; skip the parse and fail closed (matches the canvas
# gate's posture).
MAX_BYTES = 256 * 1024


def main(target_path) -> bool:
    try:
        manifest = os.path.join(str(target_path), "fused_app.json")
        if not os.path.isfile(manifest):
            return False
        # Size guard before opening — a pathological file must not be parsed.
        # os.stat (not os.path.getsize) so the mount shim routes it off the
        # kernel NFS path.
        if os.stat(manifest).st_size > MAX_BYTES:
            return False
        import json
        with open(manifest, "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        if not isinstance(data, dict):
            return False
        pages = data.get("pages")
        if not isinstance(pages, list):
            return False
        # The entry page is the pages[] item routed at "/".
        entry = None
        for page in pages:
            if isinstance(page, dict) and page.get("path") == "/":
                entry = page.get("file")
                break
        if not isinstance(entry, str) or not entry:
            return False
        # The entry must live inside the app folder — an absolute path or a
        # `..` escape is not a valid manifest, not a file to probe.
        if entry.startswith("/") or ".." in entry.split("/"):
            return False
        return os.path.isfile(os.path.join(str(target_path), entry))
    except Exception:
        # Missing file, permission error, malformed JSON, decode error — a
        # gate that can't decide is not silently shown (SPEC CT-12): fail
        # closed.
        return False
