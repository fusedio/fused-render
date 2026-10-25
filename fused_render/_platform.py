"""The server's handle on `templates/shared/platutil.py`.

That file is the one copy of the platform seams (file locks, tree kill, detach
flags); it sits under `templates/shared` because the template backends cannot
import `fused_render`. It is loaded here by path, once, and re-exported."""
from __future__ import annotations

import importlib.util
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "templates", "shared", "platutil.py")
_spec = importlib.util.spec_from_file_location("fused_render_platutil", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

IS_WINDOWS = _mod.IS_WINDOWS
flock = _mod.flock
funlock = _mod.funlock
kill_tree = _mod.kill_tree
SIGKILL = _mod.SIGKILL
detach_kwargs = _mod.detach_kwargs
probe_pid = _mod.probe_pid
euid = _mod.euid
venv_python = _mod.venv_python
relpath_within = _mod.relpath_within
is_within = _mod.is_within
