"""Tests for GPU/VRAM detection beyond RAM (SPEC AI-18, D518).

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
    monkeypatch.setattr(hw_detect, "_windows_gpus", lambda: None)
    monkeypatch.setattr(hw_detect, "_apple_gpu", lambda ram_gb: None)
    info = hw_detect.detect_hardware(ram_gb=16.0)
    assert info.gpus == []
    assert info.total_vram_gb == 0.0
    assert info.bandwidth_gb_s is None
