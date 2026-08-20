"""The llama.cpp / GGUF text runner (SPEC §40, AI-11).

Five lines of code and a `pyproject.toml`, the same shell shape
`transformers_text/worker.py` and `parakeet_mlx/worker.py` use: the manifest
beside this file is the whole of what makes this folder its own environment,
and the runner itself is `runners/llama_text.py`, imported the same way
every other shell reaches its shared module one directory up.

**Named `llama_text`, not `llamacpp_text`, DELIBERATELY** — this folder is
`llamacpp_text/`, and a shared module with the SAME stem beside a same-named
folder is a footgun: a directory with no `__init__.py` is a namespace
portion, which loses to a same-name ordinary module in the same `sys.path`
entry ONLY as long as nobody ever adds one. The other three-folder runner
(`transformers_text/`, `transformers_text_cuda/`, `transformers_text_rocm/`
all importing `torch_text.py`) avoids this by construction — the shared
module's name never matches any of the folders that import it — and this
module follows the same rule rather than relying on the accident of a
missing `__init__.py` staying missing forever.

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
