"""Finding a Chromium-family browser on Windows for the bots' headless runs."""
import ntpath

import pytest

from fused_render.bots import browser


ENV = {
    "PROGRAMFILES": r"C:\Program Files",
    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
    "LOCALAPPDATA": r"C:\Users\ada\AppData\Local",
}


def _paths(env=ENV, which=lambda n: None):
    return browser._windows_chrome_candidates(env, which, join=ntpath.join)


def test_windows_candidates_cover_chrome_edge_brave_under_every_root():
    got = _paths()
    for root in ENV.values():
        assert ntpath.join(root, "Google", "Chrome", "Application", "chrome.exe") in got
    for root in (ENV["PROGRAMFILES"], ENV["PROGRAMFILES(X86)"]):
        assert ntpath.join(root, "Microsoft", "Edge", "Application", "msedge.exe") in got
    assert ntpath.join(ENV["PROGRAMFILES"], "BraveSoftware", "Brave-Browser",
                       "Application", "brave.exe") in got
    assert ntpath.join(ENV["LOCALAPPDATA"], "BraveSoftware", "Brave-Browser",
                       "Application", "brave.exe") in got


def test_chrome_is_preferred_over_edge_over_brave():
    got = _paths()
    chrome = min(i for i, p in enumerate(got) if p.endswith("chrome.exe"))
    edge = min(i for i, p in enumerate(got) if p.endswith("msedge.exe"))
    brave = min(i for i, p in enumerate(got) if p.endswith("brave.exe"))
    assert chrome < edge < brave


def test_missing_env_roots_are_skipped_not_joined_to_empty():
    got = _paths({"LOCALAPPDATA": r"C:\L"})
    assert got and all(p.startswith("C:\\L") or not p.startswith("\\") for p in got)
    assert not any(p.startswith("Google") for p in got)


def test_path_lookups_are_included():
    names = []
    got = _paths(which=lambda n: names.append(n) or f"X:\\{n}.exe")
    assert {"chrome", "msedge"} <= set(names)
    assert "X:\\chrome.exe" in got and "X:\\msedge.exe" in got


def test_posix_candidates_unchanged():
    if not browser.IS_WINDOWS:
        assert all("Program Files" not in p for p in browser.chrome_candidates())


def test_error_text_is_platform_neutral(monkeypatch):
    monkeypatch.setattr(browser, "chrome_candidates", lambda: [])
    with pytest.raises(RuntimeError) as e:
        browser.find_chrome()
    assert "/Applications" not in str(e.value)
    # The Bots page looks for these words to offer its Install button under the error
    # (frontend/src/apps/bots/lib/setup.ts NO_BROWSER): change both together.
    assert "No Chrome, Chromium, Edge or Brave browser found" in str(e.value)


def test_a_browser_installed_after_import_is_found_without_a_restart(monkeypatch, tmp_path):
    # The walk reads PATH on every call: a list built once at import stayed empty until the server process
    # restarted, which closing the Linux app's window does not do (its supervisor keeps the server).
    monkeypatch.delenv(browser.CHROME_ENV, raising=False)
    monkeypatch.setattr(browser, "MAC_CHROME_APPS", ())
    monkeypatch.setattr(browser, "LINUX_BIN_DIRS", ())
    monkeypatch.setattr(browser, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert browser.found_chrome() is None
    with pytest.raises(RuntimeError):
        browser.find_chrome()
    exe = tmp_path / "google-chrome-stable"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert browser.found_chrome() == str(exe)
    assert browser.find_chrome() == str(exe)


def test_linux_bin_dirs_are_tried_when_path_lacks_them(monkeypatch, tmp_path):
    # snap's /snap/bin is added by a login shell's profile only; a server started without one still finds it.
    got = list(browser._path_chrome_candidates(lambda n: None, dirs=(str(tmp_path),)))
    assert f"{tmp_path}/chromium" in got and f"{tmp_path}/google-chrome" in got
    assert got.index(f"{tmp_path}/google-chrome") < got.index(f"{tmp_path}/chromium")


def test_explicit_pick_goes_first(monkeypatch, tmp_path):
    exe = tmp_path / "my-browser"
    exe.write_text("")
    monkeypatch.setenv(browser.CHROME_ENV, str(exe))
    assert browser.found_chrome() == str(exe)


def test_wsl_error_says_to_install_on_the_linux_side_and_needs_no_restart(monkeypatch):
    monkeypatch.setattr(browser, "chrome_candidates", lambda: [])
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    with pytest.raises(RuntimeError) as e:
        browser.find_chrome()
    assert "inside WSL" in str(e.value) and "restart" not in str(e.value)
