"""Detached background LaTeX compile, spawned by engine.py when a foreground
compile can't fit the runPython budget — almost always a cold Tectonic fetch of
the packages/fonts a document needs (~50 MB, several minutes). It compiles the document
into its real build dir with no timeout, so any package set completes out of
band; the page polls `warm_status` and, once this finishes, a plain recompile
serves the resulting PDF (or shows the document's real compile errors). A
`.warmed` marker records that the cache has been populated at least once —
written whenever Tectonic got far enough to emit a .log, since that means the
packages were fetched even if the document itself then failed — letting a fresh
install skip the doomed inline attempt on its very first compile. Progress is
written to a JSON file the page polls.

Run detached:
  python warm_worker.py <tectonic_bin> <cache_dir> <warm_dir> <main_path> <build_dir>
"""
import json
import os
import subprocess
import sys
import time


def _progress(warm_dir, stage, detail, done=False, error=None):
    tmp = os.path.join(warm_dir, "progress.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "detail": detail, "done": done, "error": error,
                   "pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, os.path.join(warm_dir, "progress.json"))


def _mark_warmed(cache_dir):
    with open(os.path.join(cache_dir, ".warmed"), "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def warm(bin_path, cache_dir, warm_dir, main_path, build_dir):
    os.makedirs(warm_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(build_dir, exist_ok=True)
    try:
        _progress(warm_dir, "warm", "downloading the LaTeX packages this document needs (one-time)")
        env = dict(os.environ, TECTONIC_CACHE_DIR=cache_dir)
        # Clear a prior run's PDF and log first so their presence reflects THIS
        # warm run, not an earlier success. Otherwise a warm that fails before
        # TeX even starts (offline during the fetch) would find a stale .log,
        # wrongly mark the cache warmed, and the page would loop retrying an
        # unwarmed cache with the last good PDF already gone.
        stem = os.path.splitext(os.path.basename(main_path))[0]
        for leftover in (stem + ".pdf", stem + ".log"):
            try:
                os.remove(os.path.join(build_dir, leftover))
            except OSError:
                pass
        subprocess.run(
            [bin_path, "-X", "compile", "--keep-logs", "--synctex",
             "--outdir", build_dir, main_path],
            env=env, cwd=os.path.dirname(main_path), capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        # What we're really warming is the package cache; the PDF is a side
        # effect. Tectonic only writes a .log once it has fetched what it needs
        # and started typesetting, so a .log (with or without a PDF) means the
        # cache IS warm — even if the document then failed to compile. In that
        # case mark it warmed and finish cleanly; the foreground recompile the
        # page runs next surfaces the real LaTeX diagnostics, rather than us
        # blaming a genuine document error on the network. Only a run that never
        # wrote a log truly failed to fetch (offline / repo unreachable).
        if os.path.exists(os.path.join(build_dir, stem + ".log")):
            _mark_warmed(cache_dir)
            _progress(warm_dir, "done", "LaTeX packages ready", done=True)
        else:
            _progress(warm_dir, "error", "couldn't fetch packages", done=True,
                      error="Couldn't prepare the LaTeX packages — check your connection "
                            "and try again.")
    except Exception as e:  # noqa: BLE001 — any failure must land in the progress file
        _progress(warm_dir, "error", f"{type(e).__name__}: {e}", done=True,
                  error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    warm(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
