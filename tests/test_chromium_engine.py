"""The Chromium engine's locator (`fused_render.chromium`): where the engine
the app ships is, whether it is usable, and the one rule for which engine a
new window gets. OS-neutral; the engine dir is a fake tree under tmp."""
import json
import os

import pytest

from fused_render import chromium as eng

CEF = "139.0.37+gabc+chromium-139.0.1.2"


@pytest.fixture
def manifest(monkeypatch):
    monkeypatch.setattr(eng, "_read_manifest", lambda: {
        "cef_version": CEF, "cef_sha1": {"arm64": "abc", "x86_64": None}})
    # this may be a Linux CI box: the engine is a macOS thing, so pretend
    monkeypatch.setattr(eng, "platform_supported", lambda system=None: system != "linux")
    monkeypatch.delenv("FUSED_RENDER_CHROMIUM_ENGINE_DIR", raising=False)


def _engine(root, info=None, info_in_resources=False):
    """A tree shaped like native/build.sh's output — or like the app bundle,
    where engine.json has moved to Contents/Resources."""
    app = root / "App.app"
    d = app / eng.BUNDLED_SUBDIR
    os.makedirs(d / eng.FRAMEWORK_NAME)
    where = app / eng.BUNDLED_INFO if info_in_resources else d / eng.ENGINE_INFO
    os.makedirs(where.parent, exist_ok=True)
    where.write_text(json.dumps(info or {"cef_version": CEF, "arch": "arm64"}))
    return d


def test_shipped_manifest_pins_cef_for_arm64_only():
    m = eng._read_manifest()
    assert m["cef_version"] and m["cef_sha1"]["arm64"]
    assert m["cef_sha1"]["x86_64"] is None


def test_host_arch_normalises_machine_names():
    assert eng.host_arch("arm64") == "arm64"
    assert eng.host_arch("aarch64") == "arm64"
    assert eng.host_arch("x86_64") == "x86_64"
    assert eng.host_arch("AMD64") == "x86_64"


def test_only_macos_is_supported():
    assert eng.platform_supported("darwin")
    assert not eng.platform_supported("linux")
    assert not eng.platform_supported("win32")


def test_status_unsupported_off_macos(manifest):
    assert eng.status("arm64", system="linux")["state"] == eng.UNSUPPORTED


def test_no_engine_dir_outside_a_bundle_is_missing(manifest, monkeypatch):
    monkeypatch.setattr(eng.sys, "frozen", None, raising=False)
    assert eng.engine_dir() is None
    assert eng.framework_path() is None
    st = eng.status("arm64", system="darwin")
    assert st == {"state": eng.MISSING, "cef_version": None, "path": None}


def test_engine_dir_from_the_env_override_is_ready(manifest, tmp_path, monkeypatch):
    d = _engine(tmp_path)
    monkeypatch.setenv("FUSED_RENDER_CHROMIUM_ENGINE_DIR", str(d))
    assert eng.engine_dir() == str(d)
    assert eng.framework_path() == os.path.join(str(d), eng.FRAMEWORK_NAME)
    assert eng.is_ready("arm64")
    st = eng.status("arm64", system="darwin")
    assert st == {"state": eng.READY, "cef_version": CEF, "path": str(d)}


def test_engine_dir_from_the_frozen_bundle(manifest, tmp_path, monkeypatch):
    """py2app: sys.executable is <App>.app/Contents/MacOS/python; the engine
    is at Contents/Frameworks/FusedChromium and its engine.json under
    Contents/Resources (strict sealing allows no plain file beside bundles)."""
    d = _engine(tmp_path, info_in_resources=True)
    app = tmp_path / "App.app"
    monkeypatch.setattr(eng.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(eng.sys, "executable", str(app / "Contents" / "MacOS" / "python"))
    assert eng.engine_dir() == str(d)
    assert eng.info_path(str(d)) == str(app / eng.BUNDLED_INFO)
    assert eng.status("arm64", system="darwin")["state"] == eng.READY


def test_engine_json_must_name_this_arch_and_cef(manifest, tmp_path, monkeypatch):
    d = _engine(tmp_path, info={"cef_version": CEF, "arch": "x86_64"})
    monkeypatch.setenv("FUSED_RENDER_CHROMIUM_ENGINE_DIR", str(d))
    assert not eng.is_ready("arm64")
    assert eng.status("arm64", system="darwin")["state"] == eng.MISSING
    (d / eng.ENGINE_INFO).write_text(json.dumps({"cef_version": "1.0", "arch": "arm64"}))
    assert not eng.is_ready("arm64")
    with pytest.raises(ValueError, match="cef_version"):
        eng.check_engine_info(str(d / eng.ENGINE_INFO), arch="arm64")
    (d / eng.ENGINE_INFO).write_text("not json")
    with pytest.raises(ValueError, match="readable"):
        eng.check_engine_info(str(d / eng.ENGINE_INFO), arch="arm64")


def test_a_dir_without_the_framework_is_not_ready(manifest, tmp_path, monkeypatch):
    d = tmp_path / "empty"
    os.makedirs(d)
    monkeypatch.setenv("FUSED_RENDER_CHROMIUM_ENGINE_DIR", str(d))
    assert eng.status("arm64", system="darwin") == {"state": eng.MISSING, "cef_version": None, "path": str(d)}


def test_profile_dir_is_under_the_base_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    assert eng.profile_dir() == str(tmp_path / "home" / "chromium" / "profile")


# ---- engine selection ----------------------------------------------------------

def test_engine_for_new_window_needs_pref_ready_and_loadable():
    assert eng.engine_for_new_window(True, eng.READY, True) == "chromium"
    assert eng.engine_for_new_window(False, eng.READY, True) == "webkit"
    assert eng.engine_for_new_window(True, eng.MISSING, True) == "webkit"
    assert eng.engine_for_new_window(True, eng.READY, False) == "webkit"
    assert eng.engine_for_new_window(True, eng.UNSUPPORTED, False) == "webkit"
