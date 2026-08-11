"""Best-effort warm import, run once by the Windows installer (installer.iss
[Run]). Importing the bundled native extensions here makes the OS on-access
scan of them happen during install rather than freezing the first launch — the
base-startup half of docs/RCA_FIRST_LAUNCH_COLDSTART.md. server.app pulls the
server's whole import graph (what the child loads to serve); fused pulls the
compute engine's scientific stack. Best-effort: a failure here only costs the
warm-up, never the install."""
import importlib

for _name in ("fused_render.server.app", "fused"):
    try:
        importlib.import_module(_name)
    except Exception:
        pass
