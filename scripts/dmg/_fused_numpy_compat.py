"""Pick the numpy build that matches the running macOS (D1321).

The DMG ships TWO numpy builds of the same version:

  default  <pylib>/numpy                       macosx_14_0 wheel (Accelerate)
  compat   <pylib>/compat/macos13/site-packages/numpy
                                               macosx_11_0 wheel (OpenBLAS)

The default build cannot load below macOS 14 (minos 14.0). A `.pth` in the
bundled interpreter's site-packages (`import _fused_numpy_compat`) calls
`activate()` at every interpreter start, which puts the compat directory at the
FRONT of sys.path on macOS < 14 so `import numpy` resolves to the OpenBLAS build.
Everything else is untouched.

FUSED_RENDER_NUMPY_COMPAT overrides the automatic choice:
  1  force the compat (OpenBLAS) build, whatever the OS
  0  force the default (Accelerate) build, whatever the OS
  unset/other  decide from the macOS version

Must stay cheap and must never raise: a broken .pth would break every Python
start in the bundle. The compat directory is located relative to this file, so a
moved .app keeps working.
"""

import os
import sys

ENV_VAR = "FUSED_RENDER_NUMPY_COMPAT"
FIRST_ACCELERATE_MACOS = 14
_COMPAT_REL = ("compat", "macos13", "site-packages")


def compat_dir():
    """<pylib>/compat/macos13/site-packages, next to this file's site-packages."""
    pylib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pylib, *_COMPAT_REL)


def _macos_major():
    """Running macOS major version, or None when unknown.

    '' (not macOS / lookup failed) and '10.16' (SYSTEM_VERSION_COMPAT lies to
    old-SDK binaries) are both "unknown", never "old".
    """
    import platform

    release = platform.mac_ver()[0]
    if not release or release == "10.16":
        return None
    return int(release.split(".")[0])


def wants_compat():
    """True when the OpenBLAS build should shadow the default one."""
    override = os.environ.get(ENV_VAR)
    if override == "1":
        return True
    if override == "0":
        return False
    if sys.platform != "darwin":
        return False
    major = _macos_major()
    return major is not None and major < FIRST_ACCELERATE_MACOS


def activate():
    """Insert the compat dir at the front of sys.path if wanted. Never raises."""
    try:
        if not wants_compat():
            return False
        path = compat_dir()
        if not os.path.isdir(path):
            return False
        if path not in sys.path:
            sys.path.insert(0, path)
        return True
    except Exception:  # noqa: BLE001 - a .pth must never break startup
        return False


activate()
