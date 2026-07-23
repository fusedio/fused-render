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
SUPPORTED = [
    ("Data", ["numpy", "pandas", "polars", "pyarrow", "duckdb", "scipy", "openpyxl", "msgpack"]),
    ("Geospatial", ["shapely", "geopandas", "rasterio", "zarr"]),
    ("Plots & images", ["matplotlib", "pillow"]),
    ("Documents", ["pymupdf", "pikepdf", "fpdf2", "python-pptx"]),
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
