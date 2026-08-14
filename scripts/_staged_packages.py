"""Print setup_py2app.STAGED_PACKAGES, space-separated, for build_dmg.sh.

A separate file rather than a heredoc inside build_dmg.sh: one declaration of
the list (in setup_py2app.py), read the same way by the build and by
tests/test_bundle_contents.py.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "_setup_py2app_staged", os.path.join(HERE, "setup_py2app.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.stdout.write(" ".join(module.STAGED_PACKAGES))
