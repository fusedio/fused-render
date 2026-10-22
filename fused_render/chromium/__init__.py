"""The Chromium (CEF) engine for the macOS app windows.

The desktop app's windows are `WKWebView`s (`mac_window.py`). With the
``chromium_windows_enabled`` preference on (the default), a NEW app window is
hosted by Chromium instead — the same NSWindow, main menu, Dock icon and
shortcuts, only the view inside changes. The dock, launcher, pinned popover
and recording pill stay on WebKit regardless.

The engine ships inside the DMG (D1415): `scripts/build_dmg.sh` builds it
from `native/` (CMake + the CEF binary distribution the manifest pins) and
seats it at `<App>.app/Contents/Frameworks/FusedChromium/`, with its
`engine.json` at `Contents/Resources/chromium-engine.json`. Nothing is
downloaded, pinned or published separately; there is no engine without the
app it was built with. A host outside the bundle (a dev checkout) points
``FUSED_RENDER_CHROMIUM_ENGINE_DIR`` at a `native/build.sh` output and gets
the same thing.

Layout:
* this module (OS-neutral, stdlib only): where the engine is, whether it is
  there, and the one rule for which engine a new window gets — unit-tested
  on every OS;
* `mac.py` (macOS only, PyObjC): loads the framework and makes the views
  `mac_window.py` puts in its windows;
* `native/`: the ObjC++ framework source and its build script.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import sys

logger = logging.getLogger(__name__)

#: What the engine is built against: `cef_version` and the CDN's SHA-1 of
#: the CEF tarball per arch (`native/build.sh` verifies its download against
#: it). The app's own version is the engine's version.
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.json")

#: Written by the build script beside the framework; `status` reads it so a
#: tree built for another arch or CEF is never dlopen'd.
ENGINE_INFO = "engine.json"

#: Wrapper framework bundle name inside the engine dir.
FRAMEWORK_NAME = "FusedChromium.framework"

#: Where build_dmg.sh seats the engine inside the app bundle. A plain
#: subdirectory of Contents/Frameworks holding the two frameworks and the
#: five helpers — `native/build.sh`'s own output layout, which is what
#: `FRChromiumEngine` opens. Strict codesign accepts nested bundles in such a
#: subdirectory but NOT plain files beside them (Developer ID sealing of the
#: app failed on a marker file: "code object is not signed at all",
#: 2026-10-22), so `engine.json` lives at `BUNDLED_INFO` instead.
BUNDLED_SUBDIR = os.path.join("Contents", "Frameworks", "FusedChromium")
BUNDLED_INFO = os.path.join("Contents", "Resources", "chromium-engine.json")

# `status()["state"]` is one of these.
UNSUPPORTED = "unsupported"    # not macOS
MISSING = "missing"            # macOS, but this host carries no engine
READY = "ready"                # on disk and describes this arch + CEF


def _read_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def cef_version() -> str:
    return str(_read_manifest()["cef_version"])


def host_arch(machine: str | None = None) -> str:
    """``arm64`` or ``x86_64`` for this machine (the manifest's keys)."""
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


def engine_dir() -> str | None:
    """Where this process's engine is: ``FUSED_RENDER_CHROMIUM_ENGINE_DIR``
    (a dev checkout at a `native/build.sh` output), else the app bundle's
    `BUNDLED_SUBDIR` (py2app's anatomy, `app.bundle_path`: `sys.executable`
    is `<App>.app/Contents/MacOS/python`), else None."""
    override = os.environ.get("FUSED_RENDER_CHROMIUM_ENGINE_DIR")
    if override:
        return override
    if getattr(sys, "frozen", None) != "macosx_app":
        return None
    contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.join(os.path.dirname(contents), BUNDLED_SUBDIR)


def framework_path() -> str | None:
    d = engine_dir()
    return os.path.join(d, FRAMEWORK_NAME) if d else None


def profile_dir() -> str:
    """Chromium's profile (cookies, local storage): ``<base home>/chromium/
    profile`` — the base (unnested) home, so every branch install shares it
    the way they share the WKWebView store."""
    from fused_render.shell.storage import base_home_dir

    return os.path.join(base_home_dir(), "chromium", "profile")


def info_path(d: str) -> str:
    """The `engine.json` describing the engine at ``d``: inside it (a build
    output) or, for the app bundle's copy, `BUNDLED_INFO` two levels up."""
    inside = os.path.join(d, ENGINE_INFO)
    if os.path.isfile(inside):
        return inside
    contents = os.path.dirname(os.path.dirname(d))
    return os.path.join(os.path.dirname(contents), BUNDLED_INFO)


def check_engine_info(path: str, arch: str | None = None, cef: str | None = None) -> dict:
    """`engine.json` must name this arch and the manifest's CEF: the build
    script writes it, so a tree from another build is caught before load."""
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"engine has no readable {ENGINE_INFO}: {exc}") from exc
    for key, want in (("cef_version", cef or cef_version()), ("arch", arch or host_arch())):
        if str(info.get(key)) != want:
            raise ValueError(f"engine {key} is {info.get(key)!r}, this build wants {want!r}")
    return info


def is_ready(arch: str | None = None) -> bool:
    d = engine_dir()
    if not d or not os.path.isdir(os.path.join(d, FRAMEWORK_NAME)):
        return False
    try:
        check_engine_info(info_path(d), arch)
    except ValueError as exc:
        logger.warning("chromium engine at %s unusable: %s", d, exc)
        return False
    return True


def status(arch: str | None = None, system: str | None = None) -> dict:
    """What the Preferences row shows and what `mac_window` consults. Pure
    reads."""
    if not platform_supported(system):
        return {"state": UNSUPPORTED, "cef_version": None, "path": None}
    d = engine_dir()
    ready = is_ready(arch)
    return {"state": READY if ready else MISSING,
            "cef_version": cef_version() if ready else None,
            "path": d}


def engine_for_new_window(pref_enabled: bool, state: str, loadable: bool) -> str:
    """Which engine the NEXT app window gets: ``"chromium"`` only when the
    preference is on, the engine is on disk AND the framework loaded into
    this process (`mac.available()`); anything short of that is
    ``"webkit"`` — never a blank window. Pure, so the rule is tested on
    every OS."""
    if pref_enabled and state == READY and loadable:
        return "chromium"
    return "webkit"
