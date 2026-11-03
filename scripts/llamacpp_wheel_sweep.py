#!/usr/bin/env python3
"""Integrity sweep of abetlen/llama-cpp-python release wheels.

For each version in the range, downloads the six CPU wheels (macOS arm64,
manylinux x86_64/aarch64, musllinux x86_64/aarch64, linux riscv64: the tags
`registry._llamacpp_platform` admits) and the vulkan x86_64 wheel from the
GitHub release assets, then runs `zipfile.ZipFile(...).testzip()` on each. A
bump of the `llama-cpp-python` pin in `fused_render/ai/runners/llamacpp_text*/
pyproject.toml` is only allowed where every shipped wheel passes (D411, D1413).

Run:  python scripts/llamacpp_wheel_sweep.py SCRATCH_DIR 30 36

SCRATCH_DIR receives the downloads (hundreds of MB; created if missing, files
already present are reused). 30 36 is the inclusive 0.3.x patch range. Output is
one row per wheel: version | index | tag | bytes | sha256[:12] | PASS/FAIL/error.
Compare FAIL hashes against the digest in `gh release view v<ver> --repo
abetlen/llama-cpp-python --json assets` to tell upstream-corrupt from a bad fetch.
Stdlib only; needs `curl` on PATH.
"""
import hashlib
import os
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor

BASE = "https://github.com/abetlen/llama-cpp-python/releases/download"
CPU = [
    "macosx_11_0_arm64",
    "manylinux2014_x86_64.manylinux_2_17_x86_64",
    "manylinux2014_aarch64.manylinux_2_17_aarch64",
    "musllinux_1_2_x86_64",
    "musllinux_1_2_aarch64",
    "linux_riscv64",
]
VULKAN = "manylinux2014_x86_64.manylinux_2_17_x86_64"


def jobs(out_dir, lo, hi):
    for i in range(hi, lo - 1, -1):
        v = f"0.3.{i}"
        for t in CPU:
            yield (out_dir, v, "cpu", t, f"{BASE}/v{v}/llama_cpp_python-{v}-py3-none-{t}.whl")
        t = VULKAN
        yield (out_dir, v, "vulkan", t, f"{BASE}/v{v}-vulkan/llama_cpp_python-{v}-py3-none-{t}.whl")


def run(job):
    out_dir, v, idx, t, url = job
    d = os.path.join(out_dir, v)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{idx}-{t.split('.')[0]}.whl")
    if not os.path.exists(p):
        part = p + ".part"
        r = subprocess.run(["curl", "-sSL", "--retry", "3", "-f", "-o", part, url],
                           capture_output=True, text=True)
        if r.returncode:
            if os.path.exists(part):
                os.remove(part)
            return (v, idx, t, None, None, "HTTP/ERR " + r.stderr.strip()[:80])
        os.replace(part, p)
    digest = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    h = digest.hexdigest()
    try:
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
        res = "PASS" if bad is None else "FAIL bad=" + bad
    except Exception as e:
        res = "FAIL " + type(e).__name__ + ": " + str(e)[:60]
    return (v, idx, t, os.path.getsize(p), h[:12], res)


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    out_dir, lo, hi = argv[1], int(argv[2]), int(argv[3])
    with ThreadPoolExecutor(4) as ex:
        for r in ex.map(run, jobs(out_dir, lo, hi)):
            print(*r, sep=" | ")


if __name__ == "__main__":
    main(sys.argv)
