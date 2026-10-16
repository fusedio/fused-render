import importlib.util
import json
import os
import shutil
import subprocess
import sys

VERSION = "3.12.1"
BIN_DIR = os.path.join(os.path.expanduser("~"), ".fused-render", "bin")
STAGE_DIR = os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "_pandoc_install")
CHUNK = 1 << 20


def _load_sibling_procutil():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "procutil.py")
    spec = importlib.util.spec_from_file_location("_pandoc_bin_procutil", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


procutil = _load_sibling_procutil()


def _local():
    return os.path.join(BIN_DIR, "pandoc")


def path():
    try:
        import pypandoc
        return pypandoc.get_pandoc_path()
    except (ImportError, OSError):
        pass
    return _local() if os.path.exists(_local()) else None


def _report(stage, pct, detail, pid=None, done=False, error=None):
    os.makedirs(STAGE_DIR, exist_ok=True)
    target = os.path.join(STAGE_DIR, "progress.json")
    with open(target + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "pct": round(float(pct), 1), "detail": detail,
                   "done": done, "error": error, "pid": pid or os.getpid()}, f)
    os.replace(target + ".tmp", target)


def _progress():
    try:
        with open(os.path.join(STAGE_DIR, "progress.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not data.get("done") and not procutil.pid_alive(data.get("pid", -1)):
        data["done"] = True
        data["error"] = data.get("error") or "installer exited unexpectedly"
    return data


def status():
    exe = path()
    return {"available": exe is not None, "path": exe, "progress": _progress()}


def install():
    prog = _progress()
    if path() or (prog and not prog.get("done")):
        return status()
    os.makedirs(STAGE_DIR, exist_ok=True)
    with open(os.path.join(STAGE_DIR, "worker.log"), "ab") as log:
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                                 stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
    _report("spawn", 0, "starting pandoc download", pid=child.pid)
    return status()


def _asset():
    import platform
    system, machine = platform.system(), platform.machine()
    if system == "Darwin":
        arch = "arm64" if machine == "arm64" else "x86_64"
        return f"pandoc-{VERSION}-{arch}-macOS.zip", f"pandoc-{VERSION}-{arch}/bin/pandoc"
    if system == "Linux":
        arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
        return f"pandoc-{VERSION}-linux-{arch}.tar.gz", f"pandoc-{VERSION}/bin/pandoc"
    raise RuntimeError(f"no pandoc download for {system}")


def _extract(archive, member, dest):
    if archive.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive) as zf, zf.open(member) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        import tarfile
        with tarfile.open(archive) as tf, tf.extractfile(member) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _fetch(url, dest):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "fused-render"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            _report("download", 90.0 * got / total if total else 50.0,
                    f"downloading {got >> 20} MB")


def _run():
    part = f"{_local()}.{os.getpid()}.part"
    try:
        name, member = _asset()
        archive = os.path.join(STAGE_DIR, name)
        os.makedirs(STAGE_DIR, exist_ok=True)
        _fetch(f"https://github.com/jgm/pandoc/releases/download/{VERSION}/{name}", archive)
        _report("extract", 92, "extracting")
        os.makedirs(BIN_DIR, exist_ok=True)
        _extract(archive, member, part)
        os.chmod(part, 0o755)
        subprocess.run([part, "--version"], check=True, capture_output=True, timeout=60)
        os.replace(part, _local())
        os.remove(archive)
        _report("done", 100, f"installed to {_local()}", done=True)
    except Exception as e:
        if os.path.exists(part):
            os.remove(part)
        message = (f"{type(e).__name__}: {e}. Install pandoc from "
                   "https://pandoc.org/installing.html and try again")
        _report("error", 100, message, done=True, error=message)


if __name__ == "__main__":
    _run()
