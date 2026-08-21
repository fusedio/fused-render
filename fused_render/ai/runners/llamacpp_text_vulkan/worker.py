"""The llama.cpp / GGUF text runner — the Vulkan variant (SPEC §40, AI-11).

Five lines of code and a `pyproject.toml`, the same shell shape
`llamacpp_text/worker.py` uses: the manifest beside this file is the whole of
what makes this folder its own environment, and the runner itself is
`runners/llama_text.py` — the SAME module `llamacpp_text/worker.py` imports,
reused rather than forked. Which wheel a user installed is a fact about the
hardware they picked on the Engines tab and never about how a token loop
runs, the identical argument `transformers_text_cuda/worker.py`'s docstring
makes for its own three-folder split.

**Named `llama_text`, not `llamacpp_text`, for the reason
`llamacpp_text/worker.py`'s own docstring states** — a shared module with the
same stem as this folder would be a footgun the moment this folder grew an
`__init__.py`. That reasoning is about THIS folder's own name, so it applies
here unchanged even though the shared module lives one level up from a
DIFFERENT sibling folder now too.

Nothing here may grow a second line of behaviour — a format check or a
Vulkan-specific prompt rule that lived in this shell would be a difference
between this folder and its CPU/Metal sibling that no test could see.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llama_text  # noqa: E402 - the whole runner; see runners/llama_text.py

if __name__ == "__main__":
    llama_text.main()
