"""Tests for the shared `file_lock` cross-process/thread lock primitive.

Loaded via importlib with templates/shared on sys.path, like test_latex_compile.
The lock is a kernel advisory lock held by an open file handle, so a second
acquire of the same path blocks whether it comes from another process or another
handle in this one.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/latex/tests/test_procutil.py -o addopts=""
"""
import importlib.util
import os
import sys

import pytest

_LATEX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_LATEX), "shared")


def _load_procutil():
    if _SHARED not in sys.path:
        sys.path.insert(0, _SHARED)
    spec = importlib.util.spec_from_file_location(
        "procutil", os.path.join(_SHARED, "procutil.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def procutil():
    return _load_procutil()


def test_file_lock_is_exclusive_across_two_handles_in_one_process(procutil, tmp_path):
    lock = str(tmp_path / "x.lock")
    with procutil.file_lock(lock):
        with pytest.raises(TimeoutError):
            with procutil.file_lock(lock, timeout=0.3):
                pass


def test_file_lock_is_reacquirable_after_the_block_exits(procutil, tmp_path):
    lock = str(tmp_path / "x.lock")
    with procutil.file_lock(lock, timeout=1):
        pass
    with procutil.file_lock(lock, timeout=1):
        pass


def test_file_lock_creates_a_nested_lock_dir(procutil, tmp_path):
    lock = str(tmp_path / "a" / "b" / "c.lock")
    with procutil.file_lock(lock, timeout=1):
        assert os.path.isdir(os.path.dirname(lock))
