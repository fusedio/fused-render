"""Do the core entry points actually need `[bundled]`, or do they just happen
to have it installed? (LEAN_WHEEL_SPEC.md item 4.)

Checking `sys.modules` after importing `fused_render.cli`/`fused_render.server`
would answer the wrong question — it measures what THIS interpreter's venv
already carries, not what the import graph actually reaches. A dev/CI venv
with `[bundled]` installed would report every HEAVY name "not imported"
whether or not some module deep in the graph does `import pandas` at load
time, as long as nothing else in the same process imported it first.

The only way to ask the real question is to make each HEAVY import genuinely
fail, then prove the entry points still import cleanly. `sys.meta_path` gives
a finder first crack at every import; raising ModuleNotFoundError from
`find_spec` before the real finders run makes `import pandas` fail exactly as
it would in an environment where pandas is not installed at all — not a mock,
not a sys.modules trick, an actual failed import. Ported from the sibling
openfused repo's commit d68ddb45 ("rewrite import-weight guard to block
imports, not check sys.modules"), which hit precisely this false-positive
(httpx's unconditional `import rich.console`, invisible unless something
actually tried to block rich).

Run in a subprocess: `sys.meta_path` is process-global, and this repo's suite
runs under pytest-xdist, so mutating it in-process would leak across
whatever else that worker imports next.
"""
import subprocess
import sys

# Every one of these is `[bundled]`-only today (pyproject.toml's `bundled`
# extra) — not a core dependency on any platform this test runs on. A plain
# `pip install fused-render` (no extras) must still boot the server and the
# CLI without any of them.
#
# Deliberately excluded:
#   * pillow — core on sys_platform in {"win32", "linux"} (pyproject.toml,
#     capture-backend comment), so blocking it on Linux CI would not be
#     testing a lean install at all; it is not exclusively a `[bundled]` cost
#     the way the rest of this set is.
#   * numpy/pandas — kept IN the set even though `duckdb`/`pyarrow` (core)
#     pull in bits of numpy's C API; if the entry points genuinely need numpy
#     transitively, this test should fail and say so, not paper over it.
#   * google-auth / botocore — collapsed to their real top-level import
#     names (`google`, `botocore`) rather than the PyPI project names.
#   * mcp / fused — both `[bundled]`-only and by far the heaviest transitive
#     pulls (mcp brings in pydantic/httpx-sse/etc.; fused brings in its own
#     large dependency tree); verified locally that blocking them still
#     yields IMPORT_OK.
HEAVY = frozenset({
    "numpy",
    "pandas",
    "requests",
    "openpyxl",
    "pptx",
    "msgpack",
    "fpdf",
    "drain3",
    "botocore",
    "google",
    "mcp",
    "fused",
})

_CHILD_SOURCE = """
import sys

HEAVY = {heavy!r}


class _BlockHeavy:
    def find_spec(self, fullname, path, target=None):
        root = fullname.split(".", 1)[0]
        if root in HEAVY:
            raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
        return None


sys.meta_path.insert(0, _BlockHeavy())

try:
    import fused_render.cli  # noqa: F401
    import fused_render.server  # noqa: F401
except ModuleNotFoundError as exc:
    print(f"IMPORT_FAILED:{{type(exc).__name__}}:{{exc}}")
    sys.exit(1)
else:
    print("IMPORT_OK")
"""


def test_entry_points_import_without_bundled_only_packages():
    code = _CHILD_SOURCE.format(heavy=HEAVY)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "IMPORT_OK" in result.stdout, (
        "fused_render.cli / fused_render.server reached one of the "
        "[bundled]-only packages at import time, so a plain "
        "`pip install fused-render` (no extras) cannot boot:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
