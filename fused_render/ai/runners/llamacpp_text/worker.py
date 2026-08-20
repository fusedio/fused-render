"""The llama.cpp / GGUF text runner (SPEC §40, AI-11).

Five lines of code and a `pyproject.toml`, the same shell shape
`transformers_text/worker.py` and `parakeet_mlx/worker.py` use: the manifest
beside this file is the whole of what makes this folder its own environment,
and the runner itself is `runners/llamacpp_text.py`, imported the same way
every other shell reaches its shared module one directory up.

Nothing here may grow a second line of behaviour — a format check or a prompt
rule that lived in this shell would be a difference between this folder and a
future one that no test could see.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llamacpp_text  # noqa: E402 - the whole runner; see runners/llamacpp_text.py

if __name__ == "__main__":
    llamacpp_text.main()
