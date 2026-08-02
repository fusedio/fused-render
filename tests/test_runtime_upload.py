"""The injected runtime can write BYTES and create a directory (SPEC RH-*).

`fused.writeFile` takes UTF-8 text only, so until now a page could not put a
pasted screenshot or a video on disk at all, and could not make the folder to
put it in. `fused.uploadFile(path, blob)` posts the blob as multipart to
/api/fs/upload, and `fused.mkdir(path)` fronts the existing /api/fs/mkdir.

These are string-contract checks over the shipped `static/runtime.js`, the same
D137 wiring-assertion style as tests/test_runtime_cancellation.py: the
end-to-end behaviour is exercised through the endpoint's own tests, and these
guard the app-facing surface from silently regressing.
"""
from pathlib import Path

import fused_render

RUNTIME = (Path(fused_render.__file__).parent / "static" / "runtime.js").read_text(
    encoding="utf-8")


def test_upload_and_mkdir_are_on_the_fused_surface():
    # A template can only reach what `window.fused` exports — defining the
    # function without exporting it is the whole failure mode this pins.
    surface = RUNTIME.split("window.fused = {", 1)[1].split("};", 1)[0]
    assert "uploadFile," in surface
    assert "mkdir," in surface


def test_upload_posts_multipart_to_the_upload_endpoint():
    assert 'fetch("/api/fs/upload"' in RUNTIME
    assert "new FormData()" in RUNTIME


def test_upload_lets_the_browser_set_the_multipart_boundary():
    """No explicit Content-Type on the upload fetch.

    Setting `multipart/form-data` by hand omits the `boundary=` the browser
    would have generated, and the server then fails to parse a body that looks
    perfectly fine on the wire — so the header list must carry X-Fused only.
    """
    body = RUNTIME.split("function uploadFile(", 1)[1].split("\n  }\n", 1)[0]
    assert "multipart/form-data" not in body
    assert '"Content-Type"' not in body
    assert '"X-Fused": "1"' in body


def test_upload_and_mkdir_type_their_refusals_like_writeFile():
    # Callers branch on `err.type`, not on message text: a read-only target
    # refuses with 403 {"error":"readonly"} (RO-2) and an existing directory
    # 409s — the markdown template's ensure-assets/ treats that 409 as success.
    upload = RUNTIME.split("function uploadFile(", 1)[1].split("\n  }\n", 1)[0]
    assert 'err.type = "readonly"' in upload
    mkdir = RUNTIME.split("function mkdir(", 1)[1].split("\n  }\n", 1)[0]
    assert 'err.type = "readonly"' in mkdir
    assert 'err.type = "exists"' in mkdir
