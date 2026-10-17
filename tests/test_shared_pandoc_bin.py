import importlib
import io
import os
import subprocess
import tarfile
import zipfile

import pytest

SHARED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "shared")


def _load(monkeypatch):
    monkeypatch.syspath_prepend(SHARED)
    return importlib.import_module("pandoc_bin")


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
    ("Darwin", "x86_64", "x86_64-macOS.zip", "pandoc-{v}-x86_64/bin/pandoc", _zip),
    ("Linux", "x86_64", "linux-amd64.tar.gz", "pandoc-{v}/bin/pandoc", _tar),
    ("Linux", "aarch64", "linux-arm64.tar.gz", "pandoc-{v}/bin/pandoc", _tar),
])
def test_install_puts_the_release_binary_where_path_finds_it(
        tmp_path, monkeypatch, system, machine, asset, member, pack):
    pb = _load(monkeypatch)
    monkeypatch.setattr(pb, "BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(pb, "STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
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
