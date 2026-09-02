"""`fused_render.ai.hsa_preload.resolve_preload` — whether a ROCm worker
should be launched with `LD_PRELOAD` pointed at the SYSTEM `libhsa-runtime64.so`
rather than the one torch bundles.

Root cause (verified by backtrace, not reproduced here): on Linux/ROCm,
torch's bundled `libhsa-runtime64.so` starts a thread that busy-waits on
`/dev/kfd` the moment a HIP context exists, pinning ~100% of a CPU core for as
long as the worker is loaded, even fully idle. `LD_PRELOAD`ing the system's
newer ROCr runtime instead measured zero idle jiffies with no correctness
regression on this machine. This module decides ONLY whether that swap is
safe -- it never imports torch or execs anything, since the server process's
own venv deliberately has no torch to import, and the worker's venv is a
separate on-disk tree this process must reason about from the outside.
"""
import os
import sys

import pytest

from fused_render.ai import hsa_preload

ENV_VAR = "FUSED_AI_HSA_PRELOAD"


def _make_venv(tmp_path, *, version_py: str | None, py_minor: str = "python3.12"):
    """A fake venv tree: `<venv>/lib/<py_minor>/site-packages/torch/version.py`,
    written verbatim so tests can hand it real or garbage torch source."""
    venv = tmp_path / "venv"
    torch_dir = venv / "lib" / py_minor / "site-packages" / "torch"
    torch_dir.mkdir(parents=True)
    if version_py is not None:
        (torch_dir / "version.py").write_text(version_py)
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    return str(python)


def _make_rocm_root(tmp_path, *, version: str | None, so_present: bool = True,
                     name: str = "rocm"):
    root = tmp_path / name
    (root / "lib").mkdir(parents=True)
    (root / ".info").mkdir(parents=True)
    if so_present:
        # A real file the symlink resolves to, matching the actual layout
        # (`libhsa-runtime64.so.1 -> libhsa-runtime64.so.1.18.0`).
        real = root / "lib" / "libhsa-runtime64.so.1.18.0"
        real.write_text("not really an .so, just needs to exist")
        (root / "lib" / "libhsa-runtime64.so.1").symlink_to(real)
    if version is not None:
        (root / ".info" / "version").write_text(version)
    return root


ROCM_TORCH_VERSION_PY = """\
__version__ = '2.13.0+rocm7.1'
debug = False
cuda: str = None
git_version = 'deadbeef'
hip = '7.1.52802'
"""

CUDA_TORCH_VERSION_PY = """\
__version__ = '2.13.0+cu124'
debug = False
cuda: str = '12.4'
git_version = 'deadbeef'
hip = None
"""

CPU_TORCH_VERSION_PY = """\
__version__ = '2.13.0+cpu'
debug = False
cuda: str = None
git_version = 'deadbeef'
hip = None
"""

GARBAGE_TORCH_VERSION_PY = """\
__version__ = '2.13.0+rocmXX.YY'
hip = 'nonsense'
"""


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("ROCM_PATH", raising=False)


def test_matching_major_newer_system_minor_preloads(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))

    result = hsa_preload.resolve_preload(python)

    assert result == str(root / "lib" / "libhsa-runtime64.so.1")


def test_matching_major_and_minor_preloads(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.1.0")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is not None


def test_older_system_runtime_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.0.9")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_different_major_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="8.0.0")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_no_torch_at_all_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=None)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_cuda_build_torch_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=CUDA_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_cpu_build_torch_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=CPU_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_missing_system_so_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4", so_present=False)
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_missing_info_version_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version=None)
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_garbage_torch_version_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=GARBAGE_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_garbage_system_version_does_not_preload(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="not-a-version-string")
    monkeypatch.setenv("ROCM_PATH", str(root))

    assert hsa_preload.resolve_preload(python) is None


def test_falls_back_to_opt_rocm_when_rocm_path_unset(tmp_path, monkeypatch):
    """No `ROCM_PATH` -- falls back to `/opt/rocm` itself, verified by
    pointing `_system_rocm`'s hardcoded default at a fake root instead of
    touching the real `/opt/rocm` this dev machine happens to have."""
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.delenv("ROCM_PATH", raising=False)
    monkeypatch.setattr(hsa_preload, "_DEFAULT_ROCM_ROOT", str(root))

    assert hsa_preload.resolve_preload(python) == str(root / "lib" / "libhsa-runtime64.so.1")


def test_missing_default_root_does_not_preload(tmp_path, monkeypatch):
    """No `ROCM_PATH`, and the default root has nothing at it -- confirms the
    fallback constant is actually consulted, not just always-true on a
    machine that happens to have `/opt/rocm` installed."""
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    monkeypatch.delenv("ROCM_PATH", raising=False)
    monkeypatch.setattr(hsa_preload, "_DEFAULT_ROCM_ROOT", str(tmp_path / "nowhere"))

    assert hsa_preload.resolve_preload(python) is None


@pytest.mark.parametrize("value", ["0", "off", "false", "OFF", "False"])
def test_override_disables_the_mechanism(tmp_path, monkeypatch, value):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))
    monkeypatch.setenv(ENV_VAR, value)

    assert hsa_preload.resolve_preload(python) is None


def test_override_names_an_explicit_path_verbatim(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=CPU_TORCH_VERSION_PY)  # would refuse otherwise
    custom = tmp_path / "custom-hsa.so"
    custom.write_text("stand-in")
    monkeypatch.setenv(ENV_VAR, str(custom))

    assert hsa_preload.resolve_preload(python) == str(custom)


def test_override_path_must_exist(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "does-not-exist.so"))

    assert hsa_preload.resolve_preload(python) is None


def test_non_linux_never_preloads(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    root = _make_rocm_root(tmp_path, version="7.2.4")
    monkeypatch.setenv("ROCM_PATH", str(root))
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)

    assert hsa_preload.resolve_preload(python) is None


def test_non_linux_override_is_still_refused(tmp_path, monkeypatch):
    python = _make_venv(tmp_path, version_py=ROCM_TORCH_VERSION_PY)
    custom = tmp_path / "custom-hsa.so"
    custom.write_text("stand-in")
    monkeypatch.setenv(ENV_VAR, str(custom))
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)

    assert hsa_preload.resolve_preload(python) is None
