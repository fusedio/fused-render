"""Reader backing plist/template.html. Converts an Apple property list — binary
OR XML, autodetected by plistlib — into a temp JSON file and returns its path,
so the page can hand it to the app's normal JSON tree viewer instead of
reimplementing a plist renderer.

Types plistlib emits that JSON can't represent natively are made lossless-ish:
  - bytes (<data>)    -> {"__data_base64__": "..."}
  - datetime (<date>) -> ISO-8601 string
  - plistlib.UID      -> {"__uid__": <int>}   (keyed-archive references)
"""

import base64
import datetime
import hashlib
import json
import os
import plistlib
import tempfile


def _jsonify(value):
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return {"__data_base64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, plistlib.UID):
        return {"__uid__": value.data}
    return value  # str / int / float / bool / None are already JSON-safe


def _out_path(file):
    """Stable temp path for the converted JSON, keyed on the plist's real path so
    repeated opens reuse one file instead of littering temp."""
    real = os.path.realpath(file)
    key = hashlib.sha1(real.encode("utf-8")).hexdigest()[:16]
    root = os.path.join(tempfile.gettempdir(), "fused-render-plist", key)
    os.makedirs(root, exist_ok=True)
    stem = os.path.splitext(os.path.basename(real))[0] or "plist"
    return os.path.join(root, stem + ".json")


def main(file: str, action: str = "convert") -> dict:
    if action != "convert":
        raise ValueError(f"unknown action: {action!r}")
    with open(file, "rb") as fh:
        data = plistlib.load(fh)
    out = _out_path(file)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(_jsonify(data), fh, ensure_ascii=False, indent=2)
    return {"path": out, "size": os.path.getsize(out)}
