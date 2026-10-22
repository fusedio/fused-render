"""The CUDA Wan video runner (D1424).

Five lines of code and a `pyproject.toml`. This folder installs PyPI's default torch, which on Linux is the CUDA build.
The runner itself is `runners/diffusers_video.py`; this file may never grow a
line of behaviour of its own, or a variant would differ from the shared body in
a way no test can see (the same rule as `vision_torch_cuda/worker.py` and
`diffusers_image_cuda/worker.py`).
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diffusers_video  # noqa: E402 - the whole runner; see runners/diffusers_video.py
diffusers_video.EXPECTED_DEVICE = "cuda"  # a CPU fallback here is a driver fault

if __name__ == "__main__":
    diffusers_video.main()
