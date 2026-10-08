"""scripts/_prune_stdlib_zip.py: strips setuptools' shim and the jaraco init
from py2app's frozen python312.zip, keeping what keyring needs."""
import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "_prune_stdlib_zip.py"
_spec = importlib.util.spec_from_file_location("_prune_stdlib_zip", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

REMOVED = [
    "_distutils_hack/__init__.pyc",
    "_distutils_hack/override.pyc",
    "setuptools/_vendor/x.pyc",
    "pkg_resources/__init__.pyc",
    "distutils-precedence.pth",
    "jaraco/__init__.pyc",
]
KEPT = {
    "jaraco/classes/__init__.pyc": b"classes",
    "jaraco/context/__init__.pyc": b"context",
    "encodings/__init__.pyc": b"enc",
    "setuptools_scm/x.pyc": b"scm",
    "encodings/": b"",
}


def _make(path, extra=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n in REMOVED:
            z.writestr(n, b"gone")
        for n, data in KEPT.items():
            z.writestr(n, data)
        for n, data in (extra or {}).items():
            z.writestr(n, data)


def test_prune_removes_and_keeps(tmp_path):
    zp = tmp_path / "python312.zip"
    _make(zp)
    assert mod.prune(str(zp)) == sorted(REMOVED)
    with zipfile.ZipFile(zp) as z:
        assert sorted(z.namelist()) == sorted(KEPT)
        for n, data in KEPT.items():
            assert z.read(n) == data


def test_second_run_is_noop(tmp_path):
    zp = tmp_path / "python312.zip"
    _make(zp)
    mod.prune(str(zp))
    os.utime(zp, (1_000_000, 1_000_000))
    assert mod.prune(str(zp)) == []
    assert os.stat(zp).st_mtime == 1_000_000


def test_pruned_zip_makes_jaraco_a_namespace(tmp_path):
    zp = tmp_path / "python312.zip"
    # real .py modules only: zipimport prefers a (fake) .pyc over the .py
    with zipfile.ZipFile(zp, "w") as z:
        # explicit dir entries, as in the real zip: zipimport only reports a
        # namespace portion for directories it can see in the index
        z.writestr("jaraco/", b"")
        z.writestr("jaraco/classes/", b"")
        z.writestr("jaraco/__init__.py", b"")
        z.writestr("jaraco/classes/__init__.py", b"X = 1\n")
    assert mod.prune(str(zp)) == ["jaraco/__init__.py"]
    site = tmp_path / "site"
    (site / "jaraco" / "text").mkdir(parents=True)
    (site / "jaraco" / "text" / "__init__.py").write_text("Y = 2\n")
    code = (
        "import sys; sys.path[:0]=[%r]; sys.path.append(%r);"
        "import jaraco.text, jaraco.classes;"
        "print(jaraco.text.__file__); print(jaraco.classes.__file__)"
    ) % (str(zp), str(site))
    out = subprocess.run(
        # -S: the test venv's own site-packages ships a real jaraco
        [sys.executable, "-I", "-S", "-B", "-c", code],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert out[0].startswith(str(site))
    assert out[1].startswith(str(zp))


def test_main_missing_zip(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope.zip")],
        capture_output=True, text=True,
    )
    assert r.returncode == 1 and "nope.zip" in r.stderr
