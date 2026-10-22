"""The on-demand Chromium engine: manifest, cache layout, download / verify /
unpack state machine (`fused_render.chromium`). OS-neutral; the HTTP fetch
is replaced by a fake that writes a zip built here."""
import hashlib
import io
import json
import os
import stat
import threading
import zipfile

import pytest

from fused_render import chromium as eng
from fused_render import jobs


def _manifest(sha=None, size=None, arch="arm64"):
    return {
        "engine_version": "7",
        "cef_version": "139.0.37+gabc+chromium-139.0.1.2",
        "base_url": "https://example.test/rel/",
        "artifacts": {arch: {"file": f"FusedChromium-7-{arch}.zip", "sha256": sha, "size": size}},
    }


def _engine_zip(info=None, top="FusedChromium", with_marker_dir=True, extra=None) -> bytes:
    """A zip shaped like the build script's artifact."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        base = f"{top}/" if top else ""
        if with_marker_dir:
            zf.writestr(f"{base}FusedChromium.framework/Versions/A/FusedChromium", b"\xcf\xfa\xed\xfe")
            # executable bit on the binary
            zi = zipfile.ZipInfo(f"{base}FusedChromium.framework/Versions/A/Resources/x.sh")
            zi.external_attr = (0o100755) << 16
            zf.writestr(zi, "#!/bin/sh\n")
            # a symlink member, the way `ditto`/`zip -y` records one
            li = zipfile.ZipInfo(f"{base}FusedChromium.framework/Versions/Current")
            li.external_attr = (0o120755) << 16
            zf.writestr(li, "A")
        zf.writestr(f"{base}engine.json", json.dumps(info or {
            "engine_version": "7", "cef_version": "139.0.37+gabc+chromium-139.0.1.2",
            "arch": "arm64"}))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    # this is a Linux CI box: the engine is a macOS thing, so pretend
    monkeypatch.setattr(eng, "platform_supported", lambda system=None: system != "linux")
    # fresh module state per test — and a fresh job registry, which the
    # install mirrors itself into (process-global, like the module state)
    eng._state.__init__()
    jobs.reset()
    yield tmp_path
    jobs.reset()


def _fake_fetch(data: bytes, calls=None):
    def fetch(url, dest, progress):
        if calls is not None:
            calls.append(url)
        progress(0.5)
        with open(dest, "wb") as f:
            f.write(data)
        progress(1.0)
    return fetch


def _published(data: bytes):
    return _manifest(sha=hashlib.sha256(data).hexdigest(), size=len(data))


# ---- manifest / arch / platform ---------------------------------------------

def test_shipped_manifest_parses_and_names_both_mac_arches():
    m = eng._read_manifest()
    for arch in ("arm64", "x86_64"):
        a = eng.artifact(arch, m)
        assert a is not None and a.url.endswith(".zip") and a.arch == arch
        assert a.engine_version and a.cef_version


def test_host_arch_normalises_machine_names():
    assert eng.host_arch("arm64") == "arm64"
    assert eng.host_arch("aarch64") == "arm64"
    assert eng.host_arch("AMD64") == "x86_64"
    assert eng.host_arch("x86_64") == "x86_64"


def test_only_macos_is_supported():
    assert eng.platform_supported("darwin")
    assert not eng.platform_supported("linux")
    assert not eng.platform_supported("win32")


def test_status_unsupported_off_macos(home):
    assert eng.status(system="linux")["state"] == eng.UNSUPPORTED


def test_status_unpublished_when_manifest_has_no_hash(home, monkeypatch):
    monkeypatch.setattr(eng, "_read_manifest", lambda: _manifest())
    st = eng.status("arm64", system="darwin")
    assert st["state"] == eng.UNPUBLISHED
    assert st["engine_version"] == "7"
    # install() is a no-op there — nothing to download
    assert eng.install("arm64")["state"] == eng.UNPUBLISHED


def test_status_unpublished_for_an_arch_the_manifest_lacks(home, monkeypatch):
    monkeypatch.setattr(eng, "_read_manifest", lambda: _manifest(sha="ab", size=1))
    assert eng.status("x86_64", system="darwin")["state"] == eng.UNPUBLISHED


# ---- the happy path ---------------------------------------------------------

def test_install_downloads_verifies_unpacks_and_marks_ready(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    calls = []
    assert eng.status("arm64", system="darwin")["state"] == eng.MISSING
    assert eng.ensure("arm64", fetch=_fake_fetch(data, calls), timeout=10)
    assert calls == ["https://example.test/rel/FusedChromium-7-arm64.zip"]
    st = eng.status("arm64", system="darwin")
    assert st["state"] == eng.READY and st["progress"] == 1.0
    art = eng.artifact("arm64")
    d = eng.engine_dir(art)
    assert d == os.path.join(str(home / "home"), "chromium", "engine-7-arm64")
    assert os.path.isfile(os.path.join(d, eng.READY_MARKER))
    assert os.path.isdir(eng.framework_path(art))
    # executable bit restored, symlink recreated as a symlink
    sh = os.path.join(d, "FusedChromium.framework/Versions/A/Resources/x.sh")
    assert os.stat(sh).st_mode & stat.S_IXUSR
    cur = os.path.join(d, "FusedChromium.framework/Versions/Current")
    assert os.path.islink(cur) and os.readlink(cur) == "A"
    # no temp dirs left behind
    assert [e for e in os.listdir(os.path.dirname(d)) if e.startswith(".download")] == []


def test_install_is_idempotent_once_ready(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    calls = []
    assert eng.ensure("arm64", fetch=_fake_fetch(data, calls), timeout=10)
    assert eng.install("arm64", fetch=_fake_fetch(data, calls))["state"] == eng.READY
    assert len(calls) == 1


def test_engine_root_is_the_base_home_not_the_branch_dir(home, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "feature-x")
    assert eng.engines_root() == os.path.join(str(home / "home"), "chromium")


# ---- failures -----------------------------------------------------------------

def test_hash_mismatch_fails_and_leaves_nothing(home, monkeypatch):
    data = _engine_zip()
    m = _published(data)
    m["artifacts"]["arm64"]["sha256"] = "0" * 64
    monkeypatch.setattr(eng, "_read_manifest", lambda: m)
    assert not eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    st = eng.status("arm64", system="darwin")
    assert st["state"] == eng.FAILED and "SHA-256" in st["error"]
    assert not os.path.exists(eng.engine_dir(eng.artifact("arm64")))


def test_size_mismatch_is_caught_before_hashing(home, monkeypatch):
    data = _engine_zip()
    m = _published(data)
    m["artifacts"]["arm64"]["size"] = len(data) + 1
    monkeypatch.setattr(eng, "_read_manifest", lambda: m)
    assert not eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert "size" in eng.status("arm64", system="darwin")["error"]


def test_engine_json_must_agree_with_the_manifest(home, monkeypatch):
    data = _engine_zip(info={"engine_version": "7", "cef_version": "other", "arch": "arm64"})
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    assert not eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert "cef_version" in eng.status("arm64", system="darwin")["error"]


def test_missing_framework_in_artifact_fails(home, monkeypatch):
    data = _engine_zip(with_marker_dir=False)
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    assert not eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert "not found" in eng.status("arm64", system="darwin")["error"]


def test_fetch_exception_is_reported_and_retry_works(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))

    def boom(url, dest, progress):
        raise OSError("network down")

    assert not eng.ensure("arm64", fetch=boom, timeout=10)
    assert eng.status("arm64", system="darwin") == {
        **eng.status("arm64", system="darwin"), "state": eng.FAILED, "error": "network down"}
    # a retry clears the error
    assert eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert eng.status("arm64", system="darwin")["state"] == eng.READY


def test_failure_is_scoped_to_the_engine_it_happened_to(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    assert not eng.ensure("arm64", fetch=lambda *a: (_ for _ in ()).throw(OSError("x")), timeout=10)
    # manifest moves on to engine 8: the old failure does not stick to it
    m8 = _published(data)
    m8["engine_version"] = "8"
    monkeypatch.setattr(eng, "_read_manifest", lambda: m8)
    assert eng.status("arm64", system="darwin")["state"] == eng.MISSING


# ---- the unpacker -------------------------------------------------------------

def test_safe_extract_refuses_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    p = tmp_path / "z.zip"
    p.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="unsafe member"):
        eng.safe_extract(str(p), str(tmp_path / "out"))
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_refuses_escaping_symlinks(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        li = zipfile.ZipInfo("link")
        li.external_attr = (0o120755) << 16
        zf.writestr(li, "../../etc")
    p = tmp_path / "z.zip"
    p.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="unsafe symlink"):
        eng.safe_extract(str(p), str(tmp_path / "out"))


def test_safe_extract_refuses_absolute_members(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo("/tmp/evil")
        zf.writestr(zi, "x")
    p = tmp_path / "z.zip"
    p.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="unsafe member"):
        eng.safe_extract(str(p), str(tmp_path / "out"))


# ---- concurrency / progress -----------------------------------------------------

def test_status_reports_downloading_while_the_fetch_runs(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    started, release = threading.Event(), threading.Event()

    def slow(url, dest, progress):
        progress(0.25)
        started.set()
        release.wait(5)
        with open(dest, "wb") as f:
            f.write(data)

    st = eng.install("arm64", fetch=slow)
    assert st["state"] == eng.DOWNLOADING
    assert started.wait(5)
    st = eng.status("arm64", system="darwin")
    assert st["state"] == eng.DOWNLOADING and 0 < st["progress"] < 0.9
    # a second install while running does not start another thread
    t = eng._state.thread
    assert eng.install("arm64", fetch=slow)["state"] == eng.DOWNLOADING
    assert eng._state.thread is t
    assert not eng.remove("arm64")  # refused mid-download
    release.set()
    t.join(5)
    assert eng.status("arm64", system="darwin")["state"] == eng.READY


# ---- the Activity row (D1421) -------------------------------------------------

def _row():
    rows = [j for j in jobs.list_jobs() if j["id"].startswith(eng.JOB_ID_PREFIX)]
    assert len(rows) <= 1
    return rows[0] if rows else None


def test_install_reports_a_download_row_with_bytes_then_done(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    started, release = threading.Event(), threading.Event()

    def slow(url, dest, progress):
        progress(0.25)
        started.set()
        release.wait(5)
        with open(dest, "wb") as f:
            f.write(data)
        progress(1.0)

    assert _row() is None
    eng.install("arm64", fetch=slow)
    assert started.wait(5)
    row = _row()
    assert row is not None
    assert row["id"] == "sys:chromium-engine:7-arm64"
    assert row["owner"] == "server" and row["cancellable"] is False
    assert row["kind"] == "download" and row["unit"] == "bytes"
    assert row["title"] == eng.JOB_TITLE and row["detail"] == "Downloading"
    assert row["origin"] == "Preferences" and row["page"] == "/preferences"
    assert row["state"] == "running"
    # the fetch's own fraction, in bytes of the manifest's size
    assert row["total"] == len(data) and row["done"] == pytest.approx(0.25 * len(data))
    release.set()
    eng._state.thread.join(5)
    row = _row()
    assert row["state"] == "done" and row["done"] == row["total"] == len(data)
    assert row["message"] == eng.JOB_DONE_MESSAGE


def test_install_row_exists_before_the_worker_runs(home, monkeypatch):
    # The row is opened by `install` itself, so the request that turned the
    # preference on returns with it already there for the chip's next poll.
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    release = threading.Event()

    def slow(url, dest, progress):
        release.wait(5)
        with open(dest, "wb") as f:
            f.write(data)

    eng.install("arm64", fetch=slow)
    row = _row()
    assert row is not None and row["state"] == "running" and row["done"] == 0.0
    assert row["total"] == len(data)
    release.set()
    eng._state.thread.join(5)


def test_failed_install_reports_an_error_row_and_a_retry_reopens_it(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))

    def boom(url, dest, progress):
        raise OSError("network down")

    assert not eng.ensure("arm64", fetch=boom, timeout=10)
    row = _row()
    assert row["state"] == "error" and row["message"] == "network down"
    assert eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    row = _row()
    assert row["state"] == "done" and row["message"] == eng.JOB_DONE_MESSAGE


def test_verify_and_unpack_phases_name_their_verb(home, monkeypatch):
    data = _engine_zip()
    art = eng.artifact("arm64", manifest=_published(data))
    assert eng._job_fields(art, 0.45, "download")["done"] == pytest.approx(0.5 * len(data))
    assert eng._job_fields(art, 0.9, "verify") == {
        "state": "running", "done": float(len(data)), "total": float(len(data)),
        "detail": "Verifying", "message": ""}
    assert eng._job_fields(art, 0.92, "unpack")["detail"] == "Unpacking"
    # the final tick is the worker's terminal row, not a progress one
    assert eng._job_fields(art, 1.0, "ready") is None


def test_a_registry_failure_never_breaks_the_install(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    monkeypatch.setattr(jobs, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert eng.status("arm64", system="darwin")["state"] == eng.READY


def test_remove_deletes_a_ready_engine(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    assert eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert eng.remove("arm64")
    assert eng.status("arm64", system="darwin")["state"] == eng.MISSING
    assert not eng.remove("arm64")


def test_a_dir_without_the_marker_is_not_ready(home, monkeypatch):
    data = _engine_zip()
    monkeypatch.setattr(eng, "_read_manifest", lambda: _published(data))
    art = eng.artifact("arm64")
    os.makedirs(eng.framework_path(art))
    assert eng.status("arm64", system="darwin")["state"] == eng.MISSING
    # and ensure() rebuilds it rather than trusting it
    assert eng.ensure("arm64", fetch=_fake_fetch(data), timeout=10)
    assert os.path.isfile(os.path.join(eng.engine_dir(art), eng.READY_MARKER))


# ---- engine selection ----------------------------------------------------------

def test_engine_for_new_window_needs_pref_ready_and_loadable():
    f = eng.engine_for_new_window
    assert f(True, eng.READY, True) == "chromium"
    assert f(False, eng.READY, True) == "webkit"
    assert f(True, eng.DOWNLOADING, True) == "webkit"
    assert f(True, eng.MISSING, True) == "webkit"
    assert f(True, eng.FAILED, True) == "webkit"
    assert f(True, eng.READY, False) == "webkit"


def test_http_fetch_refuses_plain_http(tmp_path):
    with pytest.raises(ValueError, match="https"):
        eng._http_fetch("http://example.test/x.zip", str(tmp_path / "x"), lambda f: None)
