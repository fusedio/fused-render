"""Tests for GPU/VRAM detection beyond RAM (SPEC AI-18, D519).

`hw_detect.py` is a subprocess-driven probe (`nvidia-smi`, `rocm-smi`, a
PowerShell WMI/registry query, `sysctl`) — exactly the kind of 50-500ms cold
spawn `fit._wired_limit_mb`'s own docstring refuses on the per-request verdict
path. So every test here drives the small, pure PARSING functions directly
with canned subprocess output, never `subprocess.run` itself, and separately
asserts that `fit.py` only ever reads the on-disk cache
(`hw_detect.cached_hardware`), never `detect_hardware` — the property the
whole module exists to keep.
"""
import json
import sys
import time

import pytest

from fused_render.ai import fit, hw_detect


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


# -- fit.py must never trigger a probe -----------------------------------------


def test_fit_module_only_reads_the_cache_never_the_probe():
    import inspect

    source = inspect.getsource(fit)
    assert "detect_hardware" not in source
    assert "hw_detect.cached_hardware" in source or "hw_detect" not in source


# -- nvidia-smi CSV parsing ------------------------------------------------------


def test_nvidia_smi_csv_is_parsed_into_gpu_devices():
    """`nvidia-smi ... memory.total` reports MEBIBYTES, and `GpuDevice.
    vram_gb`'s own docstring promises decimal GB (matching `fit.GB_BYTES`) —
    so a 24564 MiB reading must land at ~25.77 decimal GB, not the 23.99
    a bare `/1024` (treating the field as if it were already GB) would give.
    `fit._select_pool` multiplies `total_vram_gb` by `GB_BYTES` (1e9); the two
    conversions have to agree or every discrete-NVIDIA verdict silently
    under-reports the pool by ~7.4%."""
    csv = "NVIDIA GeForce RTX 4090, 24564\nNVIDIA GeForce RTX 4090, 24564\n"
    gpus = hw_detect._parse_nvidia_smi(csv)
    assert gpus is not None
    assert len(gpus) == 2
    assert gpus[0].name == "NVIDIA GeForce RTX 4090"
    assert gpus[0].vram_gb == pytest.approx(24564 * 1024 * 1024 / 1e9, rel=1e-6)


def test_nvidia_smi_blank_output_is_no_gpus():
    assert hw_detect._parse_nvidia_smi("") is None
    assert hw_detect._parse_nvidia_smi("   \n") is None


# -- rocm-smi JSON parsing --------------------------------------------------------


def test_rocm_smi_json_is_parsed_into_gpu_devices():
    payload = json.dumps({
        "card0": {"Card series": "AMD Radeon RX 7900 XTX",
                   "VRAM Total Memory (B)": "25753026560"},
    })
    gpus = hw_detect._parse_rocm_smi(payload)
    assert gpus is not None
    assert len(gpus) == 1
    assert gpus[0].name == "AMD Radeon RX 7900 XTX"
    assert gpus[0].vram_gb == pytest.approx(25753026560 / 1e9, rel=1e-3)


def test_rocm_smi_malformed_json_is_no_gpus():
    assert hw_detect._parse_rocm_smi("not json") is None


def test_rocm_smi_title_cased_keys_are_parsed_too():
    """ROCm 6 emits `"Card Series"`, the build this parser was written
    against emitted `"Card series"`. Reading only the lowercase spelling
    skipped every card on a current install, `gpus or None` returned None,
    and a real discrete Radeon reported as no GPU at all — which
    `fit._select_pool` turns into `cpu-only` for every row in the catalog.
    Payload below is verbatim from `rocm-smi 1.1.10` on an RX 9060 XT."""
    payload = json.dumps({
        "card0": {"VRAM Total Memory (B)": "17095983104",
                  "VRAM Total Used Memory (B)": "1827020800",
                  "Card Series": "AMD Radeon RX 9060 XT",
                  "Card Model": "0x7590",
                  "GFX Version": "gfx1200"},
    })
    gpus = hw_detect._parse_rocm_smi(payload)
    assert gpus is not None
    assert gpus[0].name == "AMD Radeon RX 9060 XT"
    assert gpus[0].vram_gb == pytest.approx(17095983104 / 1e9, rel=1e-3)


def test_rocm_smi_is_tried_at_its_opt_rocm_path_when_not_on_path(monkeypatch):
    """ROCm installs to `/opt/rocm/bin` and puts nothing on the default
    PATH, so the bare-name spawn fails with `FileNotFoundError` (which
    `_run` reports as None) on a machine where the tool is installed and
    working."""
    seen = []

    def fake_run(args, timeout=None):
        seen.append(args[0])
        if args[0] == "rocm-smi":
            return None  # not on PATH
        return json.dumps({"card0": {"Card Series": "AMD Radeon RX 9060 XT",
                                     "VRAM Total Memory (B)": "17095983104"}})

    monkeypatch.setattr(hw_detect, "_run", fake_run)
    gpus = hw_detect._amd_gpus()

    assert seen == ["rocm-smi", "/opt/rocm/bin/rocm-smi"]
    assert gpus is not None and gpus[0].name == "AMD Radeon RX 9060 XT"


def test_rocm_smi_on_path_wins_and_no_fallback_is_spawned(monkeypatch):
    """A user's own PATH (a newer or versioned ROCm) stays first — the
    fallback path must not be spawned once the bare name answered."""
    seen = []

    def fake_run(args, timeout=None):
        seen.append(args[0])
        return json.dumps({"card0": {"Card Series": "AMD Radeon RX 7900 XTX",
                                     "VRAM Total Memory (B)": "25753026560"}})

    monkeypatch.setattr(hw_detect, "_run", fake_run)
    gpus = hw_detect._amd_gpus()

    assert seen == ["rocm-smi"]
    assert gpus is not None and gpus[0].name == "AMD Radeon RX 7900 XTX"


# -- Linux DRM sysfs fallback -----------------------------------------------------


LSPCI_MM_NN_D = (
    '0000:00:02.0 "Host bridge [0600]" "Advanced Micro Devices, Inc. [AMD] [1022]" '
    '"Device [14d8]" -r00 -p00 "ASUSTeK Computer Inc. [1043]" "Device [8877]"\n'
    '0000:03:00.0 "VGA compatible controller [0300]" '
    '"Advanced Micro Devices, Inc. [AMD/ATI] [1002]" '
    '"Navi 44 [Radeon RX 9060 XT] [7590]" -rc0 -p00 '
    '"ASUSTeK Computer Inc. [1043]" "Device [061e]"\n'
)


def _drm_tree(tmp_path, cards):
    """A fake `/sys/class/drm`: `cards` maps a card name to the
    `{filename: contents}` under its `device/`. Connector and render-node
    entries are added alongside, since the real directory always has them
    and the probe has to filter them out by shape."""
    root = tmp_path / "drm"
    root.mkdir()
    for name, files in cards.items():
        device = root / name / "device"
        device.mkdir(parents=True)
        for filename, contents in files.items():
            (device / filename).write_text(contents)
    (root / "card1-DP-1").mkdir(exist_ok=True)
    (root / "renderD128").mkdir(exist_ok=True)
    (root / "version").write_text("drm 1.1.0\n")
    return str(root)


def test_sysfs_reports_vram_with_no_vendor_tool_installed(tmp_path, monkeypatch):
    """The whole point of this probe: `rocm-smi` is a multi-GB SDK nobody
    installs to run a GGUF through llama.cpp's Vulkan backend, but the
    kernel has already published the VRAM total. Without this fallback a
    plain Linux desktop with a discrete Radeon reads as GPU-less, and every
    row in the model catalog prints "CPU only"."""
    root = _drm_tree(tmp_path, {"card1": {
        "mem_info_vram_total": "17095983104\n",
        "uevent": "DRIVER=amdgpu\nPCI_ID=1002:7590\nPCI_SLOT_NAME=0000:03:00.0\n",
    }})
    monkeypatch.setattr(hw_detect, "_DRM_CLASS_DIR", root)
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None: LSPCI_MM_NN_D)

    gpus = hw_detect._sysfs_gpus()

    assert gpus is not None and len(gpus) == 1
    # The RETAIL name out of lspci's brackets, not the `Navi 44` codename —
    # the retail spelling is what `_BANDWIDTH_TABLE`'s keys are.
    assert gpus[0].name == "Radeon RX 9060 XT"
    assert gpus[0].vram_gb == pytest.approx(17095983104 / 1e9, rel=1e-3)


def test_sysfs_falls_back_to_the_driver_name_without_lspci(tmp_path, monkeypatch):
    """No lspci is a lost NAME, never a lost VRAM figure — the number is
    what `fit._select_pool` needs, and a device labelled by its driver stays
    identifiable in the cache."""
    root = _drm_tree(tmp_path, {"card1": {
        "mem_info_vram_total": "17095983104\n",
        "uevent": "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:03:00.0\n",
    }})
    monkeypatch.setattr(hw_detect, "_DRM_CLASS_DIR", root)
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None: None)

    gpus = hw_detect._sysfs_gpus()

    assert gpus is not None
    assert gpus[0].name == "amdgpu"
    assert gpus[0].vram_gb == pytest.approx(17.096, rel=1e-3)


def test_sysfs_skips_a_card_with_no_vram_file(tmp_path, monkeypatch):
    """An integrated `i915`/`xe` display device publishes no VRAM total.
    Reporting it as a 0GB device would only make the aggregate across a
    real second card harder to reason about — `_select_pool` reads a zero
    total as "no GPU" regardless."""
    root = _drm_tree(tmp_path, {
        "card0": {"uevent": "DRIVER=i915\n"},
        "card1": {"mem_info_vram_total": "0\n", "uevent": "DRIVER=amdgpu\n"},
    })
    monkeypatch.setattr(hw_detect, "_DRM_CLASS_DIR", root)
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None: None)

    assert hw_detect._sysfs_gpus() is None


def test_sysfs_is_absent_off_linux(tmp_path, monkeypatch):
    """No `/sys/class/drm` at all — every non-Linux platform, where this
    probe is a no-op the `or` chain falls straight through."""
    monkeypatch.setattr(hw_detect, "_DRM_CLASS_DIR", str(tmp_path / "nope"))
    assert hw_detect._sysfs_gpus() is None


def test_sysfs_spawns_lspci_at_most_once_for_many_cards(tmp_path, monkeypatch):
    """This probe exists for machines with no vendor tooling; it must not
    become a per-card subprocess storm on the way there."""
    root = _drm_tree(tmp_path, {
        "card1": {"mem_info_vram_total": "17095983104\n",
                  "uevent": "PCI_SLOT_NAME=0000:03:00.0\n"},
        "card2": {"mem_info_vram_total": "17095983104\n",
                  "uevent": "PCI_SLOT_NAME=0000:04:00.0\n"},
    })
    monkeypatch.setattr(hw_detect, "_DRM_CLASS_DIR", root)
    calls = []
    monkeypatch.setattr(hw_detect, "_run",
                        lambda args, timeout=None: (calls.append(args), LSPCI_MM_NN_D)[1])

    gpus = hw_detect._sysfs_gpus()

    assert len(calls) == 1
    # Multi-GPU aggregation still applies: two cards sum to one pool.
    assert gpus is not None
    assert hw_detect._total_vram_gb(gpus) == pytest.approx(34.19, rel=1e-3)


@pytest.mark.parametrize("raw, expected", [
    ("Navi 44 [Radeon RX 9060 XT] [7590]", "Radeon RX 9060 XT"),
    ("Navi 21 [Radeon RX 6800/6800 XT / 6900 XT] [73bf]", "Radeon RX 6800/6800 XT / 6900 XT"),
    ("GA102 [GeForce RTX 3090] [2204]", "GeForce RTX 3090"),
    ("Device [14d8]", "Device"),
])
def test_lspci_device_names_reduce_to_the_retail_name(raw, expected):
    assert hw_detect._clean_pci_device_name(raw) == expected


def test_sysfs_named_card_still_reaches_the_bandwidth_table(tmp_path, monkeypatch):
    """The reason the retail name is dug out of lspci's brackets at all:
    `_BANDWIDTH_TABLE`'s keys are retail spellings, so a card named by its
    `Navi 21` codename would miss a row it should hit."""
    root = _drm_tree(tmp_path, {"card1": {
        "mem_info_vram_total": "25753026560\n",
        "uevent": "PCI_SLOT_NAME=0000:03:00.0\n",
    }})
    monkeypatch.setattr(hw_detect, "_DRM_CLASS_DIR", root)
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None: (
        '0000:03:00.0 "VGA compatible controller [0300]" "AMD [1002]" '
        '"Navi 31 [Radeon RX 7900 XTX] [744c]" -rc8 -p00 "AMD [1002]" "Device [0e3b]"\n'))

    gpus = hw_detect._sysfs_gpus()

    assert gpus is not None
    assert hw_detect._bandwidth_for(gpus[0].name) == 960.0


# -- Windows AdapterRAM 32-bit cap ------------------------------------------------


def test_adapter_ram_at_the_32bit_cap_is_flagged_for_registry_correction():
    """4278190080 bytes (~3.984 GiB) and above is what a >=4GB card reports
    through the capped 32-bit `AdapterRAM` field — `Win32_VideoController`
    ground truth, not a value real cards report on purpose."""
    assert hw_detect._adapter_ram_is_capped(4_278_190_080) is True
    assert hw_detect._adapter_ram_is_capped(2 * 1024 ** 3) is False


def test_windows_gpu_lines_are_parsed_name_and_bytes():
    text = "NVIDIA GeForce RTX 4080|4294901760\nIntel(R) UHD Graphics|1073741824\n"
    rows = hw_detect._parse_windows_adapter_ram(text)
    assert rows == [("NVIDIA GeForce RTX 4080", 4294901760),
                     ("Intel(R) UHD Graphics", 1073741824)]


def test_registry_qwmemorysize_overrides_a_capped_reading(monkeypatch):
    """The real fix (ground truth, not WMI's own carveout-affected total RAM
    reading): a capped AdapterRAM row is re-resolved through the display
    driver's own registry key, which stores the true 64-bit VRAM size."""
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None:
                         "NVIDIA GeForce RTX 4080|4294901760\n")
    monkeypatch.setattr(hw_detect, "_windows_registry_vram",
                         lambda: {"NVIDIA GeForce RTX 4080": 17179869184})  # ~16 GiB
    gpus = hw_detect._windows_gpus()
    assert gpus is not None
    # Decimal GB, matching GpuDevice.vram_gb's own unit promise — NOT
    # `/1024**3` (binary GiB), which under-reports a real card by ~7.4%.
    assert gpus[0].vram_gb == pytest.approx(17179869184 / 1e9, rel=1e-6)


def test_an_uncapped_reading_is_kept_even_when_the_registry_disagrees(monkeypatch):
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None:
                         "NVIDIA GeForce RTX 3050|3221225472\n")  # 3 GiB, real
    monkeypatch.setattr(hw_detect, "_windows_registry_vram", lambda: {})
    gpus = hw_detect._windows_gpus()
    assert gpus is not None
    assert gpus[0].vram_gb == pytest.approx(3221225472 / 1e9, rel=1e-6)


# -- unified-memory APU overrides -------------------------------------------------


def test_amd_strix_halo_apu_overrides_to_full_system_ram():
    device = hw_detect.GpuDevice(name="AMD Radeon 8060S Graphics", vram_gb=0.5)
    hw_detect._apply_unified_override(device, cpu_name="AMD Ryzen AI Max+ 395",
                                      ram_gb=128.0)
    assert device.vram_gb == 128.0
    assert device.unified_memory is True


def test_nvidia_grace_dgx_spark_overrides_to_full_system_ram():
    device = hw_detect.GpuDevice(name="NVIDIA GB10", vram_gb=0.0)
    hw_detect._apply_unified_override(device, cpu_name="unknown", ram_gb=128.0)
    assert device.vram_gb == 128.0
    assert device.unified_memory is True


# -- Linux CPU name (Bugbot review, code review 2026-08-27) -----------------
#
# `platform.processor()` is documented as frequently empty on Linux (Python's
# own note in the stdlib docs), so `detect_hardware`'s AMD-unified-APU
# override — keyed off the CPU marketing name, never the GPU's own name
# (Strix Halo reports as an ordinary-looking iGPU, "AMD Radeon 8060S
# Graphics") — never fired there, and neither did the bandwidth table's
# "ryzen ai max"/"strix halo" rows, which are ALSO matched against the CPU
# name rather than the GPU device name for the identical reason.
#
# **Reasoned rather than observed on real Strix Halo hardware** — no such
# machine was available to verify `/proc/cpuinfo`'s exact `model name` line
# against a real BIOS string; the fix targets the documented, standard Linux
# mechanism (`/proc/cpuinfo`'s `model name:` field, the same source `lscpu`
# and `cat /proc/cpuinfo` themselves read) rather than a guess.


def test_linux_cpu_name_reads_the_model_name_field(tmp_path, monkeypatch):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "model name\t: AMD Ryzen AI Max+ 395 w/ Radeon 8060S\n"
        "cpu MHz\t\t: 3800.000\n"
    )
    monkeypatch.setattr(hw_detect, "_PROC_CPUINFO_PATH", str(cpuinfo))
    assert hw_detect._linux_cpu_name() == "AMD Ryzen AI Max+ 395 w/ Radeon 8060S"


def test_linux_cpu_name_is_empty_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hw_detect, "_PROC_CPUINFO_PATH", str(tmp_path / "nope"))
    assert hw_detect._linux_cpu_name() == ""


def test_linux_cpu_name_is_empty_when_no_model_name_line_exists(tmp_path, monkeypatch):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nvendor_id\t: GenuineIntel\n")
    monkeypatch.setattr(hw_detect, "_PROC_CPUINFO_PATH", str(cpuinfo))
    assert hw_detect._linux_cpu_name() == ""


def test_detect_hardware_reads_proc_cpuinfo_on_linux(monkeypatch):
    """The actual regression: `detect_hardware` used to read
    `platform.processor()` unconditionally, which is commonly empty on
    Linux — it now reaches for `/proc/cpuinfo` there instead, making the
    AMD-unified-APU override and its bandwidth row both reachable."""
    monkeypatch.setattr(hw_detect.sys, "platform", "linux")
    monkeypatch.setattr(hw_detect, "_nvidia_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_amd_gpus", lambda: [
        hw_detect.GpuDevice(name="AMD Radeon 8060S Graphics", vram_gb=0.5)])
    monkeypatch.setattr(hw_detect, "_sysfs_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_windows_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_apple_gpu", lambda ram_gb: None)
    monkeypatch.setattr(hw_detect, "_linux_cpu_name",
                        lambda: "AMD Ryzen AI Max+ 395 w/ Radeon 8060S")

    info = hw_detect.detect_hardware(ram_gb=128.0)

    assert info.gpus[0].unified_memory is True
    assert info.gpus[0].vram_gb == 128.0
    assert info.total_vram_gb == 128.0
    # The bandwidth table's "ryzen ai max" row, reachable via the CPU name
    # now that the device is known to be unified and its OWN name ("AMD
    # Radeon 8060S Graphics") matches nothing in the table.
    assert info.bandwidth_gb_s == 256.0


def test_detect_hardware_bandwidth_falls_back_to_the_device_name_first(monkeypatch):
    """A discrete GPU's own name is still tried first (and normally
    succeeds) — the CPU-name fallback only engages for a device the
    unified-APU override actually fired on."""
    monkeypatch.setattr(hw_detect.sys, "platform", "linux")
    monkeypatch.setattr(hw_detect, "_nvidia_gpus", lambda: [
        hw_detect.GpuDevice(name="NVIDIA GeForce RTX 4090", vram_gb=24.0)])
    monkeypatch.setattr(hw_detect, "_amd_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_sysfs_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_windows_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_apple_gpu", lambda ram_gb: None)
    monkeypatch.setattr(hw_detect, "_linux_cpu_name", lambda: "AMD Ryzen 9 7950X")

    info = hw_detect.detect_hardware(ram_gb=64.0)

    assert info.gpus[0].unified_memory is False
    assert info.bandwidth_gb_s == 1008.0


def test_a_discrete_gpu_is_not_overridden():
    device = hw_detect.GpuDevice(name="NVIDIA GeForce RTX 4090", vram_gb=24.0)
    hw_detect._apply_unified_override(device, cpu_name="AMD Ryzen 9 7950X",
                                      ram_gb=64.0)
    assert device.vram_gb == 24.0
    assert device.unified_memory is False


# -- every parser must agree on GpuDevice.vram_gb's unit (decimal GB) ------------


def test_every_parser_reports_the_same_decimal_gb_for_the_same_real_card(monkeypatch):
    """A real 24GB (decimal) RTX 4090 reports itself three different ways
    depending on which probe answered — `nvidia-smi` in MEBIBYTES,
    `rocm-smi`/the registry correction in raw BYTES, Windows' capped
    `AdapterRAM` also in bytes. All three parsers must land at the SAME
    decimal-GB figure for the same physical card; a parser that silently
    treats its input as already being the output unit (the bug this test
    guards against: `_parse_nvidia_smi` did `mib / 1024`, `_windows_gpus`
    did `bytes / 1024**3` — both binary-GiB math against a field documented
    as decimal GB) under-reports VRAM by ~7.4%, which is enough to flip a
    borderline model from `gpu` to `cpu-offload` in `fit._select_pool`."""
    target_gb = 24.0
    target_bytes = target_gb * 1e9

    nvidia = hw_detect._parse_nvidia_smi(
        f"NVIDIA GeForce RTX 4090, {target_bytes / (1024 * 1024)}\n")
    rocm = hw_detect._parse_rocm_smi(
        json.dumps({"card0": {"Card series": "NVIDIA GeForce RTX 4090",
                              "VRAM Total Memory (B)": str(int(target_bytes))}}))
    monkeypatch.setattr(hw_detect, "_run", lambda args, timeout=None:
                        f"NVIDIA GeForce RTX 4090|{int(target_bytes)}\n")
    monkeypatch.setattr(hw_detect, "_windows_registry_vram", lambda: {})
    windows = hw_detect._windows_gpus()

    assert nvidia is not None and rocm is not None and windows is not None
    for gpus in (nvidia, rocm, windows):
        assert gpus[0].vram_gb == pytest.approx(target_gb, rel=1e-6)


# -- multi-GPU aggregation --------------------------------------------------------


def test_total_vram_sums_across_multiple_gpus():
    gpus = [hw_detect.GpuDevice(name="RTX 4090", vram_gb=24.0),
            hw_detect.GpuDevice(name="RTX 4090", vram_gb=24.0)]
    assert hw_detect._total_vram_gb(gpus) == 48.0


# -- the bandwidth lookup table ----------------------------------------------------


@pytest.mark.parametrize("name,expected_range", [
    ("Apple M1", (60, 80)),
    ("Apple M1 Max", (350, 450)),
    ("Apple M2 Ultra", (750, 850)),
    ("Apple M4 Pro", (250, 300)),
    ("NVIDIA GeForce RTX 4090", (950, 1100)),
    ("NVIDIA H100 80GB HBM3", (3000, 3600)),
    ("AMD Radeon RX 7900 XTX", (900, 1000)),
])
def test_bandwidth_lookup_covers_named_families(name, expected_range):
    bw = hw_detect._bandwidth_for(name)
    assert bw is not None
    assert expected_range[0] <= bw <= expected_range[1]


def test_bandwidth_lookup_is_none_for_an_unknown_device():
    assert hw_detect._bandwidth_for("Some Unknown Card 9000") is None


def test_bandwidth_lookup_prefers_the_more_specific_match():
    """`M1 Max` must not be matched by a bare `M1` rule."""
    m1 = hw_detect._bandwidth_for("Apple M1")
    m1_max = hw_detect._bandwidth_for("Apple M1 Max")
    assert m1 is not None and m1_max is not None
    assert m1_max > m1


# -- the on-disk cache: this is what fit.py actually reads ------------------------


def test_cached_hardware_is_none_before_anything_has_ever_detected():
    assert hw_detect.cached_hardware() is None


def test_refresh_hardware_writes_a_cache_that_cached_hardware_then_reads(monkeypatch):
    gpus = [hw_detect.GpuDevice(name="RTX 4090", vram_gb=24.0)]
    monkeypatch.setattr(hw_detect, "detect_hardware", lambda ram_gb=None: hw_detect.HardwareInfo(
        gpus=gpus, total_vram_gb=24.0, bandwidth_gb_s=1008.0, detected_at=time.time()))
    hw_detect.refresh_hardware(ram_gb=32.0)
    cached = hw_detect.cached_hardware()
    assert cached is not None
    assert cached.total_vram_gb == 24.0
    assert cached.bandwidth_gb_s == 1008.0
    assert cached.gpus[0].name == "RTX 4090"


def test_a_corrupt_cache_reads_as_none(tmp_path):
    import os

    path = hw_detect._path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")
    assert hw_detect.cached_hardware() is None


def test_run_survives_non_utf8_bytes_from_a_real_child(tmp_path):
    """`_run` pins `encoding="utf-8", errors="replace"` (SPEC AI-18, house
    convention per `app_git.py`/`tests/test_subprocess_encoding.py`) rather
    than falling through `text=True`'s locale-dependent default — a
    GUI-launched process inherits no LANG/LC_ALL, which resolves to ASCII,
    so a vendor tool's non-ASCII byte would otherwise raise
    `UnicodeDecodeError` and crash the whole refresh. Driven against a REAL
    child process (not a monkeypatch of `_run` itself) so this actually
    exercises the `subprocess.run` kwargs rather than asserting they were
    typed correctly."""
    script = tmp_path / "emit_bad_bytes.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'NVIDIA GeForce RTX 4090\\xff\\xfe, 24564\\n')\n"
    )
    out = hw_detect._run([sys.executable, str(script)])
    assert out is not None
    assert "NVIDIA GeForce RTX 4090" in out
    assert "\ufffd" in out  # the replaced byte, not a raised exception


def test_detect_hardware_never_raises_when_every_probe_fails(monkeypatch):
    monkeypatch.setattr(hw_detect, "_nvidia_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_amd_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_sysfs_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_windows_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_apple_gpu", lambda ram_gb: None)
    info = hw_detect.detect_hardware(ram_gb=16.0)
    assert info.gpus == []
    assert info.total_vram_gb == 0.0
    assert info.bandwidth_gb_s is None
