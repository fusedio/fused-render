"""The llama.cpp / GGUF text embedding runner (SPEC §40, AI-11).

Five lines of code and a `pyproject.toml`, the same shell shape
`llamacpp_text/worker.py` uses: the manifest beside this file is the whole of
what makes this folder its own environment, and the runner itself is
`runners/llama_embed.py`, imported the same way every other shell reaches its
shared module one directory up.

**Named `llama_embed`, not `llamacpp_embed`, DELIBERATELY** — the rule
`llamacpp_text/worker.py` states at length and which is a fact about
`sys.path` rather than about any one library: a directory with no
`__init__.py` is a namespace portion, which loses to a same-name ordinary
module in the same `sys.path` entry only for as long as nobody adds one.
`llamacpp_embed_vulkan/` imports the same `llama_embed.py` this folder does,
so a module named after either folder would be one file away from shadowing
the other.

Nothing here may grow a second line of behaviour — a format check or a prompt
rule that lived in this shell would be a difference between this folder and
its Vulkan sibling that no test could see.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llama_embed  # noqa: E402 - the whole runner; see runners/llama_embed.py

if __name__ == "__main__":
    llama_embed.main()
