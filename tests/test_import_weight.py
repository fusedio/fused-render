"""Does the base install (`pip install fused-render`, no extras) really boot, or
does it just happen to run in a venv that has the extras installed?

Checking `sys.modules` after importing `fused_render.cli`/`fused_render.server`
would answer the wrong question: it measures what THIS interpreter's venv
already carries, not what the import graph actually reaches. A dev/CI venv
with `[all]` installed would report every OPTIONAL name "not imported"
whether or not some module deep in the graph does `import duckdb` at load
time, as long as nothing else in the same process imported it first.

The only way to ask the real question is to make each optional import genuinely
fail, then prove the entry points still import cleanly. `sys.meta_path` gives
a finder first crack at every import; raising ModuleNotFoundError from
`find_spec` before the real finders run makes `import duckdb` fail exactly as
it would in an environment where duckdb is not installed at all: not a mock,
not a sys.modules trick, an actual failed import. Ported from the sibling
openfused repo's commit d68ddb45 ("rewrite import-weight guard to block
imports, not check sys.modules"), which hit precisely this false positive
(httpx's unconditional `import rich.console`, invisible unless something
actually tried to block rich).

The same block also backs the missing-extra replies: with the optional
packages made unimportable, the guarded routes must answer 503 with
`missing_extra` naming the extra, the embed page must still render, and
`serve` must refuse with the install line.

Run in a subprocess: `sys.meta_path` is process-global, and this repo's suite
runs under pytest-xdist, so mutating it in-process would leak across
whatever else that worker imports next.
"""
import json
import subprocess
import sys

# Every top-level import name that is NOT part of the base install
# (pyproject.toml `dependencies`), by the extra that brings it:
#   * [index]: duckdb, pyarrow, watchfiles
#   * [desktop]: zeroconf (and its ifaddr), PIL, dbus_fast, objc
#   * [hf]: huggingface_hub
#   * [cloud]: botocore, google (google-auth), requests
#   * [fused]: boto3, mcp, anthropic, cryptography (via pyjwt[crypto])
#   * [bundled]: numpy, pandas, openpyxl, pptx, msgpack, fpdf, drain3
# Plus `fused`: it IS base, but the server imports it lazily, on the first
# engine call, so `open` reaches its URL without paying for it. Blocking it
# here keeps that true.
#
# cryptography is also the regression test for `fused_render.update.common`,
# which reached it unguarded once; the child imports `fused_render.update.mac`
# and `.linux` to prove the guard from both platform branches.
OPTIONAL = frozenset({
    "duckdb", "pyarrow", "watchfiles",
    "zeroconf", "ifaddr", "PIL", "dbus_fast", "objc",
    "huggingface_hub",
    "botocore", "google", "requests",
    "boto3", "mcp", "anthropic", "cryptography",
    "numpy", "pandas", "openpyxl", "pptx", "msgpack", "fpdf", "drain3",
    "fused",
})

_BLOCKER = """
import sys

OPTIONAL = {optional!r}


class _BlockOptional:
    def find_spec(self, fullname, path, target=None):
        root = fullname.split(".", 1)[0]
        if root in OPTIONAL:
            raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
        return None


sys.meta_path.insert(0, _BlockOptional())
"""

_IMPORT_CHILD = _BLOCKER + """
try:
    import fused_render.cli  # noqa: F401
    import fused_render.server  # noqa: F401
    # Imported unconditionally, regardless of host platform: both are pure
    # Python, and the whole point is that the darwin-only update chain
    # (update/common.py -> update/mac.py) must be provable from a Linux CI
    # runner, not just from a machine that happens to be a Mac.
    import fused_render.update.mac  # noqa: F401
    import fused_render.update.linux  # noqa: F401
    import tempfile

    from fused_render.server import create_app

    create_app(tempfile.mkdtemp(), lean=True)
    create_app(tempfile.mkdtemp())
except ModuleNotFoundError as exc:
    print(f"IMPORT_FAILED:{{type(exc).__name__}}:{{exc}}")
    sys.exit(1)
else:
    print("IMPORT_OK")
"""

# Every route that needs an extra, hit on a lean app (what `open` serves) with
# the extras blocked. Each reply is printed as one JSON line for the parent.
_ROUTES_CHILD = _BLOCKER + """
import json
import os
import tempfile

from fastapi.testclient import TestClient

from fused_render.server import create_app

root = tempfile.mkdtemp()
with open(os.path.join(root, "index.html"), "w") as fh:
    fh.write("<!doctype html><title>t</title><p>hello</p>")
client = TestClient(create_app(root, lean=True))
H = {{"X-Fused": "1"}}
calls = [
    ("GET", "/api/index/status", None),
    ("GET", "/api/index/search?q=a", None),
    ("POST", "/api/index/query", {{}}),
    ("POST", "/api/index/scan", {{}}),
    ("GET", "/api/git-repos", None),
    ("POST", "/api/search/files", {{"q": "a"}}),
    ("POST", "/api/hf/login", None),
    ("GET", "/api/hf/auth", None),
]
for method, path, body in calls:
    r = client.request(method, path, headers=H, json=body)
    try:
        payload = r.json()
    except ValueError:
        payload = None
    print(json.dumps({{"path": path, "status": r.status_code, "body": payload}}))

r = client.get("/explorer/embed" + os.path.join(root, "index.html"))
print(json.dumps({{"path": "embed", "status": r.status_code, "body": None}}))
"""

_SERVE_CHILD = _BLOCKER + """
from fused_render import cli

try:
    cli._require_serve_extras()
except SystemExit as exc:
    print("SERVE_REFUSED:" + str(exc.code))
else:
    print("SERVE_ALLOWED")
"""


def _run_child(source: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", source.format(optional=OPTIONAL)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_entry_points_import_and_build_on_the_base_install():
    result = _run_child(_IMPORT_CHILD)
    assert "IMPORT_OK" in result.stdout, (
        "fused_render.cli / fused_render.server / create_app reached an optional "
        "package at import time, so a plain `pip install fused-render` (no extras) "
        f"cannot boot:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_routes_without_their_extra_answer_503_naming_it():
    result = _run_child(_ROUTES_CHILD)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    replies = {}
    for line in result.stdout.splitlines():
        if line.startswith("{"):
            row = json.loads(line)
            replies[row["path"]] = row
    expected = {
        "/api/index/status": "index",
        "/api/index/search?q=a": "index",
        "/api/index/query": "index",
        "/api/index/scan": "index",
        "/api/git-repos": "index",
        "/api/search/files": "index",
        "/api/hf/login": "hf",
    }
    for path, extra in expected.items():
        row = replies[path]
        assert row["status"] == 503, row
        assert row["body"]["missing_extra"] == extra, row
        assert f"pip install 'fused-render[{extra}]'" in row["body"]["error"], row
    # The Hub status read degrades to signed-out instead of erroring: it is a
    # GET the Preferences page polls, and "not signed in" is true.
    assert replies["/api/hf/auth"]["status"] == 200, replies["/api/hf/auth"]
    assert replies["/api/hf/auth"]["body"]["signedIn"] is False
    # The embed page is what `open` serves, and it needs no extra.
    assert replies["embed"]["status"] == 200, replies["embed"]


def test_serve_refuses_on_the_base_install_and_names_all():
    result = _run_child(_SERVE_CHILD)
    assert "SERVE_REFUSED:" in result.stdout, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}")
    message = result.stdout.split("SERVE_REFUSED:", 1)[1]
    assert "pip install 'fused-render[all]'" in message
    for extra in ("index", "desktop", "hf", "cloud", "fused"):
        assert f"[{extra}]" in message


def test_serve_starts_when_every_extra_is_installed(monkeypatch):
    from fused_render import cli, extras

    monkeypatch.setattr(extras, "missing_for_serve", lambda: [])
    cli._require_serve_extras()
