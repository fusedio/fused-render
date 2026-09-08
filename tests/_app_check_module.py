"""Loads skills/fused-render-app-doctor/ci/app_check.py by path.

The script lives under a skill directory rather than inside the
`fused_render` package (it is stdlib-only and ships verbatim into a user's
repo — see its own module docstring), so it is never importable as
`from fused_render import ...`. Both test_app_doctor.py and
test_app_doctor_housekeeping.py need the same module object, so the loader
lives here once rather than being duplicated in each file. Derived from this
file's own location so it resolves regardless of the working directory the
tests run from.
"""
import importlib.util
import os

_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir,
    "skills", "fused-render-app-doctor", "ci", "app_check.py",
)


def load():
    spec = importlib.util.spec_from_file_location("app_check", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app_doctor = load()
