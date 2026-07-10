# /// script
# dependencies = ["numpy>=1.26", "msgpack>=1.0", "usd-core>=24.5"]
# ///
"""Data/dispatch side of the usd preview template.

Actions (all return JSON-native dicts):
    inspect  — sniff the file, report cache state + manifest + progress
    prepare  — spawn the detached convert_worker (survives this subprocess)
    status   — read progress.json / manifest.json for the poll loop
    browse   — list loadable files in a directory for the file picker
"""

import hashlib
import json
import os
import subprocess
import sys
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    import os, sys
    __file__ = os.path.join(sys.path[0], "reader.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "usd"))

LOADABLE = (".usdz", ".usd", ".usda", ".usdc", ".ply", ".splat", ".ksplat",
            ".glb", ".gltf", ".obj", ".stl")


def _cache_dir(source):
    if source.startswith(("http://", "https://")):
        ident = source
        stem = os.path.splitext(os.path.basename(source.split("?")[0]))[0]
    else:
        st = os.stat(source)
        ident = f"{os.path.abspath(source)}:{st.st_size}:{st.st_mtime_ns}"
        stem = os.path.splitext(os.path.basename(source))[0]
    key = hashlib.sha1(ident.encode()).hexdigest()[:12]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:40]
    return os.path.join(CACHE_ROOT, f"{safe}-{key}")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    # os.kill(pid, 0) is the POSIX no-op liveness check, but on Windows signal 0
    # aliases CTRL_C_EVENT and doesn't reliably error on a dead pid — check the
    # process's exit code via the Win32 API instead.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _state(cache_dir, budget, crop=1):
    manifest = _read_json(os.path.join(cache_dir, "manifest.json"))
    progress = _read_json(os.path.join(cache_dir, "progress.json"))
    running = bool(progress and not progress.get("done")
                   and _pid_alive(progress.get("pid")))
    ready = bool(manifest and (
        manifest.get("kind") == "usd"
        or manifest.get("mesh")
        or f"{budget}c{crop}" in manifest.get("splatFiles", {})
        or str(budget) in manifest.get("pointFiles", {})))
    return {"cacheKey": os.path.basename(cache_dir),
            "cacheDir": os.path.abspath(cache_dir).replace(os.sep, "/"),
            "manifest": manifest,
            "progress": progress, "running": running, "ready": ready}


def main(action: str = "inspect", file: str = "", budget: int = 1000000,
         crop: int = 1, dir: str = ""):
    if action == "browse":
        base = dir or (os.path.dirname(file) if file else os.path.expanduser("~"))
        base = os.path.abspath(base)
        dirs, entries = [], []
        try:
            for name in sorted(os.listdir(base), key=str.lower):
                if name.startswith("."):
                    continue
                full = os.path.join(base, name)
                try:
                    if os.path.isdir(full):
                        dirs.append({"name": name, "path": full})
                    elif os.path.isfile(full) and name.lower().endswith(LOADABLE):
                        entries.append({"name": name, "path": full,
                                        "size": os.path.getsize(full)})
                except OSError:
                    continue  # unreadable entry (permissions, dangling link)
        except OSError as e:
            return {"dir": base, "parent": os.path.dirname(base),
                    "dirs": [], "entries": [], "error": str(e)}
        parent = os.path.dirname(base)
        return {"dir": base, "parent": parent if parent != base else None,
                "dirs": dirs, "entries": entries}

    if not file:
        return {"error": "no file given"}
    is_url = file.startswith(("http://", "https://"))
    if not is_url and not os.path.isfile(file):
        return {"error": f"file not found: {file}"}

    ext = os.path.splitext(file.split("?")[0])[1].lower()
    # .ply is NOT direct: gaussian plys need activation baking and plain
    # point-cloud plys (SLAM dumps) need synthesized gaussians server-side
    direct_splat = ext in (".splat", ".ksplat")
    # glTF is already the viewer's mesh format — hand it straight to GLTFLoader
    direct_mesh = ext in (".glb", ".gltf")
    direct = direct_splat or direct_mesh

    if action == "inspect":
        if direct:
            return {"kind": "mesh-direct" if direct_mesh else "splat-direct",
                    "ext": ext, "ready": True,
                    "size": None if is_url else os.path.getsize(file)}
        cd = _cache_dir(file)
        out = _state(cd, budget, crop)
        out.update({"kind": "usd", "ext": ext})
        return out

    if action == "prepare":
        if direct:
            return {"ready": True}
        cd = _cache_dir(file)
        st = _state(cd, budget, crop)
        if st["ready"] or st["running"]:
            return st
        os.makedirs(cd, exist_ok=True)
        worker = os.path.join(HERE, "convert_worker.py")
        logf = open(os.path.join(cd, "worker.log"), "ab")
        # detach: outlive this 30 s subprocess. start_new_session (setsid) is
        # POSIX-only and silently a no-op on Windows, where DETACHED_PROCESS +
        # CREATE_NEW_PROCESS_GROUP is the equivalent.
        detach_kwargs = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        child = subprocess.Popen(
            [sys.executable, worker, file, cd, str(int(budget)),
             str(int(crop))],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            cwd=HERE,                # the backend's exec dir is deleted after
            **detach_kwargs)
        logf.close()
        # stamp progress immediately: the worker needs a second or two to boot,
        # and a status poll in that window must see "running", not "dead"
        stamp = os.path.join(cd, "progress.json")
        with open(stamp + ".tmp", "w") as f:
            json.dump({"stage": "spawn", "pct": 0,
                       "detail": "starting converter", "done": False,
                       "error": None, "pid": child.pid, "elapsed": 0,
                       "ts": time.time()}, f)
        os.replace(stamp + ".tmp", stamp)
        time.sleep(0.3)
        return _state(cd, budget, crop)

    if action == "status":
        if direct:
            return {"ready": True}
        return _state(_cache_dir(file), budget, crop)

    return {"error": f"unknown action {action}"}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
