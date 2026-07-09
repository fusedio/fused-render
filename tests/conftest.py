"""Redirect the shell home dir to a throwaway tmp dir for the whole test run.

Importing fused_render.server / .executor now stages the core templates into
home_dir()/.core-templates on import (core_templates.ensure_core_templates).
This runs at collection time — before any fixture — so the redirect must be
set here at conftest import, ahead of the first test-module import, or the
copy would land in the real ~/.fused-render.

Only allocate + register cleanup when FUSED_RENDER_HOME is unset, so a caller
that set it (CI pointing at a real dir) still wins and we don't eagerly leak a
mkdtemp we never use. The dir we create is removed at process exit.
"""
import atexit
import os
import shutil
import tempfile

if "FUSED_RENDER_HOME" not in os.environ:
    _home = tempfile.mkdtemp(prefix="fused-render-tests-")
    os.environ["FUSED_RENDER_HOME"] = _home
    atexit.register(shutil.rmtree, _home, ignore_errors=True)
