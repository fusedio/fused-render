"""Tests for the per-file sidecar's new home (D83-reversal):
home_dir()/sidecar/<mapped path>.json instead of a sibling of the target.

Two implementations exist, deliberately: `fused_render/shell/storage.py`
(importable by core server code) and `fused_render/templates/shared/appenv.py`
(a self-contained stdlib-only mirror for template subprocesses, which cannot
import fused_render — SPEC PY-15). Both must agree, so parity between them is
pinned here the same way test_template_appenv.py pins is_mount_backed/
mount_read_only parity against shell/mounts.py.

The pure path-classification half (_sidecar_subpath) takes no OS calls and is
built on ntpath.splitdrive, so — like _view_url_codec.py's tests — it runs
identically on Windows, macOS, and Linux with no platform guard: a
Windows-shaped absolute path is valid input on any host.
"""
import importlib.util
import os

import pytest

from fused_render.shell import storage

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPENV_PATH = os.path.join(REPO_ROOT, "fused_render", "templates", "shared",
                           "appenv.py")


def _load_appenv():
    spec = importlib.util.spec_from_file_location("_appenv_sidecar", APPENV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def appenv():
    return _load_appenv()


# ------------------------------------------------------- _sidecar_subpath

@pytest.mark.parametrize("abs_path,expected", [
    ("/Users/vasu/Documents/temp/abc.html", "Users/vasu/Documents/temp/abc.html"),
    ("/data/x.json", "data/x.json"),
    ("/", ""),
    (r"C:\Users\vasu\Documents\temp\abc.html", "C/Users/vasu/Documents/temp/abc.html"),
    ("C:/Users/vasu/file.txt", "C/Users/vasu/file.txt"),  # forward-slash form
    (r"c:\users\vasu\FILE.TXT", "C/users/vasu/FILE.TXT"),  # drive folds to upper, rest untouched
    (r"\\server\share\dir\file.txt", "unc/server/share/dir/file.txt"),
    (r"\\server\share", "unc/server/share"),  # bare share, no trailing path
])
def test_sidecar_subpath(abs_path, expected):
    assert storage._sidecar_subpath(abs_path) == expected


def test_sidecar_subpath_preserves_posix_backslash():
    # Backslash is a legal POSIX filename character. A POSIX path (no drive,
    # no UNC prefix) must round-trip it untouched, never fold it to "/" — that
    # would collide "weird\file.txt" with the entirely different
    # "weird/file.txt" (Bugbot).
    assert storage._sidecar_subpath("/data/weird\\file.txt") == "data/weird\\file.txt"
    assert (storage._sidecar_subpath("/data/weird\\file.txt")
            != storage._sidecar_subpath("/data/weird/file.txt"))


def test_sidecar_subpath_preserves_case():
    # A case-sensitive filesystem (Linux) must never fold "/Users/..." and
    # "/users/..." into the same sidecar — they are different files.
    assert storage._sidecar_subpath("/Users/vasu/a.txt") == "Users/vasu/a.txt"
    assert storage._sidecar_subpath("/users/vasu/a.txt") == "users/vasu/a.txt"
    assert (storage._sidecar_subpath("/Users/vasu/a.txt")
            != storage._sidecar_subpath("/users/vasu/a.txt"))


def test_sidecar_subpath_parity_with_appenv_mirror(appenv):
    cases = [
        "/Users/vasu/Documents/temp/abc.html",
        r"C:\Users\vasu\Documents\temp\abc.html",
        "C:/Users/vasu/file.txt",
        r"\\server\share\dir\file.txt",
        "/data/résumé.parquet",
        "/data/weird\\file.txt",
    ]
    for p in cases:
        assert storage._sidecar_subpath(p) == appenv._sidecar_subpath(p), p


# ------------------------------------------------------------- sidecar_path

@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    return h


def test_sidecar_path_rooted_under_home(home):
    f = "/Users/vasu/Documents/temp/abc.html"
    expected = os.path.join(str(home), "sidecar", "Users", "vasu", "Documents",
                             "temp", "abc.html.json")
    assert storage.sidecar_path(f) == expected


def test_sidecar_path_relative_input_is_resolved_first(home, tmp_path, monkeypatch):
    # sidecar_path abspaths its input (matching the prior co-located behavior:
    # the sidecar sits wherever the file APPEARS to be, not a resolved target).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.html").write_text("<html></html>")
    resolved = storage.sidecar_path("rel.html")
    assert resolved == storage.sidecar_path(str(tmp_path / "rel.html"))


def test_sidecar_path_parity_with_appenv_mirror(home, monkeypatch, appenv):
    # appenv.home_dir() prefers FUSED_RENDER_HOME_DIR (already branch-resolved)
    # over FUSED_RENDER_HOME; with both unset here and FUSED_RENDER_BRANCH
    # cleared, storage.home_dir()'s branch_dir() is a no-op, so the two land on
    # the identical unnested home — the fair comparison for parity.
    f = "/Users/vasu/Documents/temp/abc.html"
    assert storage.sidecar_path(f) == appenv.sidecar_path(f)


def test_sidecar_path_ends_in_json(home):
    assert storage.sidecar_path("/a/b/c.parquet").endswith("c.parquet.json")


def test_sidecar_path_of_the_filesystem_root_stays_inside_the_sidecar_tree(home):
    # abs_path == "/" maps to an EMPTY subpath, so the parts list is empty —
    # os.path.join(home, "sidecar", *[]) drops straight to ".../sidecar" with
    # nothing to descend into, landing a "sidecar.json" FILE beside the
    # "sidecar" DIRECTORY instead of inside it (Bugbot). The result must stay
    # under the sidecar root so _is_under_sidecar_root's auto-mkdir gate
    # (fs_mutate.py) still recognizes it.
    p = storage.sidecar_path("/")
    root = os.path.join(str(home), "sidecar")
    assert p == os.path.join(root, ".json")
    assert p.startswith(root + os.sep)


def test_sidecar_path_of_root_parity_with_appenv_mirror(home, appenv):
    assert storage.sidecar_path("/") == appenv.sidecar_path("/")
