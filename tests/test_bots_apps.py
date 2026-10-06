"""fused_render.bots.apps: list_apps / import_app / mkbuild / reveal
(ports of OpenBot listapps.py, importapp.py, mkbuild.py, revealapp.py)."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import base64
import io
import json
import os
import zipfile

import pytest

from fused_render.bots import apps

MARKED = '<!doctype html><html><head><meta name="fused-app" /><title>T</title></head><body></body></html>'


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries:
            zf.writestr(name, body)
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "apps")


# ------------------------------------------------------------------ import ---
def test_import_plain_zip_strips_wrapper_folder(root):
    data = _zip([("My App/index.html", MARKED), ("My App/calc.py", "x = 1\n"), ("My App/data/n.txt", "hi"),
                 ("__MACOSX/My App/._index.html", "junk"), ("My App/.DS_Store", "junk")])
    out = apps.import_app(root, "upload.zip", data)
    assert out["folder"] == "My App" and out["files"] == 3 and out["fusedApp"] is True
    assert out["dir"] == os.path.realpath(os.path.join(root, "My App"))
    assert sorted(os.listdir(out["dir"])) == ["calc.py", "data", "index.html"]
    assert open(os.path.join(out["dir"], "data", "n.txt")).read() == "hi"


def test_import_plain_zip_without_wrapper_uses_upload_name(root):
    data = _zip([("index.html", "<html>plain</html>"), ("a.py", "")])
    out = apps.import_app(root, "Thing.zip", data)
    assert out["folder"] == "Thing" and out["files"] == 2 and out["fusedApp"] is False


def test_import_never_touches_existing_folder(root):
    data = _zip([("index.html", MARKED)])
    first = apps.import_app(root, "dup.zip", data)
    with open(os.path.join(first["dir"], "mine.txt"), "w") as f:
        f.write("keep")
    second = apps.import_app(root, "dup.zip", data)
    third = apps.import_app(root, "dup.zip", data)
    assert (first["folder"], second["folder"], third["folder"]) == ("dup", "dup-2", "dup-3")
    assert open(os.path.join(first["dir"], "mine.txt")).read() == "keep"


def test_import_v1_export(root, v1_fused):
    data = base64.b64encode(open(v1_fused, "rb").read()).decode()
    out = apps.import_app(root, "whatever.fused", data)
    assert out["folder"] == "legacy" and out["files"] == 2 and out["fusedApp"] is True
    assert sorted(os.listdir(out["dir"])) == ["calc.py", "index.html"]  # manifest.json not copied


def test_import_v1_export_with_explicit_top_root(root):
    data = _zip([("manifest.json", json.dumps({"fused_app_file": 1, "name": "Top", "entry": "index.html", "root": ""})),
                 ("index.html", MARKED), ("files/index.html", "<html>a data file</html>")])
    out = apps.import_app(root, "t.fused", data)
    assert out["folder"] == "Top" and out["fusedApp"] is True
    assert sorted(os.listdir(out["dir"])) == ["files", "index.html", "manifest.json"]


def test_import_v1_export_with_nested_root(root):
    data = _zip([("manifest.json", json.dumps({"fused_app_file": 1, "name": "Nested", "entry": "index.html",
                                               "root": "payload/app"})),
                 ("payload/app/index.html", MARKED), ("payload/app/sub/x.py", ""), ("payload/other.txt", "no")])
    out = apps.import_app(root, "n.fused", data)
    assert out["folder"] == "Nested" and out["files"] == 2
    assert os.path.isfile(os.path.join(out["dir"], "sub", "x.py"))
    assert not os.path.exists(os.path.join(out["dir"], "other.txt"))


def test_import_v2_container(root, v2_fused):
    data = base64.b64encode(open(v2_fused, "rb").read()).decode()
    out = apps.import_app(root, "demo.fused", data)
    assert out["folder"] == "demo" and out["files"] == 3 and out["fusedApp"] is True
    assert open(os.path.join(out["dir"], "data", "note.txt"), "rb").read() == b"hello"
    assert "def main" in open(os.path.join(out["dir"], "calc.py")).read()


def test_import_v2_container_hash_mismatch_leaves_nothing(root, tmp_path):
    from fused_render import appfile_container as container

    p = tmp_path / "bad.fused"
    container.write(str(p), {"name": "bad", "entry": "index.html"},
                    [("index.html", MARKED.encode()), ("b.txt", b"payload")])
    raw = bytearray(p.read_bytes())
    # Rewrite the index with a wrong sha for b.txt.
    hdr = apps.HEADER
    magic, ver, flags, isize, icsize = hdr.unpack(bytes(raw[:hdr.size]))
    import zlib
    index = json.loads(zlib.decompress(bytes(raw[hdr.size:hdr.size + icsize])))
    index["files"][1]["sha256"] = "0" * 64
    new_idx = json.dumps(index).encode()
    cidx = zlib.compress(new_idx)
    rebuilt = hdr.pack(magic, ver, flags, len(new_idx), len(cidx)) + cidx + bytes(raw[hdr.size + icsize:])
    with pytest.raises(ValueError, match="does not match its declared size or hash"):
        apps.import_app(root, "bad.fused", base64.b64encode(rebuilt).decode())
    assert os.listdir(root) == []


def test_import_v2_without_index_html_refused(root, tmp_path):
    from fused_render import appfile_container as container

    p = tmp_path / "doc.fused"
    container.write(str(p), {"name": "doc", "entry": "a.md"}, [("a.md", b"# hi")])
    with pytest.raises(ValueError, match="no index.html"):
        apps.import_app(root, "doc.fused", base64.b64encode(p.read_bytes()).decode())


def test_import_refuses_path_escape_and_leaves_nothing(root, tmp_path):
    # zipfile writes "../" names verbatim; _safe_parts drops them. A zip of only
    # such entries is empty; one with a nested ../ has no top-level index.html.
    data = _zip([("../index.html", MARKED), ("../../evil.py", "x")])
    with pytest.raises(ValueError, match="the archive is empty"):
        apps.import_app(root, "evil.zip", data)
    data = _zip([("app/../../index.html", MARKED), ("app/x.py", "")])
    with pytest.raises(ValueError, match="no index.html"):
        apps.import_app(root, "evil.zip", data)
    assert os.listdir(root) == []
    assert not os.path.exists(tmp_path / "evil.py") and not os.path.exists(tmp_path / "index.html")


def test_import_drops_unsafe_entries_beside_good_ones(root, tmp_path):
    data = _zip([("index.html", MARKED), ("../escape.txt", "x"), ("a/../../b.txt", "y"), ("c:d.txt", "z")])
    out = apps.import_app(root, "mixed.zip", data)
    assert out["files"] == 1 and os.listdir(out["dir"]) == ["index.html"]
    assert not os.path.exists(tmp_path / "escape.txt") and not os.path.exists(tmp_path / "b.txt")


def test_import_symlink_escape_is_refused_and_cleaned(root, tmp_path, monkeypatch):
    """`_target` resolves realpath: an entry that lands outside dest raises and
    the half-written folder is removed."""
    real_target = apps._target

    def evil_target(dest, parts):
        if parts == ["sub", "x.txt"]:
            os.makedirs(os.path.join(dest), exist_ok=True)
            os.symlink(str(tmp_path), os.path.join(dest, "sub"))
        return real_target(dest, parts)
    monkeypatch.setattr(apps, "_target", evil_target)
    data = _zip([("index.html", MARKED), ("sub/x.txt", "x")])
    with pytest.raises(ValueError, match="unsafe path"):
        apps.import_app(root, "sym.zip", data)
    assert os.listdir(root) == []
    assert not os.path.exists(tmp_path / "x.txt")


def test_import_too_large_refused(root, monkeypatch):
    monkeypatch.setattr(apps, "UPLOAD_MAX", 100)
    data = _zip([("index.html", MARKED + " " * 500)])
    with pytest.raises(ValueError, match="too large"):
        apps.import_app(root, "big.zip", data)
    assert not os.path.exists(root) or os.listdir(root) == []


def test_import_bad_inputs(root):
    with pytest.raises(ValueError, match="absolute root"):
        apps.import_app("rel/root", "x.zip", _zip([("index.html", MARKED)]))
    with pytest.raises(ValueError, match="empty upload"):
        apps.import_app(root, "x.zip", "")
    with pytest.raises(ValueError, match="neither a zip"):
        apps.import_app(root, "x.bin", base64.b64encode(b"hello there").decode())
    with pytest.raises(ValueError, match="no index.html"):
        apps.import_app(root, "x.zip", _zip([("a/readme.md", "x"), ("b/index.html", MARKED)]))
    with pytest.raises(ValueError, match="truncated header"):
        apps.import_app(root, "x.fused", base64.b64encode(b"FUSEDAPP\x02").decode())
    assert os.listdir(root) == []


def test_slug():
    assert apps._slug("My App.fused") == "My App"
    assert apps._slug("a/b\\c?.zip") == "a-b-c"
    assert apps._slug("...") == "app" and apps._slug("") == "app"
    assert len(apps._slug("x" * 200)) == 80


# -------------------------------------------------------------------- list ---
def _app(root, folder, html=MARKED, files=()):
    d = os.path.join(root, folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write(html)
    for name in files:
        with open(os.path.join(d, name), "w") as f:
            f.write("<svg/>")
    return d


def test_list_apps_finds_marked_and_skips_unmarked(root):
    a = _app(root, "alpha", files=("icon.png",))
    b = _app(root, "beta", files=("icon.svg", "icon.png"))
    _app(root, "plain", html="<html><title>no marker</title></html>")
    os.makedirs(os.path.join(root, "empty"))
    _app(root, ".hidden")
    os.utime(os.path.join(a, "index.html"), (1_000_000, 1_000_000))
    os.utime(os.path.join(a, "icon.png"), (1_000_000, 1_000_000))
    out = apps.list_apps(root)
    assert out["root"] == root
    assert [x["folder"] for x in out["apps"]] == ["beta", "alpha"]  # newest first
    by = {x["folder"]: x for x in out["apps"]}
    assert by["beta"]["icon"] == "icon.svg" and by["alpha"]["icon"] == "icon.png"
    assert by["alpha"]["mtime"] == 1_000_000 and by["beta"]["mtime"] > 1_000_000
    assert by["alpha"]["dir"] == a and by["beta"]["dir"] == b
    assert set(by["alpha"]) >= {"folder", "dir", "name", "desc", "tools", "skill", "icon", "mtime"}
    assert by["alpha"]["name"] == "T"


def test_list_apps_missing_root_and_bad_root(tmp_path):
    assert apps.list_apps(str(tmp_path / "nope")) == {"root": str(tmp_path / "nope"), "apps": []}
    with pytest.raises(ValueError, match="absolute root"):
        apps.list_apps("relative")
    with pytest.raises(ValueError):
        apps.list_apps("")


# ------------------------------------------------------- mkbuild / reveal ---
def test_mkbuild(tmp_path):
    d = str(tmp_path / "app" / "new-build")
    assert apps.mkbuild(d) == {"dir": d, "existed": False}
    assert os.path.isdir(d)
    assert apps.mkbuild(d) == {"dir": d, "existed": False}  # exists but empty
    open(os.path.join(d, "index.html"), "w").close()
    assert apps.mkbuild(d) == {"dir": d, "existed": True}
    with pytest.raises(ValueError, match="absolute dir"):
        apps.mkbuild("rel")


def test_reveal(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(apps.subprocess, "Popen", lambda argv, **kw: calls.append(argv))  # kw: close_fds=False
    assert apps.reveal(str(tmp_path)) == {"dir": str(tmp_path)}
    assert calls == [["open", str(tmp_path)]]
    for bad in ("", "rel", str(tmp_path / "missing")):
        with pytest.raises(ValueError, match="existing folder"):
            apps.reveal(bad)
    assert len(calls) == 1
