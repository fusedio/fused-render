"""Containment checks compare `os.path.normcase` forms, so on a case-folding
platform (Windows) `C:/Users/X` and `c:/users/x/a` count as inside. Windows is
simulated by making normcase lower-case and realpath the identity."""
import os
import sys

import pytest


@pytest.fixture
def folding(monkeypatch):
    # The POSIX-style fixture paths below only mean what they say where os.sep is
    # "/"; a real Windows host exercises the real normcase for free anyway.
    if sys.platform == "win32":
        pytest.skip("simulates Windows case-folding on a POSIX host")
    monkeypatch.setattr(os.path, "normcase", lambda p: p.lower())
    monkeypatch.setattr(os.path, "realpath", lambda p, **kw: p)
    monkeypatch.setattr(os.path, "abspath", lambda p: p)


def test_export_within_page_dir_ignores_case(folding):
    from fused_render.export import _within_page_dir
    assert _within_page_dir("/Pages/Demo", "/pages/demo/a.png")
    assert _within_page_dir("/Pages/Demo", "/PAGES/DEMO")
    assert not _within_page_dir("/Pages/Demo", "/pages/demo-other/a.png")


def test_roots_is_under_ignores_case(folding):
    from fused_render.agent_backends.roots import is_under
    assert is_under("/Users/X/proj/a", "/users/x/proj")
    assert not is_under("/users/x/proj-old", "/Users/X/proj")


def test_registered_apps_workspace_overlap_ignores_case(folding, monkeypatch):
    from fused_render import registered_apps
    monkeypatch.setattr("fused_render.shell.seed.fused_dir", lambda: "/Users/X/Fused")
    assert registered_apps._in_or_over_workspace("/users/x/fused/app/foo")


def test_fs_mutate_descendant_guard_ignores_case(folding):
    from fused_render.server import fs_mutate
    assert fs_mutate._is_descendant_or_self("/Users/X/a", "/users/x/A/b")
    assert not fs_mutate._is_descendant_or_self("/Users/X/a", "/users/x/ab")


def test_posix_case_still_matters():
    from fused_render.export import _within_page_dir
    # normcase is the identity on POSIX: byte-identical behaviour.
    if os.path.normcase("A") == "A":
        assert not _within_page_dir("/tmp/Pages/Demo", "/tmp/pages/demo/a.png")
