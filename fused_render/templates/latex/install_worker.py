"""Detached installer for the Tectonic LaTeX engine, spawned by engine.py's
`tectonic_install` action when no `tectonic` binary is found on PATH or in
~/.fused-render/bin/. Downloads the platform's static binary straight from the
project's GitHub release (no vendored binary in this repo) and reports
progress via a JSON file the page polls (runPython has a 30s budget; a cold
download can run longer than that on a slow connection).

Run detached:  python install_worker.py <version> <bin_dir> <progress_dir>
"""
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import time
import urllib.request
import zipfile

CHUNK = 1 << 20


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


def install(version, bin_dir, progress_dir):
    prog = Progress(progress_dir)
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


if __name__ == "__main__":
    install(sys.argv[1], sys.argv[2], sys.argv[3])
