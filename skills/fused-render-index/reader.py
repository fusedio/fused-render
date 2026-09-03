"""Read-only access to the fused-render file index. Only dep: duckdb.

Copy this file into the app (declare `duckdb` in its pyproject.toml) — app
venvs cannot `import fused_render`.
"""
import json, os, duckdb


def store_dir(location=None):
    # Prefer the `location` that /api/index/stats reports; the env resolution
    # below is the fallback (fused.runPython inherits os.environ).
    if location:
        return location
    # FUSED_RENDER_HOME_DIR is exported by the server ALREADY BRANCH-RESOLVED
    # (server/app.py:export_app_env), so re-derive nothing from it: no branches/
    # nesting, no ref sanitizing. Duplicating those rules is how the copies
    # drift, and a branch build's baked ref is not even in the environment —
    # same reasoning as templates/shared/appenv.py:home_dir (SPEC PY-15, D166).
    home = os.environ.get("FUSED_RENDER_HOME_DIR")
    if not home:
        # No server around: the un-branched baseline, the same override the app
        # honors. A branch-scoped store is only findable via the server.
        home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser(
            "~/.fused-render")
    return os.path.join(home, "index")


def connect(location=None):
    """A duckdb connection with `files` and `dirs` views, plus the manifest.

    Returns (None, None) for every "nothing to read yet" shape: no manifest, an
    unreadable one, a manifest naming zero partitions, or no dirs.parquet.
    "No index" is an answer to render, not an error to raise.
    """
    d = store_dir(location)
    try:
        with open(os.path.join(d, "partitions.json")) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None, None
    parts = [os.path.join(d, "files", p["file"])
             for p in manifest.get("partitions") or []]
    dirs = os.path.join(d, "dirs.parquet")
    # Legitimate empty stores, not corruption; read_parquet raises on both.
    if not parts or not os.path.exists(dirs):
        return None, None
    con = duckdb.connect()
    # NEVER a files/*.parquet glob: old generations stay on disk beside new
    # ones and double-count. Relation API because CREATE VIEW refuses ? params.
    con.read_parquet(parts).create_view("files")
    con.read_parquet(dirs).create_view("dirs")
    return con, manifest


def main(min_size="0"):
    con, manifest = connect()
    if con is None:
        return {"indexed": False, "rows": []}  # say so, don't return []
    rows = con.execute(
        "SELECT ext, count(*) n, sum(size) bytes FROM files "
        "WHERE size >= ? GROUP BY 1 ORDER BY bytes DESC LIMIT 20",
        [int(min_size)]).fetchall()
    return {"indexed": True, "updated": manifest["updated"],
            "rows": [{"ext": e, "n": n, "bytes": b} for e, n, b in rows]}
