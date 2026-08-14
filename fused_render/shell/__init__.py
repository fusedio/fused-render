"""Shell-specific state backends.

This package holds the backends for the React shell's own persisted state —
bookmarks today, small user-config resources later — deliberately kept
separate from the HTML-rendering / filesystem internals in server.py. Every
resource is a plain JSON file under one user-data dir (see storage.py), so a
new resource is just a new module here reusing read_json/write_json + an
APIRouter that create_app includes.
"""
