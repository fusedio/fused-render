"""Reader backing template.html — a read-only data-prep step run server-side.

`fused.runPython("./reader.py", { file, ... })` in the template calls `main`
with the query params as keyword args (all strings; annotate for coercion).
Return anything JSON-serialisable — it lands back in the page as the resolved
value. This runs on the machine hosting fused-render with full Python, so keep
it READ-ONLY (inspect the target file, never mutate it) and cheap (only touch
what the current view needs).

This stub returns a small summary plus a head of the file's bytes. Replace it
with whatever your template needs — parse the format, page a table, pull out
metadata. Stdlib only here so the starter has no third-party dependency; import
pandas/pyarrow/etc. freely once your template needs them.
"""

import os


def main(file: str, max_bytes: int = 4096) -> dict:
    size = os.path.getsize(file)
    with open(file, "rb") as fh:
        head = fh.read(max_bytes)
    # Decode as text when we can; otherwise hand back a hex preview so binary
    # files still render something instead of erroring.
    try:
        preview = head.decode("utf-8")
        is_text = True
    except UnicodeDecodeError:
        preview = head.hex()
        is_text = False
    return {
        "name": os.path.basename(file),
        "size": size,
        "isText": is_text,
        "truncated": size > len(head),
        "preview": preview,
    }
