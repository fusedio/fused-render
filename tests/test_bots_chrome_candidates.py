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
        assert all("Program Files" not in p for p in browser.CHROME_CANDIDATES)


def test_error_text_is_platform_neutral(monkeypatch):
    monkeypatch.setattr(browser, "CHROME_CANDIDATES", [])
    with pytest.raises(RuntimeError) as e:
        browser.find_chrome()
    assert "/Applications" not in str(e.value)
    assert "Chrome" in str(e.value)
