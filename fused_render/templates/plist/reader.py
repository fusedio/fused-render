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
import json
import os
import plistlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
import remote_cache  # noqa: E402  (stdlib-only shared helper, see templates/shared/)


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
    """`(path, remote)`: a stable path for the converted JSON, keyed on the
    plist's real path — under the system temp dir, or the remote cache's
    guarded `.previews/` when the plist is a bucket download
    (templates/shared/remote_cache.py)."""
    root, remote = remote_cache.preview_root(file, "plist")
    stem = os.path.splitext(os.path.basename(os.path.realpath(file)))[0] or "plist"
    return os.path.join(root, stem + ".json"), remote


def main(file: str, action: str = "convert") -> dict:
    if action != "convert":
        raise ValueError(f"unknown action: {action!r}")
    with open(file, "rb") as fh:
        data = plistlib.load(fh)
    out, remote = _out_path(file)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(_jsonify(data), fh, ensure_ascii=False, indent=2)
    return {"path": out, "size": os.path.getsize(out), "remote": remote}
