"""The llama.cpp / GGUF text runner (SPEC §40, AI-11).

Five lines of code and a `pyproject.toml`, the same shell shape
`parakeet_mlx/worker.py` uses: the manifest
beside this file is the whole of what makes this folder its own environment,
and the runner itself is `runners/llama_text.py`, imported the same way
every other shell reaches its shared module one directory up.

**Named `llama_text`, not `llamacpp_text`, DELIBERATELY** — this folder is
`llamacpp_text/`, and a shared module with the SAME stem beside a same-named
folder is a footgun: a directory with no `__init__.py` is a namespace
portion, which loses to a same-name ordinary module in the same `sys.path`
entry ONLY as long as nobody ever adds one. The rule came from the
transformers family that used to sit beside this one — three folders
(`transformers_text/`, `_cuda`, `_rocm`) all importing a `torch_text.py`
whose stem matched none of them — and it outlives that family because it is
a fact about `sys.path`, not about torch: `llamacpp_text_vulkan/` imports the
same `llama_text.py` this folder does, so the collision this avoids is still
two folders away from happening by accident.

Nothing here may grow a second line of behaviour — a format check or a prompt
rule that lived in this shell would be a difference between this folder and a
future one that no test could see.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llama_text  # noqa: E402 - the whole runner; see runners/llama_text.py

if __name__ == "__main__":
    llama_text.main()
