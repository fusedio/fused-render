"""Report the bundled Python version and the installed versions of the
supported library set.

Called from the Learn "Building with Fused Render" page via:
    await fused.runPython("./check_libs.py", {})

Returns:
    {"python": "3.12.13", "libs": [{"name": str, "import": str, "version": str|None}, ...]}

Runs inside FusedRender's bundled Python itself, so importlib.metadata sees
exactly what a user's page code can import — the list can't drift from the app.
"""

# Distribution names as they appear in pyproject's [bundled] extra (plus the
# core deps pages can also import), grouped for the Learn page table.
#
# This is a PROMISE, so it is checked rather than trusted: every name here must
# be something the packaged app really ships
# (tests/test_bundle_contents.py::test_the_learn_page_only_promises_what_the_app_ships).
# The reverse is not required — this list is curated, and the app's own
# plumbing (fastapi, packaging, tomli, …) is not something a page should import.
#
# What is deliberately NOT here: polars, matplotlib, scipy, geopandas, shapely,
# rasterio, zarr, pymupdf, pikepdf, fpdf2. They left the bundle in D276 —
# 556 MB that every user carried for a minority of pages. They are still one
# line away: a `pyproject.toml` next to your .py naming them gets that folder
# its own environment, built on first run (SPEC PY-16/PY-18). The Learn page
# says so under the table; keep the two in step.
SUPPORTED = [
    ("Data", ["numpy", "pandas", "pyarrow", "duckdb", "openpyxl", "msgpack"]),
    ("Images", ["pillow"]),
    ("Documents", ["python-pptx"]),
    ("Network & cloud", ["requests", "httpx", "botocore", "google-auth"]),
    ("Logs", ["drain3"]),
]


def main():
    import platform
    from importlib import metadata

    libs = []
    for group, names in SUPPORTED:
        for name in names:
            try:
                version = metadata.version(name)
            except Exception:
                version = None
            libs.append({"name": name, "group": group, "version": version})

    return {"python": platform.python_version(), "libs": libs}
