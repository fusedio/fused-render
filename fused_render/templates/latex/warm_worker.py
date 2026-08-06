"""Detached one-time warm of the Tectonic package cache, spawned by engine.py's
`_ensure_warming` on the first compile after Tectonic is installed. A cold
compile fetches ~30 MB of packages + fonts from the network (~2 min) — far past
the runPython budget — so we do it once here, out of band, and drop a `.warmed`
marker in the cache dir. Compiles then hit a warm cache (~4 s). Progress is
reported via a JSON file the page polls.

Run detached:  python warm_worker.py <tectonic_bin> <cache_dir> <warm_dir>
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


def warm(bin_path, cache_dir, warm_dir):
    os.makedirs(warm_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    try:
        _progress(warm_dir, "warm", "downloading LaTeX packages and fonts (one-time)")
        work = tempfile.mkdtemp(prefix="tectonic-warm-")
        tex = os.path.join(work, "warm.tex")
        with open(tex, "w", encoding="utf-8") as f:
            f.write(WARM_TEX)
        outdir = os.path.join(work, "out")
        os.makedirs(outdir, exist_ok=True)
        env = dict(os.environ, TECTONIC_CACHE_DIR=cache_dir)
        p = subprocess.run(
            [bin_path, "-X", "compile", "--outdir", outdir, tex],
            env=env, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if p.returncode == 0:
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
    warm(sys.argv[1], sys.argv[2], sys.argv[3])
