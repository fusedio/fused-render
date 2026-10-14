"""`_big_files.sparse_file` keeps the one promise its callers rely on.

Those callers assert about a `size_gb` or a quantisation pick derived from a
file's length, so a helper that quietly produced the wrong length would turn
them into tests of nothing. Pinned here rather than left implicit because the
implementation is a `truncate` — plausible-looking and easy to "simplify"
into something that no longer reports the right size on one platform.
"""

import os

import pytest

from _big_files import sparse_file

FIVE_GIB = 5 * 1024 ** 3


def test_the_reported_length_is_the_length_that_was_asked_for(tmp_path):
    path = tmp_path / "model.safetensors"
    sparse_file(path, FIVE_GIB)
    assert os.stat(path).st_size == FIVE_GIB
    assert os.path.getsize(path) == FIVE_GIB      # what the callers call
    assert path.stat().st_size == FIVE_GIB        # …and via pathlib


def test_a_missing_parent_directory_is_created(tmp_path):
    """`_text_repo` hands in a path several levels down a cache layout."""
    path = tmp_path / "snapshots" / "c0ffee" / "model.safetensors"
    sparse_file(path, 2048)
    assert os.path.getsize(path) == 2048


def test_zero_is_a_real_empty_file(tmp_path):
    """`_text_repo`'s default size, which a couple of its callers take."""
    path = tmp_path / "empty.bin"
    sparse_file(path, 0)
    assert path.is_file() and os.path.getsize(path) == 0


def test_reads_come_back_as_nul_bytes(tmp_path):
    """Nothing reads these fixtures today; this says what would be seen if
    something started to, so a future reader is not surprised by `b"x"`."""
    path = tmp_path / "holey.bin"
    sparse_file(path, 4096)
    assert path.read_bytes() == b"\0" * 4096


@pytest.mark.skipif(not hasattr(os.stat_result, "st_blocks"),
                    reason="st_blocks is POSIX-only")
def test_nothing_is_allocated_on_the_disk(tmp_path):
    """The half that makes it worth doing: 5GB of length, no 5GB of disk.

    This is the assertion that fails if the helper regresses to writing real
    bytes — the speed is a side effect of allocating nothing.
    """
    path = tmp_path / "model.safetensors"
    sparse_file(path, FIVE_GIB)
    assert os.stat(path).st_blocks * 512 < 1024 * 1024
