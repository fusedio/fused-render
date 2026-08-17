"""Detached background LaTeX compile, spawned by engine.py when a foreground
compile can't fit the runPython budget — almost always a cold Tectonic fetch
(~50 MB, several minutes) of the packages a document needs. Compiles into the
real build dir under a generous timeout backstop, drops a `.warmed` marker once
Tectonic emits a .log (packages fetched, even if the doc itself then failed),
and writes progress the page polls; the page recompiles when this finishes.

Run detached:
  python warm_worker.py <tectonic_bin> <cache_dir> <warm_dir> <main_path> <build_dir>
"""
import json
import os
import subprocess
import sys
import time

WARM_TIMEOUT_S = 30 * 60   # generous backstop so a stalled fetch can't hang forever


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
        # Clear a prior run's PDF/log so their presence reflects THIS run — a
        # stale .log would otherwise wrongly mark the cache warmed.
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
            timeout=WARM_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            encoding="utf-8", errors="replace")
        # A .log means Tectonic fetched what it needed and started typesetting, so
        # the cache is warm even if the doc then failed — mark it and let the page's
        # recompile surface the real diagnostics. Only a run that wrote no log
        # truly failed to fetch (offline / repo unreachable).
        if os.path.exists(os.path.join(build_dir, stem + ".log")):
            _mark_warmed(cache_dir)
            _progress(warm_dir, "done", "LaTeX packages ready", done=True)
        else:
            _progress(warm_dir, "error", "couldn't fetch packages", done=True,
                      error="Couldn't prepare the LaTeX packages — check your connection "
                            "and try again.")
    except subprocess.TimeoutExpired:
        _progress(warm_dir, "error", "package fetch timed out", done=True,
                  error="Preparing the LaTeX packages took too long — check your "
                        "connection and try again.")
    except Exception as e:  # noqa: BLE001 — any failure must land in the progress file
        _progress(warm_dir, "error", f"{type(e).__name__}: {e}", done=True,
                  error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    warm(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
