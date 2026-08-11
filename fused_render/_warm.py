"""Warm import run once by the Windows installer ([Run]): scanning the bundled
native extensions here moves the OS on-access scan to install, off first launch."""
import importlib

for _name in ("fused_render.server.app", "fused"):
    try:
        importlib.import_module(_name)
    except Exception:
        pass
