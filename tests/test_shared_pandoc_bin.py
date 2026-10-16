import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile

import pytest

PANDOC_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "shared", "pandoc_bin.py")


def _load():
    spec = importlib.util.spec_from_file_location("_test_pandoc_bin", PANDOC_BIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(io.BytesIO):
    headers = {}


def _zip(member):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, b"native pandoc")
    return buf.getvalue()


def _tar(member):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(b"native pandoc")
        tf.addfile(info, io.BytesIO(b"native pandoc"))
    return buf.getvalue()


@pytest.mark.parametrize("system, machine, asset, member, pack", [
    ("Darwin", "arm64", "arm64-macOS.zip", "pandoc-{v}-arm64/bin/pandoc", _zip),
    ("Linux", "x86_64", "linux-amd64.tar.gz", "pandoc-{v}/bin/pandoc", _tar),
])
def test_install_puts_the_release_binary_where_path_finds_it(
        tmp_path, monkeypatch, system, machine, asset, member, pack):
    pb = _load()
    monkeypatch.setattr(pb, "BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(pb, "STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    monkeypatch.setitem(sys.modules, "pypandoc", None)
    monkeypatch.setattr(pb.shutil, "which", lambda name: None)
    body = pack(member.format(v=pb.VERSION))
    urls = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: urls.append(req.full_url) or _Response(body))
    monkeypatch.setattr(pb.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))

    assert pb.path() is None
    pb._run()

    assert urls[0].endswith(f"/{pb.VERSION}/pandoc-{pb.VERSION}-{asset}")
    assert pb.path() == str(tmp_path / "bin" / "pandoc")
    assert open(pb.path(), "rb").read() == b"native pandoc" and os.access(pb.path(), os.X_OK)
    assert pb._progress()["done"] is True


@pytest.mark.parametrize("version_line, found", [("pandoc 3.8.2", True), ("pandoc 2.12", False)])
def test_a_pandoc_on_path_is_used_only_when_new_enough(tmp_path, monkeypatch, version_line, found):
    pb = _load()
    monkeypatch.setattr(pb, "BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setitem(sys.modules, "pypandoc", None)
    monkeypatch.setattr(pb.shutil, "which", lambda name: "/opt/homebrew/bin/pandoc")
    monkeypatch.setattr(pb.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=version_line + "\n"))
    assert pb.path() == ("/opt/homebrew/bin/pandoc" if found else None)
