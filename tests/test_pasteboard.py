"""Tests for the platform-agnostic pasteboard contract
(fused_render/shell/pasteboard/__init__.py).

Everything here monkeypatches the backend seam, so no OS clipboard is ever
touched and the whole file runs identically on macOS, Windows and Linux.
The per-platform backends get their own test files; this one only pins the
contract they all have to satisfy: absolute paths in, (paths, token,
supported) out, and every backend failure degrading to unsupported rather
than propagating.
"""
import pytest

from fused_render.shell import pasteboard


class _Backend:
    """Stand-in for a platform backend module (a namespace, not an ABC —
    same shape as supervisor/_backend.py's re-exported surface)."""

    def __init__(self, paths=(), raises=None):
        self.paths = list(paths)
        self.raises = raises
        self.written = None

    def read_files(self):
        if self.raises is not None:
            raise self.raises
        return list(self.paths)

    def write_files(self, paths):
        if self.raises is not None:
            raise self.raises
        self.written = list(paths)


@pytest.fixture
def backend(monkeypatch):
    """Install a fake backend and return it."""
    def _install(b):
        monkeypatch.setattr(pasteboard, "_load_backend", lambda: b)
        return b
    return _install


# ------------------------------------------------------------------ fingerprint

def test_fingerprint_is_stable():
    a = pasteboard.fingerprint(["/a/b.csv", "/c/d"])
    b = pasteboard.fingerprint(["/a/b.csv", "/c/d"])
    assert a == b
    assert isinstance(a, str) and a


def test_fingerprint_is_order_sensitive():
    # The clipboard is an ordered list — a reorder is a different selection,
    # and the reconcile must see it as a new event.
    assert pasteboard.fingerprint(["/a", "/b"]) != pasteboard.fingerprint(["/b", "/a"])


def test_fingerprint_distinguishes_contents():
    assert pasteboard.fingerprint(["/a"]) != pasteboard.fingerprint(["/a/b"])
    # A separator in a path must not collide with the joining separator.
    assert pasteboard.fingerprint(["/a\n/b"]) != pasteboard.fingerprint(["/a", "/b"])


def test_fingerprint_of_empty_list_is_empty_token():
    # Nothing on the clipboard is not "some state we last saw" — an empty
    # token keeps the frontend's "unchanged?" check honest.
    assert pasteboard.fingerprint([]) == ""


# ------------------------------------------------------------------ unsupported

def test_read_is_unsupported_without_a_backend(backend):
    backend(None)
    paths, token, supported = pasteboard.read_files()
    assert (paths, token, supported) == ([], "", False)


def test_write_is_unsupported_without_a_backend(backend):
    backend(None)
    token, supported = pasteboard.write_files(["/a/b.csv"])
    assert (token, supported) == ("", False)


def test_read_reports_unsupported_when_the_backend_raises(backend):
    backend(_Backend(raises=RuntimeError("no pyobjc")))
    assert pasteboard.read_files() == ([], "", False)


def test_write_reports_unsupported_when_the_backend_raises(backend):
    backend(_Backend(raises=OSError("clipboard busy")))
    assert pasteboard.write_files(["/a/b.csv"]) == ("", False)


# ------------------------------------------------------------------- happy path

def test_read_returns_paths_and_their_fingerprint(backend):
    backend(_Backend(paths=["/a/b.csv", "/c/d"]))
    paths, token, supported = pasteboard.read_files()
    assert paths == ["/a/b.csv", "/c/d"]
    assert token == pasteboard.fingerprint(paths)
    assert supported is True


def test_read_of_an_empty_clipboard_is_supported(backend):
    # Supported means "the bridge works", not "there is something on it".
    b = backend(_Backend(paths=[]))
    assert b is not None
    assert pasteboard.read_files() == ([], "", True)


def test_read_drops_non_absolute_paths(backend):
    # A backend should never hand back a relative path, but if one leaks
    # through it would resolve against the server's cwd on paste.
    backend(_Backend(paths=["/a/b.csv", "relative.txt", ""]))
    paths, _, supported = pasteboard.read_files()
    assert paths == ["/a/b.csv"]
    assert supported is True


def test_write_hands_the_paths_to_the_backend(backend):
    b = backend(_Backend())
    token, supported = pasteboard.write_files(["/a/b.csv", "/c/d"])
    assert b.written == ["/a/b.csv", "/c/d"]
    assert supported is True
    assert token == pasteboard.fingerprint(["/a/b.csv", "/c/d"])


# ------------------------------------------------------------------- validation

def test_write_rejects_a_relative_path(backend):
    b = backend(_Backend())
    with pytest.raises(ValueError):
        pasteboard.write_files(["relative.txt"])
    assert b.written is None


def test_write_rejects_a_non_string_path(backend):
    backend(_Backend())
    with pytest.raises(ValueError):
        pasteboard.write_files([None])


def test_write_of_nothing_is_a_no_op(backend):
    b = backend(_Backend())
    assert pasteboard.write_files([]) == ("", True)
    assert b.written is None
