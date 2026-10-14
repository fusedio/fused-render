"""Shared helper: force every hardware-accelerated AI runner probe (`_cuda`,
`_rocm`, `_vulkan`) to refuse, regardless of what the host
machine running the suite actually has.

**Why this exists (code review finding).** Every one of those probes reads
the REAL host by design — kernel device nodes, a loader `.so` — because that is the whole point of `registry.py`'s "answers
with a REASON" contract. A test that fakes `platform.system`/`machine` alone
and then asserts a CPU/base-row resolution is silently host-dependent: green
on the machine that wrote it, red the moment CI runs on a box that happens to
have a real Vulkan-capable GPU or a real NVIDIA card / AMD render node on a
Linux runner. `test_ai_runtime.py::_fake_amd`/`_fake_vulkan` and
their neighbours already fake hardware PRESENT for the probes' own dedicated
tests; this is the opposite fixture, for every test whose actual subject is
the FALLTHROUGH path (the CPU/base row `auto` reaches when nothing
accelerated is there) rather than hardware detection itself.

Every constant here is the same module-level seam each probe's own tests
already monkeypatch individually — see `registry.py`'s own comment ("Every
path is a module-level constant so a test can build a fake sysfs on a
tmp_path and repoint them"). This helper just does all of them together, for
tests that don't care which one refuses and only need ALL of them to.
"""
from fused_render.ai import registry


def force_no_accelerators(monkeypatch, tmp_path):
    """After this call, `_cuda`, `_rocm` and `_vulkan` all refuse on every
    platform branch they have, no matter what device nodes or loader
    libraries actually exist on the machine running
    the test. Callers still control `platform.system`/`machine` themselves —
    this only removes the hardware-presence half of the answer.
    """
    missing = str(tmp_path / "fused-render-test-missing")

    # _cuda: the WSL2 branch and the Linux device-node branch, both at once.
    monkeypatch.setattr(registry, "WSL_DXG_DEVICE", missing + "-dxg")
    monkeypatch.setattr(registry, "WSL_CUDA_LIBRARY", missing + "-libcuda.so.1")
    monkeypatch.setattr(registry, "NVIDIA_CONTROL_DEVICE", missing + "-nvidiactl")
    monkeypatch.setattr(registry, "NVIDIA_DEVICE_DIR", str(tmp_path / "no-dev"))

    # _rocm: no /dev/kfd, no DRM class directory to find an AMD render node in.
    monkeypatch.setattr(registry, "KFD_DEVICE", missing + "-kfd")
    monkeypatch.setattr(registry, "DRM_CLASS_DIR", str(tmp_path / "no-drm"))
    monkeypatch.setattr(registry, "KFD_NODES_DIR", str(tmp_path / "no-kfd-nodes"))

    # _vulkan: no loader, no ICD directory.
    monkeypatch.setattr(registry, "VULKAN_LOADER_PATHS",
                        (missing + "-libvulkan.so.1",))
    monkeypatch.setattr(registry, "VULKAN_ICD_DIRS", (str(tmp_path / "no-icd"),))
