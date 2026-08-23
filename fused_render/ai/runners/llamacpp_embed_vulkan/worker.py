"""The llama.cpp / GGUF text embedding runner — the Vulkan variant (SPEC §40,
AI-11).

Five lines of code and a `pyproject.toml`, the same shell shape
`llamacpp_embed/worker.py` uses: the manifest beside this file is the whole of
what makes this folder its own environment, and the runner itself is
`runners/llama_embed.py` — the SAME module `llamacpp_embed/worker.py`
imports, reused rather than forked. Which wheel a user installed is a fact
about the hardware they picked on the Engines tab and never about how an
encoder pass runs; `llama_embed.load()` asks llama.cpp itself whether GPU
offload is available rather than inferring it from which folder started the
process, which is what lets one module serve both.

**Named `llama_embed`, not `llamacpp_embed`, for the reason
`llamacpp_embed/worker.py`'s own docstring states** — a shared module with the
same stem as a sibling folder would be a footgun the moment either folder
grew an `__init__.py`.

Nothing here may grow a second line of behaviour — a format check or a
Vulkan-specific pooling rule that lived in this shell would be a difference
between this folder and its CPU/Metal sibling that no test could see.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llama_embed  # noqa: E402 - the whole runner; see runners/llama_embed.py

if __name__ == "__main__":
    llama_embed.main()
