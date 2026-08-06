"""Detached background LaTeX compile, spawned by engine.py when a foreground
compile can't fit the runPython budget — almost always a cold Tectonic fetch of
the packages/fonts a document needs (~30 MB, ~2 min). It compiles the document
into its real build dir with no timeout, so any package set completes out of
band; the page polls `warm_status` and, once this finishes, a plain recompile
serves the PDF it produced. A `.warmed` marker records that the cache has been
populated at least once, letting a fresh install skip the doomed inline attempt
on its very first compile. Progress is written to a JSON file the page polls.

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


def warm(bin_path, cache_dir, warm_dir, main_path, build_dir):
    os.makedirs(warm_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(build_dir, exist_ok=True)
    try:
        _progress(warm_dir, "warm", "downloading the LaTeX packages this document needs (one-time)")
        env = dict(os.environ, TECTONIC_CACHE_DIR=cache_dir)
        subprocess.run(
            [bin_path, "-X", "compile", "--keep-logs", "--synctex",
             "--outdir", build_dir, main_path],
            env=env, cwd=os.path.dirname(main_path), capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        # A produced PDF means the fetch succeeded (the foreground recompile then
        # serves it and surfaces any LaTeX diagnostics); no PDF is a real failure.
        stem = os.path.splitext(os.path.basename(main_path))[0]
        if os.path.exists(os.path.join(build_dir, stem + ".pdf")):
            with open(os.path.join(cache_dir, ".warmed"), "w", encoding="utf-8") as f:
                f.write(str(time.time()))
            _progress(warm_dir, "done", "LaTeX packages ready", done=True)
        else:
            _progress(warm_dir, "error", "compile produced no PDF", done=True,
                      error="Couldn't prepare the LaTeX packages — check your connection "
                            "and try again.")
    except Exception as e:  # noqa: BLE001 — any failure must land in the progress file
        _progress(warm_dir, "error", f"{type(e).__name__}: {e}", done=True,
                  error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    warm(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
