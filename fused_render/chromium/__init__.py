"""The optional Chromium (CEF) engine for the macOS app windows.

The desktop app's windows are `WKWebView`s (`mac_window.py`). With the
experimental ``chromium_windows_enabled`` preference on, a NEW app window is
hosted by Chromium instead — the same NSWindow, main menu, Dock icon and
shortcuts, only the view inside changes. The dock, launcher, pinned popover
and recording pill stay on WebKit regardless.

The engine is NOT in the app bundle: it is fetched on demand, the way
usd-core is (D119), from a pinned release artifact named in `manifest.json`,
verified by size and SHA-256, and unpacked under the shell home dir. The app
ships at today's size, the engine updates out of band from app releases, and
a machine that never turns the preference on never pays for it.

Layout:
* this module (OS-neutral, stdlib only — the few fused_render imports are
  lazy and leaf modules: `shell.storage` for the home dir, `update.common`
  for the HTTPS opener, `jobs` for the Activity row): the manifest, the
  cache layout, the download/verify/unpack state machine (`status`,
  `ensure`, `install`), unit-tested on every OS with fakes;
* `mac.py` (macOS only, PyObjC): loads the unpacked framework and makes the
  views `mac_window.py` puts in its windows;
* `native/`: the ObjC++ framework source and its build script (CMake + the
  CEF binary distribution) — what the artifact is built from.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.json")

# The unpacked engine is complete only once this file exists (written last,
# after the hash check and the unpack): a crash mid-unpack leaves a dir with
# no marker, which `status` reports as missing and `ensure` rebuilds.
READY_MARKER = ".ready"
# Written by the build script next to the framework; checked at load time so
# an artifact built for another arch or wrapper version is never dlopen'd.
ENGINE_INFO = "engine.json"

#: Wrapper framework bundle name inside the unpacked dir.
FRAMEWORK_NAME = "FusedChromium.framework"

# The Activity row (SPEC §36, D1421). `status()` is what the Preferences row
# polls, but that row is only on screen while Preferences is — turn the
# preference on, navigate away, and a ~150 MB download was running with
# nothing anywhere to say so. Every tick of the install is therefore also
# mirrored into the job registry (`fused_render.jobs`), the one surface the
# status bar's activity chip and the Notifications panel draw from, the way
# the app updater (`update/_manager.py`) mirrors its own DMG download. The
# id is per engine+arch, so a retry after a failure inherits the failed
# attempt's row instead of opening a second one; the `sys:` prefix makes it
# server-owned (a page can read it, never write it). Reporting is
# best-effort and never load-bearing: a registry error is logged once per
# call at debug and the install carries on.
JOB_ID_PREFIX = "sys:chromium-engine:"
JOB_TITLE = "Chromium engine"
# The phase word goes in `detail`, not `message`: the chip's type label
# (`jobTypeLabel`, jobs.ts) reads the leading "-ing" verb off `detail`.
JOB_PHASE_DETAIL = {"download": "Downloading", "verify": "Verifying", "unpack": "Unpacking"}
JOB_DONE_MESSAGE = "Ready. New windows use Chromium; open windows keep their engine."

# Download state names. `status()["state"]` is one of these.
UNSUPPORTED = "unsupported"    # not macOS (or an arch the manifest lacks)
UNPUBLISHED = "unpublished"    # manifest has no hash for this arch yet
MISSING = "missing"            # supported, published, not on disk
DOWNLOADING = "downloading"    # a download/unpack is in flight
READY = "ready"                # unpacked and verified
FAILED = "failed"              # last attempt failed; `error` says why


def _read_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def host_arch(machine: str | None = None) -> str:
    """The manifest's arch key for this machine: ``arm64`` or ``x86_64``."""
    m = (machine if machine is not None else platform.machine()).lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m


def platform_supported(system: str | None = None) -> bool:
    """Only macOS hosts the engine: Linux windows are WebKitGTK from the
    distro (D1314), iOS allows WebKit alone, Windows has no native windows."""
    return (system or sys.platform) == "darwin"


@dataclass(frozen=True)
class Artifact:
    engine_version: str
    cef_version: str
    arch: str
    url: str
    sha256: str | None
    size: int | None

    @property
    def published(self) -> bool:
        return bool(self.sha256) and bool(self.size)


def artifact(arch: str | None = None, manifest: dict | None = None) -> Artifact | None:
    """The pinned artifact for ``arch`` (default: this machine), or None when
    the manifest has no entry for it."""
    m = manifest if manifest is not None else _read_manifest()
    arch = arch or host_arch()
    entry = (m.get("artifacts") or {}).get(arch)
    if not entry:
        return None
    base = str(m["base_url"]).rstrip("/") + "/"
    return Artifact(
        engine_version=str(m["engine_version"]),
        cef_version=str(m["cef_version"]),
        arch=arch,
        url=base + str(entry["file"]),
        sha256=(str(entry["sha256"]).lower() if entry.get("sha256") else None),
        size=(int(entry["size"]) if entry.get("size") else None),
    )


def engines_root() -> str:
    """Where unpacked engines live: ``<base home>/chromium/``. The base
    (unnested) home, not the branch dir: one engine serves every branch
    install on the machine, the artifact is ~150 MB."""
    from fused_render.shell.storage import base_home_dir

    return os.path.join(base_home_dir(), "chromium")


def engine_dir(art: Artifact) -> str:
    return os.path.join(engines_root(), f"engine-{art.engine_version}-{art.arch}")


def framework_path(art: Artifact) -> str:
    return os.path.join(engine_dir(art), FRAMEWORK_NAME)


def is_ready(art: Artifact) -> bool:
    d = engine_dir(art)
    return (os.path.isfile(os.path.join(d, READY_MARKER))
            and os.path.isdir(os.path.join(d, FRAMEWORK_NAME)))


def job_id(art: Artifact) -> str:
    """The registry id of ``art``'s install row — see `JOB_ID_PREFIX`."""
    return f"{JOB_ID_PREFIX}{art.engine_version}-{art.arch}"


def _report_job(art: Artifact, **fields) -> None:
    """Mirror one tick of ``art``'s install into the job registry. Never
    raises, never holds `_state.lock` (the registry's own lock is taken
    inside; `install` is the one caller that reports with `_state.lock`
    held, so the order is always state lock → registry lock, never the
    reverse — `jobs.py` is a leaf and calls nothing here back)."""
    try:
        from fused_render import jobs

        jobs.upsert({"id": job_id(art), "title": JOB_TITLE, "kind": "download",
                     "unit": "bytes", "origin": "Preferences", **fields},
                    page="/preferences", server=True)
    except Exception:  # noqa: BLE001 — reporting must never break the install
        logger.debug("could not report the chromium engine job", exc_info=True)


def _job_fields(art: Artifact, progress: float, phase: str) -> dict | None:
    """The registry fields one `_set_progress` tick maps to, or None for the
    tick that needs no report of its own (the final "ready" one — the
    terminal row is `_run_install`'s, which also knows whether the prune
    and the marker write after it went through)."""
    if phase == "ready":
        return None
    size = float(art.size or 0)
    if phase == "download":
        # `_install_sync` scales the fetch's own fraction by 0.9 to leave the
        # last tenth of the bar for verify + unpack; undo that for the bytes.
        done = min(1.0, progress / 0.9) * size
    else:
        done = size  # the bytes are in; the verb says what is left
    return {"state": "running", "done": done, "total": size,
            "detail": JOB_PHASE_DETAIL.get(phase, ""), "message": ""}


# ---- the download state machine --------------------------------------------

class _State:
    """Module-wide download state, shared by the server thread that reports
    it and the worker thread that advances it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.progress = 0.0     # 0..1 of the download, then unpack
        self.phase = ""         # "download" | "verify" | "unpack"
        self.error: str | None = None
        self.failed_for: str | None = None  # engine dir the error belongs to
        self.art: Artifact | None = None    # what the running install fetches


_state = _State()


def status(arch: str | None = None, system: str | None = None) -> dict:
    """What the Preferences page shows and what `mac_window` consults. Pure
    reads: never starts anything."""
    if not platform_supported(system):
        return {"state": UNSUPPORTED, "progress": 0.0, "error": None,
                "engine_version": None, "cef_version": None, "path": None}
    art = artifact(arch)
    base = {
        "engine_version": art.engine_version if art else None,
        "cef_version": art.cef_version if art else None,
        "path": engine_dir(art) if art else None,
        "progress": 0.0,
        "error": None,
    }
    if art is None or not art.published:
        return {**base, "state": UNPUBLISHED}
    with _state.lock:
        running = _state.thread is not None and _state.thread.is_alive()
        if running:
            return {**base, "state": DOWNLOADING, "progress": _state.progress,
                    "phase": _state.phase}
        if is_ready(art):
            return {**base, "state": READY, "progress": 1.0}
        if _state.error and _state.failed_for == engine_dir(art):
            return {**base, "state": FAILED, "error": _state.error}
    return {**base, "state": MISSING}


def install(arch: str | None = None, fetch=None) -> dict:
    """Start the download in a daemon thread unless one is running or the
    engine is already there. Returns the status after the call. ``fetch``
    (tests) replaces the HTTP download: ``fetch(url, dest_path, progress_cb)``."""
    st = status(arch)
    if st["state"] in (UNSUPPORTED, UNPUBLISHED, READY, DOWNLOADING):
        return st
    art = artifact(arch)
    assert art is not None  # status() said published
    with _state.lock:
        if _state.thread is not None and _state.thread.is_alive():
            return status(arch)
        _state.progress, _state.phase, _state.error = 0.0, "download", None
        _state.failed_for = None
        _state.art = art
        # Opened here, before the thread starts, so the row exists by the
        # time the request that turned the preference on returns and the
        # activity chip's very next poll draws it. No ✕: the state machine
        # has no cancel (a half-fetched artifact is simply rebuilt next time).
        _report_job(art, state="running", done=0.0, total=float(art.size or 0),
                    detail=JOB_PHASE_DETAIL["download"], message="",
                    cancellable=False)
        t = threading.Thread(target=_run_install, args=(art, fetch),
                             name="chromium-engine-install", daemon=True)
        _state.thread = t
        t.start()
    return status(arch)


def ensure(arch: str | None = None, fetch=None, timeout: float | None = None) -> bool:
    """Synchronous `install`: start if needed, wait (bounded by ``timeout``),
    report readiness. For callers that want the engine NOW and can block —
    tests and the build smoke test; the app itself polls `status`."""
    install(arch, fetch)
    t = _state.thread
    if t is not None and t is not threading.current_thread():
        t.join(timeout)
    art = artifact(arch)
    return art is not None and is_ready(art)


def _set_progress(value: float, phase: str | None = None) -> None:
    with _state.lock:
        _state.progress = max(0.0, min(1.0, float(value)))
        if phase is not None:
            _state.phase = phase
        art, progress, phase = _state.art, _state.progress, _state.phase
    # One report per fetch chunk (1 MB, `_http_fetch`) — the updater's own
    # cadence for the same kind of row. Outside the lock: see `_report_job`.
    if art is not None:
        fields = _job_fields(art, progress, phase)
        if fields is not None:
            _report_job(art, **fields)


def _run_install(art: Artifact, fetch) -> None:
    dest = engine_dir(art)
    try:
        _install_sync(art, fetch or _http_fetch)
        logger.info("chromium engine %s (%s) ready at %s",
                    art.engine_version, art.cef_version, dest)
    except Exception as exc:  # noqa: BLE001 — reported through status()
        logger.warning("chromium engine install failed: %s", exc)
        error = str(exc) or exc.__class__.__name__
        with _state.lock:
            _state.error = error
            _state.failed_for = dest
        shutil.rmtree(dest, ignore_errors=True)
        # The failure is news nothing else on screen carries once the user
        # has left Preferences: an `error` row sticks until dismissed.
        _report_job(art, state="error", detail="", message=error)
        return
    # The terminal row, unlike the updater's (which forgets its row because
    # the restart dialog already announces the finish): "ready" is the one
    # moment the user waiting to open a Chromium window wants to hear about,
    # and the Preferences row that also says it is only there if they stayed.
    _report_job(art, state="done", done=float(art.size or 0),
                total=float(art.size or 0), detail="", message=JOB_DONE_MESSAGE)
    # Outside the handler above: a prune failure must never delete the
    # engine that just installed, nor report the install as failed.
    try:
        prune_others(art)
    except Exception:  # noqa: BLE001
        logger.debug("chromium prune failed", exc_info=True)


def prune_others(art: Artifact | None = None) -> list[str]:
    """Delete the unpacked engines OLDER than ``art``'s (the one the running
    app's manifest names): each is ~125 MB and no newer build names them.
    Newer ones stay — an engine prefetched for a pending app update
    (`prefetch_for_bundle`) must survive the relaunch that will use it.
    `engines_root` is shared by every branch install on the machine; a dev
    process on an older branch that loses its engine fetches it again at
    its next start (app.py's settle thread) and shows WebKit until then."""
    art = art or artifact()
    if art is None:
        return []
    try:
        current = int(art.engine_version)
    except ValueError:
        return []
    root = engines_root()
    gone: list[str] = []
    try:
        names = os.listdir(root)
    except OSError:
        return gone
    for name in names:
        m = re.fullmatch(r"engine-(\d+)-" + re.escape(art.arch), name)
        if m and int(m.group(1)) < current:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            gone.append(name)
    if gone:
        logger.info("chromium: removed old engines %s", ", ".join(sorted(gone)))
    return gone


def prefetch_for_bundle(app_dir: str, arch: str | None = None, fetch=None) -> bool:
    """An app update is about to be installed: fetch the engine ITS manifest
    pins, so the restart finds it on disk instead of falling back to WebKit
    until the download the preference would start later finishes. Reads
    ``<app>/Contents/Resources/lib/python3.*/fused_render/chromium/manifest.json``
    (the DMG layout). Synchronous, on the caller's thread; returns whether
    the engine is on disk afterwards. Never prunes: the running app's engine
    stays in use until the relaunch."""
    import glob

    pattern = os.path.join(app_dir, "Contents", "Resources", "lib", "python3.*",
                           "fused_render", "chromium", "manifest.json")
    paths = sorted(glob.glob(pattern))
    if not paths:
        logger.info("chromium prefetch: no manifest under %s", app_dir)
        return False
    with open(paths[0], encoding="utf-8") as f:
        manifest = json.load(f)
    art = artifact(arch, manifest=manifest)
    if art is None or not art.published:
        logger.info("chromium prefetch: engine %s not published", manifest.get("engine_version"))
        return False
    if is_ready(art):
        return True
    current = artifact(arch)
    if current is not None and engine_dir(current) == engine_dir(art):
        # The update pins the engine this app already has — or is installing
        # right now: join that download (bounded) rather than swap the app
        # while it is still in flight, which would relaunch on WebKit.
        return ensure(arch, fetch=fetch, timeout=600)
    logger.info("chromium prefetch: engine %s (%s) for the pending update",
                art.engine_version, art.cef_version)
    _install_sync(art, fetch or _http_fetch, progress=lambda *_: None)
    return is_ready(art)


def _install_sync(art: Artifact, fetch, progress=None) -> None:
    progress = progress or _set_progress
    root = engines_root()
    os.makedirs(root, exist_ok=True)
    dest = engine_dir(art)
    # A half-unpacked dir from an earlier crash is never trusted: start over.
    shutil.rmtree(dest, ignore_errors=True)
    tmpdir = tempfile.mkdtemp(prefix=".download-", dir=root)
    try:
        zip_path = os.path.join(tmpdir, "engine.zip")
        progress(0.0, "download")
        fetch(art.url, zip_path, lambda frac: progress(frac * 0.9, "download"))
        progress(0.9, "verify")
        verify_artifact(zip_path, art)
        progress(0.92, "unpack")
        staging = os.path.join(tmpdir, "unpacked")
        safe_extract(zip_path, staging)
        inner = _locate_framework_root(staging)
        check_engine_info(os.path.join(inner, ENGINE_INFO), art)
        os.replace(inner, dest)
        with open(os.path.join(dest, READY_MARKER), "w", encoding="utf-8") as f:
            f.write(art.cef_version + "\n")
        progress(1.0, "ready")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def verify_artifact(path: str, art: Artifact) -> None:
    """Size first (cheap, catches a truncated transfer), then SHA-256 against
    the manifest. Raises ValueError on a mismatch."""
    actual_size = os.path.getsize(path)
    if art.size is not None and actual_size != art.size:
        raise ValueError(f"engine artifact size {actual_size} != expected {art.size}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if art.sha256 and h.hexdigest() != art.sha256:
        raise ValueError("engine artifact SHA-256 mismatch")


def safe_extract(zip_path: str, dest: str) -> None:
    """Unpack ``zip_path`` into ``dest`` refusing any member that would land
    outside it (absolute paths, ``..``), and restoring the executable bit
    from the member's external attributes — a framework whose binaries are
    not executable loads nothing. Symlinks (CEF frameworks are full of
    `Versions/Current` links) are recreated as symlinks, never followed."""
    dest = os.path.realpath(dest)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or os.path.isabs(name):
                raise ValueError(f"unsafe member path: {name}")
            target = os.path.realpath(os.path.join(dest, name))
            if target != dest and not target.startswith(dest + os.sep):
                raise ValueError(f"unsafe member path: {name}")
            mode = (info.external_attr >> 16) & 0o7777
            is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if is_symlink:
                link = zf.read(info).decode("utf-8")
                if os.path.isabs(link) or ".." in link.split("/"):
                    raise ValueError(f"unsafe symlink in archive: {name} -> {link}")
                if os.path.lexists(target):
                    os.remove(target)
                os.symlink(link, target)
                continue
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            if mode:
                os.chmod(target, mode)


def _locate_framework_root(staging: str) -> str:
    """The artifact is a zip of one top-level dir holding the framework, the
    helpers and `engine.json`; tolerate the framework at the top level too."""
    if os.path.isdir(os.path.join(staging, FRAMEWORK_NAME)):
        return staging
    entries = [e for e in os.listdir(staging) if not e.startswith("__MACOSX")]
    for e in entries:
        p = os.path.join(staging, e)
        if os.path.isdir(p) and os.path.isdir(os.path.join(p, FRAMEWORK_NAME)):
            return p
    raise ValueError(f"{FRAMEWORK_NAME} not found in the engine artifact")


def check_engine_info(path: str, art: Artifact) -> dict:
    """`engine.json` inside the artifact must agree with the manifest: the
    build script writes it, so a mislabelled upload is caught before the
    marker is written."""
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"engine artifact has no readable {ENGINE_INFO}: {exc}") from exc
    for key, want in (("engine_version", art.engine_version),
                      ("cef_version", art.cef_version), ("arch", art.arch)):
        if str(info.get(key)) != want:
            raise ValueError(f"engine artifact {key} is {info.get(key)!r}, manifest says {want!r}")
    return info


def _http_fetch(url: str, dest: str, progress_cb) -> None:
    """Stream ``url`` to ``dest`` reporting fraction done (by Content-Length;
    without one the bar sits at 0 until the file lands)."""
    if not url.startswith("https://"):
        raise ValueError("engine artifact URL must be https")
    # The updater's opener: refuses a redirect that leaves HTTPS.
    from fused_render.update.common import _opener

    req = urllib.request.Request(url, headers={"User-Agent": "fused-render"})
    with _opener.open(req, timeout=60) as r, open(dest, "wb") as f:
        total = r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else 0
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress_cb(done / total)
    progress_cb(1.0)


def engine_for_new_window(pref_enabled: bool, state: str, loadable: bool) -> str:
    """Which engine the NEXT app window gets: ``"chromium"`` only when the
    preference is on, the engine is on disk and verified, AND the framework
    loaded into this process (`mac.available()`); anything short of that is
    ``"webkit"`` — never a blank window waiting on a download. Pure, so the
    rule is tested on every OS."""
    if pref_enabled and state == READY and loadable:
        return "chromium"
    return "webkit"


def remove(arch: str | None = None) -> bool:
    """Delete the unpacked engine (Preferences → Remove). False when there
    was none or a download is running."""
    art = artifact(arch)
    if art is None:
        return False
    with _state.lock:
        if _state.thread is not None and _state.thread.is_alive():
            return False
    d = engine_dir(art)
    if not os.path.isdir(d):
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True
