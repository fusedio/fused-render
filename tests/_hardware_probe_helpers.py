"""Shared helper: force every hardware-accelerated AI runner probe (`_cuda`,
`_rocm`, `_vulkan`, `_directml`) to refuse, regardless of what the host
machine running the suite actually has.

**Why this exists (code review finding).** Every one of those probes reads
the REAL host by design — kernel device nodes, a loader `.so`/DLL, a Windows
registry key — because that is the whole point of `registry.py`'s "answers
with a REASON" contract. A test that fakes `platform.system`/`machine` alone
and then asserts a CPU/base-row resolution is silently host-dependent: green
on the machine that wrote it, red the moment CI runs on a box that happens to
have a real Vulkan-capable GPU (the GitHub `windows-latest` runner ships
`vulkan-1.dll`, which is exactly what turned this from a theoretical risk
into a real `test-python-windows` failure) or a real NVIDIA card / AMD render
node on a Linux runner. `test_ai_runtime.py::_fake_amd`/`_fake_vulkan` and
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
    """After this call, `_cuda`, `_rocm`, `_vulkan` and `_directml` all
    refuse on every platform branch they have, no matter what device nodes,
    loader libraries, or registry keys actually exist on the machine running
    the test. Callers still control `platform.system`/`machine` themselves —
    this only removes the hardware-presence half of the answer.
    """
    missing = str(tmp_path / "fused-render-test-missing")

    # _cuda: the WSL2 branch, the Linux device-node branch, and the Windows
    # DLL branch, all at once.
    monkeypatch.setattr(registry, "WSL_DXG_DEVICE", missing + "-dxg")
    monkeypatch.setattr(registry, "WSL_CUDA_LIBRARY", missing + "-libcuda.so.1")
    monkeypatch.setattr(registry, "NVIDIA_CONTROL_DEVICE", missing + "-nvidiactl")
    monkeypatch.setattr(registry, "NVIDIA_DEVICE_DIR", str(tmp_path / "no-dev"))
    monkeypatch.setattr(registry, "NVCUDA_DLL", missing + "-nvcuda.dll")

    # _rocm: no /dev/kfd, no DRM class directory to find an AMD render node in.
    monkeypatch.setattr(registry, "KFD_DEVICE", missing + "-kfd")
    monkeypatch.setattr(registry, "DRM_CLASS_DIR", str(tmp_path / "no-drm"))
    monkeypatch.setattr(registry, "KFD_NODES_DIR", str(tmp_path / "no-kfd-nodes"))

    # _vulkan: no loader, no ICD directory, no Windows DLL.
    monkeypatch.setattr(registry, "VULKAN_LOADER_PATHS",
                        (missing + "-libvulkan.so.1",))
    monkeypatch.setattr(registry, "VULKAN_ICD_DIRS", (str(tmp_path / "no-icd"),))
    monkeypatch.setattr(registry, "VULKAN_DLL", missing + "-vulkan-1.dll")

    # _directml: the seam is the function itself, not a path — real code
    # reads a Windows registry key this test process may not even have
    # access to. Reporting "only the Basic Render Driver" refuses on every
    # platform the same way a real headless Windows box would.
    monkeypatch.setattr(
        registry, "_windows_display_adapter_ids",
        lambda: [(registry.MS_BASIC_RENDER_VENDOR, registry.MS_BASIC_RENDER_DEVICE)])
