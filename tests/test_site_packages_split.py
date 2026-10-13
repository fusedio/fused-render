"""Bundled third-party packages live in a real site-packages that app venvs exclude.

scripts/_split_site_packages.py moves them out of py2app's stdlib dirs,
scripts/_site_isolation_check.py proves a venv can neither see nor be shadowed
by them, and scripts/_fused_render_site.py keeps them ahead of a user site.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


split_mod = _load("_split_site_packages")
check_mod = _load("_site_isolation_check")
hook_mod = _load("_fused_render_site")

STDLIB = {"os", "json", "encodings", "site", "_json"}


def _write(path, data=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _bundle(tmp_path):
    """A py2app-shaped lib/python3.12 + python312.zip mixing stdlib and third-party."""
    pylib = tmp_path / "lib" / "python3.12"
    for rel in ("json/__init__.py", "encodings/__init__.py", "site.pyc", "config-3.12-darwin/Makefile",
                "numpy/__init__.py", "numpy/_core/multiarray.py", "typing_extensions.py",
                "fused_render/__init__.py", "fused_render/ai/runners/mlx/worker.py",
                "lib-dynload/_json.so", "lib-dynload/_duckdb.so", "lib-dynload/_testcapi.so"):
        _write(pylib / rel, rel.encode())
    zp = tmp_path / "lib" / "python312.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for name in ("os.pyc", "encodings/aliases.pyc", "_sysconfigdata__darwin_darwin.pyc", "test/__init__.pyc", "./",
                     "six.pyc", "jaraco/context/__init__.pyc", "jaraco/context/py.typed",
                     "huggingface_hub-1.33.0.dist-info/METADATA",
                     "numpy/_core/include/__init__.pyc", "fused_render/ai/runners/__init__.pyc",
                     "typing_extensions.pyc", "jaraco/__pycache__/x.cpython-312.pyc"):
            z.writestr(name, b"" if name.endswith("/") else ("zip:" + name).encode())
    return pylib, zp


def test_split_moves_every_third_party_entry_and_leaves_the_stdlib(tmp_path):
    pylib, zp = _bundle(tmp_path)
    report = split_mod.split(str(pylib), str(zp), STDLIB)
    site = pylib / "site-packages"

    assert sorted(report["dirs"]) == ["fused_render", "numpy", "typing_extensions.py"]
    assert report["dynload"] == ["_duckdb.so"]
    assert sorted(os.listdir(pylib)) == ["config-3.12-darwin", "encodings", "json", "lib-dynload",
                                         "site-packages", "site.pyc"]
    assert sorted(os.listdir(pylib / "lib-dynload")) == ["_json.so", "_testcapi.so"]
    with zipfile.ZipFile(zp) as z:
        assert sorted(z.namelist()) == sorted(["os.pyc", "encodings/aliases.pyc", "_sysconfigdata__darwin_darwin.pyc",
                                               "test/__init__.pyc", "./"])

    assert (site / "six.pyc").read_bytes() == b"zip:six.pyc"
    assert (site / "jaraco/context/py.typed").exists()
    assert (site / "huggingface_hub-1.33.0.dist-info/METADATA").exists()
    assert (site / "_duckdb.so").read_bytes() == b"lib-dynload/_duckdb.so"
    # The whole-copied package wins: zip fragments beside it were never imported.
    assert (site / "typing_extensions.py").read_bytes() == b"typing_extensions.py"
    assert not (site / "typing_extensions.pyc").exists()
    assert not (site / "numpy/_core/include").exists()
    assert not (site / "fused_render/ai/runners/__init__.pyc").exists()
    assert not (site / "jaraco/__pycache__").exists()
    assert report["extracted"] == 4 and report["dropped"] == 4


def test_split_is_idempotent(tmp_path):
    pylib, zp = _bundle(tmp_path)
    split_mod.split(str(pylib), str(zp), STDLIB)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    report = split_mod.split(str(pylib), str(zp), STDLIB)
    assert report == {"dirs": [], "dynload": [], "extracted": 0, "dropped": 0}
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_split_refuses_to_pick_between_two_copies(tmp_path):
    pylib, zp = _bundle(tmp_path)
    _write(pylib / "site-packages" / "numpy" / "__init__.py")
    with pytest.raises(RuntimeError, match="refusing"):
        split_mod.split(str(pylib), str(zp), STDLIB)


def test_stdlib_names_cover_test_and_sysconfig_modules(tmp_path):
    _write(tmp_path / "lib-dynload" / "_testcapi.cpython-312-darwin.so")
    _write(tmp_path / "_sysconfigdata__darwin_darwin.py")
    names = split_mod.stdlib_names(str(tmp_path))
    assert {"_testcapi", "_sysconfigdata__darwin_darwin", "json"} <= names
    assert not split_mod.third_party("_testinternalcapi.so", set())
    assert split_mod.third_party("_cffi_backend.so", names)
    assert split_mod.third_party("six-1.17.0.dist-info", names)


@pytest.mark.parametrize("path,expected", [
    (["/z", "/user", "/user/extra", "/bundle"], ["/z", "/bundle", "/user", "/user/extra"]),
    (["/z", "/bundle", "/user"], ["/z", "/bundle", "/user"]),
    (["/z", "/bundle"], ["/z", "/bundle"]),
])
def test_bundle_site_goes_before_the_user_site(path, expected):
    assert hook_mod.reorder(path, "/user", "/bundle") == expected
    assert hook_mod.reorder(path, None, "/bundle") == path


def test_the_installed_pth_reorders_during_site_processing(tmp_path):
    user, hooked = tmp_path / "user", tmp_path / "pylib" / "site-packages"
    user.mkdir()
    hooked.mkdir(parents=True)
    split_mod.install_site_hook(str(tmp_path / "pylib"))
    code = ("import site, sys; site.USER_SITE = sys.argv[1]; sys.path.append(sys.argv[1]); "
            "site.addsitedir(sys.argv[2]); print(sys.path.index(sys.argv[2]) < sys.path.index(sys.argv[1]))")
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(user), str(hooked)],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True", out.stderr


def _venv(tmp_path):
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(env_dir)
    return env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _fake_bundle_site(tmp_path):
    site = tmp_path / "bundle-site-packages"
    _write(site / "zz_bundled_pkg" / "__init__.py", b"WHO = 'bundle'")
    _write(site / "zz_bundled_mod.py", b"WHO = 'bundle'")
    _write(site / "zz_bundled_pkg-1.33.0.dist-info" / "METADATA",
           b"Metadata-Version: 2.1\nName: zz-bundled-pkg\nVersion: 1.33.0\n")
    return site


def _run_check(python, site, **env):
    full = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    full.update(env)
    return subprocess.run([str(python), "-B", str(SCRIPTS / "_site_isolation_check.py"), str(site)],
                          capture_output=True, text=True, env=full)


def test_the_isolation_check_passes_for_an_isolated_venv_and_leaves_it_untouched(tmp_path):
    python, site = _venv(tmp_path), _fake_bundle_site(tmp_path)
    purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                             capture_output=True, text=True, check=True).stdout.strip()
    before = sorted(os.listdir(purelib))
    proc = _run_check(python, site)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("SITE-ISOLATION ok 2 names, 1 distributions")
    assert sorted(os.listdir(purelib)) == before


def test_the_isolation_check_fails_when_the_bundle_precedes_the_venv(tmp_path):
    python, site = _venv(tmp_path), _fake_bundle_site(tmp_path)
    proc = _run_check(python, site, PYTHONPATH=str(site))
    assert proc.returncode == 1
    out = proc.stdout
    assert "bundle site-packages is on the venv's sys.path" in out
    assert re.search(r"venv-installed packages shadowed by the bundle: zz_bundled_mod .*zz_bundled_pkg", out)
    assert "venv-installed distributions shadowed: zz-bundled-pkg==1.33.0" in out


def test_the_isolation_check_refuses_a_non_venv(tmp_path):
    proc = subprocess.run([sys._base_executable, "-I", "-B", str(SCRIPTS / "_site_isolation_check.py"),
                           str(_fake_bundle_site(tmp_path))], capture_output=True, text=True)
    assert proc.returncode == 1 and "not running inside a venv" in proc.stdout


@pytest.mark.skipif(sys.version_info < (3, 11), reason="-P (safe_path) is 3.11+")
def test_child_and_engine_worker_import_their_sibling_without_their_dir_on_sys_path(tmp_path):
    # -P is what the Windows python312._pth implies: the script's own dir is not sys.path[0].
    probe = tmp_path / "probe.py"
    probe.write_text("def main(n: int = 0) -> dict:\n    return {'n': n + 1}\n")
    proc = subprocess.run([sys.executable, "-P", "-B", str(REPO / "fused_render" / "_child.py")],
                          input=json.dumps({"path": str(probe), "params": {"n": "41"}}),
                          capture_output=True, text=True, timeout=60)
    assert json.loads(proc.stdout.strip().splitlines()[-1])["result"] == {"n": 42}, proc.stderr
    proc = subprocess.run([sys.executable, "-P", "-B", str(REPO / "fused_render" / "engine_worker.py"), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "--module" in proc.stdout, proc.stderr


def test_the_build_scripts_split_and_check_isolation():
    dmg = (SCRIPTS / "build_dmg.sh").read_text(encoding="utf-8")
    assert dmg.index('_prune_stdlib_zip.py" "$STDLIB_ZIP"') < dmg.index('_split_site_packages.py" "$PRUNE_PYLIB"') \
        < dmg.index("==> bundle sanity: Mach-O-as-.py check")
    assert '"$REPO_ROOT/scripts/_site_isolation_check.py"' in dmg and "ISO_ARGS=(--bundled" in dmg
    assert '"$APP_SITE/fused_render/_child.py"' in dmg
    assert "site-packages/fused_render/_fused_cli.py" in dmg
    ps1 = (SCRIPTS / "build_windows_installer.ps1").read_text(encoding="utf-8")
    pth = re.search(r'^@\((.*)\) \|\s*$\s*Set-Content -Path \(Join-Path \$PythonRoot "python312\._pth"\)', ps1, re.M)
    assert pth and "site-packages" not in pth.group(1) and '"import site"' in pth.group(1)
    assert "_site_isolation_check.py" in ps1
    assert "_site_isolation_check.py" in (SCRIPTS / "build_linux_appimage.sh").read_text(encoding="utf-8")
