"""Detached installer for the Tectonic LaTeX engine, spawned by engine.py's
`tectonic_install` action when no `tectonic` binary is found on PATH or in
~/.fused-render/bin/. Downloads the platform's static binary straight from the
project's GitHub release (no vendored binary in this repo) and reports
progress via a JSON file the page polls (runPython has a 30s budget; a cold
download can run longer than that on a slow connection).

Once the binary lands it also warms the base package/font cache on a trivial
document — the ~50 MB, ~485-file one-time fetch — so that cost is paid up front
during install rather than gating the user's first real compile. It writes the
same WARM_DIR/progress.json + `.warmed` marker engine.py's own warm path uses,
and claims that warm slot before the download starts, so a compile that fires
the instant the binary appears waits on this fetch instead of launching a
second, competing one.

Run detached:
  python install_worker.py <version> <bin_dir> <progress_dir> [<cache_dir> <warm_dir>]
"""
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

CHUNK = 1 << 20

# A trivial document that still pulls in the packages the built-in project
# templates use (amsmath, hyperref, geometry, graphicx, booktabs), so warming it
# caches the common set, not just the bare format.
_WARM_DOC = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,amsthm}
\usepackage[margin=1in]{geometry}
\usepackage{graphicx}
\usepackage[colorlinks=true]{hyperref}
\usepackage{booktabs}
\begin{document}
Warming the LaTeX package cache. $E = mc^2$
\end{document}
"""


def _warm_progress(warm_dir, stage, detail, done=False, error=None):
    tmp = os.path.join(warm_dir, "progress.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "detail": detail, "done": done, "error": error,
                   "pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, os.path.join(warm_dir, "progress.json"))


def _warm_base(cache_dir, warm_dir, tectonic_bin):
    """Fetch the base LaTeX format + common packages/fonts once, on a trivial
    doc, into the shared cache — no timeout. A `.log` means Tectonic fetched what
    it needed and started typesetting, so the cache is warm even if the trivial
    doc itself hiccuped; that drops the `.warmed` marker."""
    marker = os.path.join(cache_dir, ".warmed")
    if os.path.exists(marker):
        _warm_progress(warm_dir, "done", "LaTeX packages ready", done=True)
        return
    scratch = os.path.join(warm_dir, "_bootstrap")
    os.makedirs(scratch, exist_ok=True)
    doc = os.path.join(scratch, "warm.tex")
    with open(doc, "w", encoding="utf-8") as f:
        f.write(_WARM_DOC)
    _warm_progress(warm_dir, "warm",
                   "downloading the base LaTeX packages and fonts (one-time)")
    env = dict(os.environ, TECTONIC_CACHE_DIR=cache_dir)
    subprocess.run(
        [tectonic_bin, "-X", "compile", "--keep-logs", "--outdir", scratch, doc],
        env=env, cwd=scratch, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if os.path.exists(os.path.join(scratch, "warm.log")):
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        _warm_progress(warm_dir, "done", "LaTeX packages ready", done=True)
    else:
        _warm_progress(warm_dir, "error", "couldn't fetch packages", done=True,
                       error="Couldn't prepare the LaTeX packages — check your "
                             "connection and open the document again to retry.")


class Progress:
    def __init__(self, progress_dir):
        self.path = os.path.join(progress_dir, "progress.json")

    def update(self, stage, pct, detail="", done=False, error=None):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "pct": round(float(pct), 1), "detail": detail,
                       "done": done, "error": error, "pid": os.getpid(),
                       "ts": time.time()}, f)
        os.replace(tmp, self.path)

    def fail(self, message):
        self.update("error", 100, message, done=True, error=message)


def _asset(version):
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        return f"tectonic-{version}-x86_64-unknown-linux-musl.tar.gz", "tar.gz"
    if system == "Darwin":
        arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"tectonic-{version}-{arch}-apple-darwin.tar.gz", "tar.gz"
    if system == "Windows":
        return f"tectonic-{version}-x86_64-pc-windows-msvc.zip", "zip"
    raise RuntimeError(f"unsupported platform: {system}")


def _extract_binary(archive_path, kind, dest_bin):
    member_name = os.path.basename(dest_bin)
    if kind == "tar.gz":
        with tarfile.open(archive_path, "r:gz") as tf:
            member = next((m for m in tf.getmembers()
                          if os.path.basename(m.name) == member_name), None)
            if not member:
                raise RuntimeError("tectonic binary not found in archive")
            with tf.extractfile(member) as src, open(dest_bin, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with zipfile.ZipFile(archive_path) as zf:
            member = next((n for n in zf.namelist()
                          if os.path.basename(n) == member_name), None)
            if not member:
                raise RuntimeError("tectonic.exe not found in archive")
            with zf.open(member) as src, open(dest_bin, "wb") as dst:
                shutil.copyfileobj(src, dst)


def install(version, bin_dir, progress_dir, cache_dir=None, warm_dir=None):
    prog = Progress(progress_dir)
    # Claim the warm slot before the download starts (see module docstring).
    if warm_dir:
        os.makedirs(warm_dir, exist_ok=True)
        _warm_progress(warm_dir, "install", "installing the LaTeX engine")
    try:
        prog.update("start", 0, "starting Tectonic download")
        name, kind = _asset(version)
        url = (f"https://github.com/tectonic-typesetting/tectonic/releases/"
               f"download/tectonic%40{version}/{name}")
        os.makedirs(bin_dir, exist_ok=True)
        archive_path = os.path.join(progress_dir, name)
        req = urllib.request.Request(url, headers={"User-Agent": "fused-render"})
        with urllib.request.urlopen(req, timeout=30) as r, open(archive_path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                pct = 90.0 * got / total if total else 50.0
                prog.update("download", pct, f"downloading {got >> 20} MB")

        prog.update("extract", 92, "extracting")
        bin_name = "tectonic.exe" if kind == "zip" else "tectonic"
        dest_bin = os.path.join(bin_dir, bin_name)
        _extract_binary(archive_path, kind, dest_bin)
        os.remove(archive_path)
        mode = os.stat(dest_bin).st_mode
        os.chmod(dest_bin, mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        prog.update("done", 100, f"installed to {dest_bin}", done=True)
    except Exception as e:
        prog.fail(
            f"{type(e).__name__}: {e} — install manually from "
            "https://tectonic-typesetting.github.io/ and place `tectonic` on "
            f"your PATH or at {bin_dir}"
        )
        if warm_dir:
            _warm_progress(warm_dir, "error", "install failed", done=True,
                           error="Couldn't install the LaTeX engine — see the "
                                 "install error above.")
        return

    if cache_dir and warm_dir:
        os.makedirs(cache_dir, exist_ok=True)
        _warm_base(cache_dir, warm_dir, dest_bin)


if __name__ == "__main__":
    install(*sys.argv[1:6])
