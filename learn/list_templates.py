"""List the Fused Render templates available on this machine.

Called from a Fused Render "Learn" page via:
    await fused.runPython("./list_templates.py", {})

Returns:
    {
      "core":   [{"name","title","extensions":[...]}, ...],   # sorted by name
      "custom": [{"name","title","extensions":[...]}, ...],   # [] if none
    }

Core templates live under ~/.fused-render/.core-templates/ (one subdir each).
The extension->template map is registry.json; we reverse it so each template
lists the extensions it handles. Custom templates are the non-dotfile
top-level dirs in ~/.fused-render/ that contain a template.html.
"""


def main():
    import os
    import json

    home = os.path.expanduser("~")
    root = os.path.join(home, ".fused-render")
    core_dir = os.path.join(root, ".core-templates")

    # Acronyms / brand names that title-case badly. Everything else just gets
    # underscores -> spaces and Title Case.
    NICE = {
        "duckdb": "DuckDB",
        "csv": "CSV",
        "tsv": "TSV",
        "xlsx": "XLSX",
        "pdf": "PDF",
        "pdf_studio": "PDF Studio",
        "geotiff": "GeoTIFF",
        "netcdf": "NetCDF",
        "html": "HTML",
        "api": "API",
        "h3": "H3",
        "usd": "USD",
        "las": "LAS",
        "glb": "GLB",
        "sqlite": "SQLite",
        "latex": "LaTeX",
        "pmtiles": "PMTiles",
        "zarr_aoi": "Zarr AOI",
        "zarr": "Zarr",
        "log_studio": "Log Studio",
        "geometry_editor": "Geometry Editor",
    }

    def titleize(name):
        if name in NICE:
            return NICE[name]
        return name.replace("_", " ").replace("-", " ").title()

    def load_ext_map():
        """template name -> sorted list of extensions it handles."""
        candidates = [
            os.path.join(core_dir, "registry.json"),
            "/Users/maximelenormand/Library/CloudStorage/Dropbox/Documents/"
            "repos/fused-render/fused_render/templates/registry.json",
        ]
        registry = None
        for p in candidates:
            try:
                with open(p) as f:
                    registry = json.load(f)
                break
            except Exception:
                continue
        rev = {}
        if isinstance(registry, dict):
            for ext, tmpls in registry.items():
                if not isinstance(tmpls, list):
                    continue
                for t in tmpls:
                    rev.setdefault(t, set()).add(ext)
        return {t: sorted(exts) for t, exts in rev.items()}

    def is_template_dir(path):
        try:
            return os.path.isfile(os.path.join(path, "template.html"))
        except Exception:
            return False

    ext_map = load_ext_map()

    def entry(name):
        return {
            "name": name,
            "title": titleize(name),
            "extensions": ext_map.get(name, []),
        }

    # --- Core templates ---------------------------------------------------
    core = []
    try:
        for name in os.listdir(core_dir):
            if name.startswith("."):
                continue
            full = os.path.join(core_dir, name)
            if os.path.isdir(full) and is_template_dir(full):
                core.append(entry(name))
    except Exception:
        pass
    core.sort(key=lambda e: e["name"].lower())

    # --- Custom templates -------------------------------------------------
    # Top-level dirs (or symlinks to dirs) directly under ~/.fused-render/
    # that are not dot-dirs and that contain a template.html.
    EXCLUDE = {".core-templates", ".import-staging"}
    custom = []
    try:
        for name in sorted(os.listdir(root), key=str.lower):
            if name.startswith(".") or name in EXCLUDE:
                continue
            full = os.path.join(root, name)
            if os.path.isdir(full) and is_template_dir(full):
                custom.append(entry(name))
    except Exception:
        pass

    return {"core": core, "custom": custom}
