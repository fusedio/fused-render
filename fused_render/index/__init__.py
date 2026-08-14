"""The local filesystem metadata index (paths, names, extensions, sizes,
mtimes) — a path-sorted parquet store written by a detached scan worker and
read with duckdb.

Ported from the OpenIndex fused-render app (runner.py + query.py). What
changed in the move, and why:

  * **No module-level configuration.** OpenIndex froze the index dir, the
    ignore list and the worker count at IMPORT time, which is fine when every
    call is a fresh `fused.runPython` subprocess and wrong inside a long-lived
    server — the values would go stale the moment a user edited them. They now
    travel in an explicit `IndexConfig` (config.py) resolved per call and
    carried to worker processes in the run's `spec.json`.
  * **Storage under `storage.home_dir()/index`** instead of three scattered
    `~/.fused-render/cache/OpenIndex*` paths, so FUSED_RENDER_HOME and branch
    nesting redirect it like every other persistent store (and tests never
    touch a real home).
  * **`python -m fused_render.index.worker`** rather than `Popen([python,
    __file__])`, which has no meaning inside a py2app bundle.
  * **A structural mount guard** (ignore.MountGuard) on top of the ignore
    entry for the mounts dir: kernel FS syscalls on an rclone NFS mount path
    can wedge the mount permanently, so refusing them cannot be left to a
    user-editable list.
  * **No raw `sql` action.** OpenIndex handed arbitrary duckdb statements to a
    trusted local page; behind an HTTP route that is an arbitrary read/write
    surface. `stats` and `lookup` remain.

Specs live in `fused_render/index/specs/`.
"""

from fused_render.index.config import IndexConfig, load_config, save_config

__all__ = ["IndexConfig", "load_config", "save_config"]
