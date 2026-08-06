"""Detached background LaTeX compile, spawned by engine.py when a compile can't
fit the runPython budget (a cold Tectonic fetch of ~30 MB of packages/fonts
runs ~2 min). Runs out of band and reports progress via a JSON file the page
polls. Two modes:

  * scaffold (no main_path): compile a common-package scaffold and drop a
    `.warmed` marker, so a fresh install's first real compile is fast.
  * document (main_path + build_dir): compile the actual document into its real
    build dir with no timeout, so a document whose packages exceed the budget
    completes out of band — the page then serves the PDF it produced.

Run detached:
  python warm_worker.py <tectonic_bin> <cache_dir> <warm_dir> [<main_path> <build_dir>]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

# Pulls the packages the built-in scaffolds use (article/report/beamer/letter
# share this common set), so one warm run covers the create-new templates.
WARM_TEX = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,amsthm}
\usepackage[margin=1in]{geometry}
\usepackage{graphicx}
\usepackage[colorlinks=true,linkcolor=blue,citecolor=teal,urlcolor=blue]{hyperref}
\usepackage{booktabs}
\begin{document}
Warming the LaTeX package cache.
\end{document}
"""


def _progress(warm_dir, stage, detail, done=False, error=None):
    tmp = os.path.join(warm_dir, "progress.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "detail": detail, "done": done, "error": error,
                   "pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, os.path.join(warm_dir, "progress.json"))


def _run(bin_path, cache_dir, args, cwd):
    env = dict(os.environ, TECTONIC_CACHE_DIR=cache_dir)
    return subprocess.run(
        [bin_path, "-X", "compile", *args], env=env, cwd=cwd,
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def warm(bin_path, cache_dir, warm_dir, main_path=None, build_dir=None):
    os.makedirs(warm_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    try:
        if main_path:
            _progress(warm_dir, "warm", "fetching this document's LaTeX packages (one-time)")
            os.makedirs(build_dir, exist_ok=True)
            p = _run(bin_path, cache_dir,
                     ["--keep-logs", "--synctex", "--outdir", build_dir, main_path],
                     cwd=os.path.dirname(main_path))
        else:
            _progress(warm_dir, "warm", "downloading LaTeX packages and fonts (one-time)")
            work = tempfile.mkdtemp(prefix="tectonic-warm-")
            tex = os.path.join(work, "warm.tex")
            with open(tex, "w", encoding="utf-8") as f:
                f.write(WARM_TEX)
            outdir = os.path.join(work, "out")
            os.makedirs(outdir, exist_ok=True)
            p = _run(bin_path, cache_dir, ["--outdir", outdir, tex], cwd=work)

        if p.returncode == 0:
            # The scaffold run marks the cache "warmed"; a document run leaves no
            # marker (it just populated the cache + produced the doc's PDF).
            if not main_path:
                with open(os.path.join(cache_dir, ".warmed"), "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
            _progress(warm_dir, "done", "LaTeX packages ready", done=True)
        else:
            tail = "\n".join((p.stderr or "").splitlines()[-8:])
            _progress(warm_dir, "error", tail or "package fetch failed", done=True,
                      error="Couldn't fetch the LaTeX packages — check your connection "
                            "and try again.")
    except Exception as e:  # noqa: BLE001 — any failure must land in the progress file
        _progress(warm_dir, "error", f"{type(e).__name__}: {e}", done=True,
                  error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    warm(sys.argv[1], sys.argv[2], sys.argv[3],
         main_path=sys.argv[4] if len(sys.argv) > 4 else None,
         build_dir=sys.argv[5] if len(sys.argv) > 5 else None)
