"""A broken supervisor import (e.g. pywin32 DLL load failure after a bad
upgrade) happens at fused_render.supervisor.__main__ module level, before
main() exists — it must show a MessageBox and exit 1 instead of dying
silently under pythonw. Needs a fresh interpreter: the failure is a module
load-time effect, and MessageBoxW must be stubbed before the module runs."""
import os
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="ctypes.windll is Windows-only")


def test_broken_supervisor_import_shows_messagebox_and_exits_1():
    code = (
        "import ctypes, sys\n"
        # None in sys.modules makes `import fused_render.supervisor.protocol`
        # raise ImportError — the same failure shape as a broken pywin32 DLL.
        "sys.modules['fused_render.supervisor.protocol'] = None\n"
        "calls = []\n"
        "ctypes.windll.user32.MessageBoxW = lambda *a: calls.append(a) or 1\n"
        "try:\n"
        "    import fused_render.supervisor.__main__\n"
        "except SystemExit as e:\n"
        "    ok = e.code == 1 and len(calls) == 1 and 'could not start' in calls[0][1]\n"
        "    print(ok)\n"
        "    sys.exit(0)\n"
        "print(False)\n"
    )
    with tempfile.TemporaryDirectory() as tmp_path:
        env = {**os.environ, "LOCALAPPDATA": tmp_path}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
        )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"
