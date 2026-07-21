"""List the Fused Render templates available on this machine.

Called from a Fused Render "Learn" page via:
    await fused.runPython("./list_templates.py", {})

Returns:
    {
      "core":   [{"name","title","extensions":[...]}, ...],   # sorted by name
      "custom": [{"name","title","extensions":[...]}, ...],   # [] if none
    }

Core templates ship inside the `fused_render` package (`.../templates/`); user
("custom") templates live under `~/.fused-render/templates/` (decision D76).
Each is a folder containing a `template.html`. The extension->template map is
`registry.json` (core registry beside the core templates, user registry beside
the user templates); we reverse it so each template lists the extensions it
handles.
"""


def main():
    import os
    import json

    home = os.path.expanduser("~")
    root = os.path.join(home, ".fused-render")

    # Core templates ship with the package — locate them from the import path
    # (works in any install), falling back to the app's local mirror.
    core_dir = None
    try:
        import fused_render  # noqa: F401
        cand = os.path.join(os.path.dirname(fused_render.__file__), "templates")
        if os.path.isdir(cand):
            core_dir = cand
    except Exception:
        pass
    if not core_dir:
        for cand in (os.path.join(root, ".core-templates"),):
            if os.path.isdir(cand):
                core_dir = cand
                break
        else:
            core_dir = os.path.join(root, ".core-templates")

    # User templates + their registry (D76: nested under templates/); fall back
    # to the pre-D76 flat home root for users who haven't migrated.
    user_dir = os.path.join(root, "templates")
    if not os.path.isdir(user_dir):
        user_dir = root

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
        """template name -> sorted list of extensions it handles.

        Merges the core registry (beside the core templates) with the user
        registry (beside the user templates), so custom templates also show
        the extensions they're bound to.
        """
        rev = {}
        for p in (os.path.join(core_dir, "registry.json"),
                  os.path.join(user_dir, "registry.json")):
            try:
                with open(p) as f:
                    registry = json.load(f)
            except Exception:
                continue
            if not isinstance(registry, dict):
                continue
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
    # Folders (or symlinks to folders) under the user templates dir that
    # contain a template.html. Skip dot-dirs and known non-template siblings.
    EXCLUDE = {".core-templates", ".import-staging", "spec", "vendor", "shared"}
    custom = []
    try:
        for name in sorted(os.listdir(user_dir), key=str.lower):
            if name.startswith(".") or name in EXCLUDE:
                continue
            full = os.path.join(user_dir, name)
            if os.path.isdir(full) and is_template_dir(full):
                custom.append(entry(name))
    except Exception:
        pass

    return {"core": core, "custom": custom}
