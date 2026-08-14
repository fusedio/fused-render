"""fused_render.supervisor.__main__ must apply the desktop env (branch
opt-out, state/cache dirs) before its own `protocol`/`core` import
statement runs, or an inherited FUSED_RENDER_BRANCH can latch into
fused_render._branch's process-lifetime cache before it's overridden.
Needs a fresh interpreter, since module-import ordering is a load-time
effect that a monkeypatched in-process test can't observe."""
import os
import subprocess
import sys
import tempfile

import pytest

pytest.importorskip("win32job")
pytest.importorskip("pystray")


def test_desktop_env_applied_before_supervisor_imports():
    code = (
        "import os\n"
        "import fused_render.supervisor.__main__\n"
        "from fused_render import _branch\n"
        "print(os.environ['FUSED_RENDER_BRANCH'] == '' and _branch.branch_ref() == '')\n"
    )
    with tempfile.TemporaryDirectory() as tmp_path:
        env = {**os.environ, "FUSED_RENDER_BRANCH": "feature-x", "LOCALAPPDATA": tmp_path}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
        )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"
