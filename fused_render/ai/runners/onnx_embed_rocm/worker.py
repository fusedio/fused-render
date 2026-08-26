"""The ROCm embeddings runner on ONNX Runtime — the AMD-on-Linux variant
(SPEC §40).

Five lines, and they are the SAME five `onnx_embed/worker.py` holds: the runner
is `runners/onnx_embed.py`, shared by all four folders, and this shell exists
only so the supervisor has a `worker.py` to spawn on THIS folder's venv. Read
`onnx_embed/worker.py`'s docstring for why a hardware variant is a folder rather
than a flag, and this folder's `pyproject.toml` for the one line that differs.

Nothing here may grow a second line of behaviour — see the CPU shell.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import onnx_embed  # noqa: E402 - the whole runner; see runners/onnx_embed.py

if __name__ == "__main__":
    onnx_embed.main()
