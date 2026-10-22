"""The CPU-index vision runner (SPEC §40, AI-33).

Five lines of code and a `pyproject.toml`. This folder installs torch from the CPU index (Apple Silicon: MPS).
The runner itself is `runners/torch_vision.py`, shared with its sibling folder;
this file may never grow a line of behaviour of its own, or the two variants
would differ in a way no test can see (the same rule as
`diffusers_image_cuda/worker.py`).
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch_vision  # noqa: E402 - the whole runner; see runners/torch_vision.py

if __name__ == "__main__":
    torch_vision.main()
